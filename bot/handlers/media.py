import asyncio
import logging
import time
import uuid
from aiogram import Router, F

logger = logging.getLogger(__name__)
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_MEDIA, MENU_BUTTONS, main_menu_kb, inline_main_menu_kb
from bot.keyboards.billing import quick_topup_kb
from bot.keyboards.media import (
    media_type_kb, media_edit_kb, model_top_kb, model_variant_kb,
    model_select_text, model_variant_text, back_to_model_kb, back_to_confirm_kb,
    image_confirm_kb, image_ratio_kb, image_resolution_kb,
    video_confirm_kb, video_duration_picker_kb, audio_confirm_kb, edit_confirm_kb,
    style_ref_collecting_kb, after_generation_kb, gen_waiting_kb, error_kb,
)
from providers.base import ProviderError, ProviderContentPolicyError, TaskType
from providers.router import get_models_for_task
from services import media_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()

# user_id → asyncio.Task текущей генерации (для возможности отмены)
_active_tasks: dict[int, asyncio.Task] = {}
# user_id → время запуска генерации (monotonic), для окна отмены
_task_start_times: dict[int, float] = {}

_CANCEL_WINDOW_SEC = 5  # секунд, в течение которых отмена ещё возможна


async def _update_sref_status(bot, chat_id: int, state: FSMContext, text: str, kb) -> None:
    """Редактирует сообщение-счётчик ориентиров; если не удаётся — отправляет новое."""
    data = await state.get_data()
    msg_id = data.get("_sref_msg_id")
    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id, reply_markup=kb,
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, reply_markup=kb)
    await state.update_data(_sref_msg_id=sent.message_id)


async def _notify_generating(bot, chat_id: int, delay: float = 5.0) -> None:
    """Отправляет временное уведомление 'Идёт генерация' и удаляет его через delay секунд."""
    try:
        msg = await bot.send_message(chat_id, "⏳ Идёт генерация, подожди...")
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass


async def _track_msg(state: FSMContext, msg_id: int) -> None:
    """Запоминает message_id отправленного ботом сообщения для последующей очистки."""
    data = await state.get_data()
    ids = list(data.get("_tracked_msg_ids") or [])
    if msg_id not in ids:
        ids.append(msg_id)
    await state.update_data(_tracked_msg_ids=ids)


async def _delete_msgs_below(bot, chat_id: int, state: FSMContext, anchor_id: int) -> None:
    """Удаляет трекнутые сообщения с ID > anchor_id, сохраняет остальные."""
    data = await state.get_data()
    ids = list(data.get("_tracked_msg_ids") or [])
    to_delete = [mid for mid in ids if mid > anchor_id]
    keep = [mid for mid in ids if mid <= anchor_id]
    await state.update_data(_tracked_msg_ids=keep or None)
    for mid in to_delete:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


class GenerationCancelledError(Exception):
    pass


async def _start_tracked(user_id: int, send_msg: Message, tg_user, state: FSMContext, data: dict) -> None:
    """Запускает _run_generation как Task, чтобы пользователь мог отменить нажатием кнопки."""
    task = asyncio.create_task(_run_generation(send_msg, tg_user, state, data))
    _active_tasks[user_id] = task
    _task_start_times[user_id] = time.monotonic()
    try:
        await task
    except asyncio.CancelledError:
        raise GenerationCancelledError()
    finally:
        _active_tasks.pop(user_id, None)
        _task_start_times.pop(user_id, None)


class MediaStates(StatesGroup):
    select_type = State()
    select_model = State()
    enter_prompt = State()
    enter_reference = State()
    confirm = State()


_TYPE_LABELS = {
    "image": "Фото",
    "video": "Видео",
    "audio": "Аудио",
    "photo_edit": "Изменить фото",
    "video_edit": "Изменить видео",
}

_TYPE_TO_TASK = {
    "image": TaskType.IMAGE_GENERATION,
    "video": TaskType.VIDEO_GENERATION,
    "audio": TaskType.AUDIO_GENERATION,
    "photo_edit": TaskType.IMAGE_EDIT,
    "video_edit": TaskType.VIDEO_EDIT,
}

_PROMPT_HINTS = {
    "image": "Опиши изображение, которое хочешь получить:",
    "video": "Опиши видео (действие, сцена, стиль):",
    "audio": "Введи текст для озвучки или описание музыки:",
    "photo_edit": "Опиши, что нужно изменить на фото:",
    "video_edit": "Опиши, что нужно изменить в видео:",
}


def _confirm_card_text(data: dict) -> str:
    media_type = data.get("media_type", "")
    prompt = data.get("prompt") or "не задано"
    model_label = data.get("model_label", "—")
    model_description = data.get("model_description", "")

    lines = []
    variant_desc = data.get("model_variant_description", "")
    if variant_desc:
        # Для варианта из группы — компактное описание именно этого варианта
        lines.append(f"<b>Модель:</b> {model_label}\n<blockquote expandable>{variant_desc}</blockquote>")
    elif model_description and not data.get("model_has_group"):
        # Для одиночных моделей — полное описание с именем как заголовком
        lines.append(f"<b>{model_label}</b>\n<blockquote expandable>{model_description}</blockquote>")
    else:
        lines.append(f"<b>Модель:</b> {model_label}")

    if media_type == "image":
        lines.append(f"<b>Масштаб:</b> {data.get('aspect_ratio', '1:1')}  |  <b>Качество:</b> {data.get('resolution', '1K')}")
        _srefs = data.get("style_reference_file_ids") or []
        if _srefs:
            lines.append(f"<b>{'Ориентир' if len(_srefs) == 1 else 'Ориентиры'}:</b> {len(_srefs)} фото ✅")
    elif media_type == "video":
        lines.append(f"<b>Длительность:</b> {data.get('duration', 5)} сек")
    elif media_type == "audio":
        t = "Озвучка" if data.get("audio_type", "voice") == "voice" else "Музыка"
        lines.append(f"<b>Тип аудио:</b> {t}")
    elif media_type in ("photo_edit", "video_edit"):
        ref_type = data.get("reference_type")
        if ref_type:
            label = "Видео" if "video" in ref_type else "Фото"
            lines.append(f"<b>Загружено:</b> {label} ✅")
        else:
            lines.append("<b>Загружено:</b> не добавлено")
        if media_type == "photo_edit":
            _srefs = data.get("style_reference_file_ids") or []
            if _srefs:
                lines.append(f"<b>{'Ориентир' if len(_srefs) == 1 else 'Ориентиры'}:</b> {len(_srefs)} фото ✅")

    if media_type == "audio":
        prompt_label = "Текст для озвучки" if data.get("audio_type", "voice") == "voice" else "Описание музыки"
    else:
        prompt_label = "Описание"
    lines.append(f"<b>{prompt_label}:</b> {prompt}")
    return "\n".join(lines)


def _confirm_kb(data: dict):
    has_prompt = bool(data.get("prompt"))
    style_ref_count = len(data.get("style_reference_file_ids") or [])
    media_type = data.get("media_type")
    if media_type == "image":
        return image_confirm_kb(
            data.get("aspect_ratio", "1:1"), data.get("resolution", "1K"),
            has_prompt=has_prompt, style_ref_count=style_ref_count,
            max_style_refs=data.get("model_max_style_refs", 14),
        )
    elif media_type == "video":
        return video_confirm_kb(
            data.get("duration", 5),
            has_prompt=has_prompt,
            duration_options=data.get("model_duration_options"),
            has_first_frame=bool(data.get("video_first_frame_file_id")),
            has_last_frame=bool(data.get("video_last_frame_file_id")),
            frames_expanded=bool(data.get("video_frames_expanded")),
        )
    elif media_type == "audio":
        return audio_confirm_kb(has_prompt=has_prompt)
    elif media_type in ("photo_edit", "video_edit"):
        return edit_confirm_kb(
            has_prompt=has_prompt,
            has_reference=bool(data.get("reference_file_id")),
            media_type=media_type,
            style_ref_count=style_ref_count,
        )


# ─── Вход в раздел ───────────────────────────────────────────────────────────

MEDIA_MENU_TEXT = (
    "✨ <b>Генерация</b> — создать новый контент с нуля\n"
    "✏️ <b>Редактирование</b> — улучшение качества, замена лиц и т.д.\n\n"
    "Выбери действие:"
)


@router.message(F.text == BTN_MEDIA)
async def media_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(MediaStates.select_type)
    await message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())


# ─── Выбор типа ──────────────────────────────────────────────────────────────

@router.callback_query(MediaStates.select_type, F.data == "media:edit_menu")
async def edit_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Выбери, что нужно изменить:",
        reply_markup=media_edit_kb(),
    )
    await callback.answer()


@router.callback_query(MediaStates.select_type, F.data.startswith("media:type:"))
async def select_type(callback: CallbackQuery, state: FSMContext) -> None:
    media_type = callback.data.split(":")[2]
    defaults = {
        "image": {"aspect_ratio": "1:1", "resolution": "1K"},
        "video": {"duration": 5},
        "audio": {"audio_type": "voice"},
    }
    # Сбрасываем все данные предыдущего раздела (промпт, модель и т.д.)
    await state.set_data({"media_type": media_type, **defaults.get(media_type, {})})

    task_type = _TYPE_TO_TASK.get(media_type)
    models = get_models_for_task(task_type) if task_type else []

    if not models:
        await callback.answer("Нет доступных моделей для этого типа", show_alert=True)
        return

    type_label = _TYPE_LABELS.get(media_type, media_type)
    await callback.message.edit_text(
        model_select_text(type_label, models),
        parse_mode="HTML",
        reply_markup=model_top_kb(models),
    )
    await state.set_state(MediaStates.select_model)
    await callback.answer()


# ─── Выбор модели ────────────────────────────────────────────────────────────

@router.callback_query(MediaStates.select_model, F.data.startswith("media:model:"))
async def select_model(callback: CallbackQuery, state: FSMContext) -> None:
    model_slug = callback.data[len("media:model:"):]
    data = await state.get_data()
    media_type = data.get("media_type", "")

    task_type = _TYPE_TO_TASK.get(media_type)
    models = get_models_for_task(task_type) if task_type else []
    model_cfg = next((m for m in models if m["id"] == model_slug), None)

    if not model_cfg:
        await callback.answer("Модель недоступна", show_alert=True)
        return

    aspect_ratios = model_cfg.get("aspect_ratios", [])
    duration_options = model_cfg.get("duration_options", [5, 10])
    min_duration = model_cfg.get("min_duration", min(duration_options))
    max_duration = model_cfg.get("max_duration", max(duration_options))
    update = {
        "model_slug": model_slug,
        "model_label": model_cfg["label"],
        "model_description": model_cfg.get("description", ""),
        "model_variant_description": model_cfg.get("variant_description", ""),
        "model_has_group": bool(model_cfg.get("group")),
        "model_actual_id": model_cfg["model_id"],
        "model_aspect_ratios": aspect_ratios,
        "model_duration_options": duration_options,
        "model_min_duration": min_duration,
        "model_max_duration": max_duration,
        "model_max_style_refs": model_cfg.get("max_style_refs", 14),
        "model_resolutions": model_cfg.get("resolutions"),
    }
    # если текущий ratio недоступен у новой модели — сбрасываем на 1:1
    if aspect_ratios and data.get("aspect_ratio", "1:1") not in aspect_ratios:
        update["aspect_ratio"] = "1:1"
    # если текущее разрешение недоступно у новой модели — сбрасываем на первое из списка
    allowed_res = model_cfg.get("resolutions")
    if allowed_res and data.get("resolution", "1K") not in allowed_res:
        update["resolution"] = allowed_res[0]
    # если текущая длительность вне допустимых опций — сбрасываем на первую
    if media_type == "video" and data.get("duration", 5) not in duration_options:
        update["duration"] = duration_options[0]
    # для аудио — тип (voice/music) берём из конфига модели
    if media_type == "audio":
        update["audio_type"] = model_cfg.get("audio_type", "voice")

    await state.update_data(**update)
    data = await state.get_data()

    if media_type in ("photo_edit", "video_edit"):
        await state.set_state(MediaStates.confirm)
        await state.update_data(confirm_msg_id=callback.message.message_id)
        await callback.message.edit_text(
            _confirm_card_text(data),
            parse_mode="HTML",
            reply_markup=_confirm_kb(data),
        )
    else:
        await state.set_state(MediaStates.confirm)
        await state.update_data(confirm_msg_id=callback.message.message_id)
        await callback.message.edit_text(
            _confirm_card_text(data),
            parse_mode="HTML",
            reply_markup=_confirm_kb(data),
        )

    await callback.answer()


# ─── Выбор группы (раскрывает версии) ───────────────────────────────────────

@router.callback_query(MediaStates.select_model, F.data.startswith("media:group:"))
async def select_group(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = callback.data[len("media:group:"):]
    data = await state.get_data()
    task_type = _TYPE_TO_TASK.get(data.get("media_type", ""))
    all_models = get_models_for_task(task_type) if task_type else []
    variants = [m for m in all_models if m.get("group") == group_id]

    if not variants:
        await callback.answer("Нет доступных версий", show_alert=True)
        return

    group_label = variants[0].get("group_label", group_id)
    description = variants[0].get("description", "")
    await callback.message.edit_text(
        model_variant_text(group_label, description),
        parse_mode="HTML",
        reply_markup=model_variant_kb(variants),
    )
    await callback.answer()


# ─── Навигация назад ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "media:back:type")
async def back_to_type(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MediaStates.select_type)
    await callback.message.edit_text(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())
    await callback.answer()


async def _restore_gen_snapshot(callback: CallbackQuery, state: FSMContext) -> bool:
    """Восстанавливает состояние генерации из снапшота и показывает confirm-карточку.

    Возвращает True если снапшот был и восстановление прошло успешно, иначе False.
    """
    data = await state.get_data()
    snapshot = data.get("_gen_snapshot")
    if not snapshot:
        return False
    logger.info(
        "_restore_gen_snapshot: slug=%s label=%s media_type=%s prompt=%r",
        snapshot.get("model_slug"), snapshot.get("model_label"),
        snapshot.get("media_type"), (snapshot.get("prompt") or "")[:40],
    )
    await state.update_data(
        quick_edit=None,
        _gen_snapshot=None,
        media_type=snapshot.get("media_type", "image"),
        model_slug=snapshot.get("model_slug"),
        model_label=snapshot.get("model_label"),
        model_description=snapshot.get("model_description"),
        model_variant_description=snapshot.get("model_variant_description"),
        model_has_group=snapshot.get("model_has_group"),
        model_actual_id=snapshot.get("model_actual_id"),
        model_aspect_ratios=snapshot.get("model_aspect_ratios"),
        model_resolutions=snapshot.get("model_resolutions"),
        model_max_style_refs=snapshot.get("model_max_style_refs"),
        prompt=snapshot.get("prompt"),
        reference_file_id=None,
        reference_type=None,
        adding_style_ref=None,
    )
    await state.set_state(MediaStates.confirm)
    data = await state.get_data()
    text = _confirm_card_text(data)
    kb = _confirm_kb(data)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    sent = await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await _track_msg(state, sent.message_id)
    await state.update_data(confirm_msg_id=sent.message_id)
    await callback.answer()
    return True


@router.callback_query(F.data == "media:back:model")
async def back_to_model(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()

    # Если пришли из быстрого редактирования — возвращаем к карточке генерации
    if data.get("quick_edit"):
        logger.info(
            "back_to_model: quick_edit=True, has_snapshot=%s, media_type=%s",
            bool(data.get("_gen_snapshot")), data.get("media_type"),
        )
        if await _restore_gen_snapshot(callback, state):
            return
        # Снапшот не найден (старая сессия) — fallback: список моделей генерации фото
        models_img = get_models_for_task(TaskType.IMAGE_GENERATION)
        await state.update_data(
            quick_edit=None, _gen_snapshot=None,
            media_type="image",
            model_slug=None, model_label=None, model_description=None,
            model_variant_description=None, model_has_group=None,
            model_aspect_ratios=None, model_actual_id=None,
            reference_file_id=None, reference_type=None,
        )
        await state.set_state(MediaStates.select_model)
        text = model_select_text("Фото", models_img)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=model_top_kb(models_img))
        except Exception:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            sent = await callback.message.answer(text, parse_mode="HTML", reply_markup=model_top_kb(models_img))
            await _track_msg(state, sent.message_id)
        await callback.answer()
        return

    media_type = data.get("media_type", "")
    task_type = _TYPE_TO_TASK.get(media_type)
    models = get_models_for_task(task_type) if task_type else []
    type_label = _TYPE_LABELS.get(media_type, media_type)

    # Сессия устарела (перезапуск бота или слишком давно) — нет данных о типе задачи
    if not models:
        await state.clear()
        try:
            await callback.message.edit_text(
                "⚠️ Сессия устарела — начни заново.",
                reply_markup=inline_main_menu_kb(),
            )
        except Exception:
            await callback.message.answer("⚠️ Сессия устарела — начни заново.", reply_markup=main_menu_kb())
        await callback.answer()
        return

    await state.update_data(
        quick_edit=None,
        _gen_snapshot=None,
        _gen_nonce=None,
        prompt=None, model_slug=None, model_label=None,
        model_description=None, model_variant_description=None,
        model_has_group=None, model_aspect_ratios=None,
        model_duration_options=None, model_min_duration=None, model_max_duration=None,
        model_max_style_refs=None, model_resolutions=None, entering_duration=None, confirm_msg_id=None,
        reference_file_id=None, reference_type=None,
        generated_file_id=None,
        style_reference_file_ids=None, adding_style_ref=None,
        managing_style_ref=None, managing_style_ref_index=None,
        video_first_frame_file_id=None, video_last_frame_file_id=None,
        video_frames_expanded=None,
    )
    await state.set_state(MediaStates.select_model)
    text = model_select_text(type_label, models)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=model_top_kb(models))
    except Exception:
        # callback.message может быть фото (результат генерации) — edit_text на фото не работает
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        sent = await callback.message.answer(text, parse_mode="HTML", reply_markup=model_top_kb(models))
        await _track_msg(state, sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "media:back:menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "media:switch_to_photo_edit")
async def switch_to_photo_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Переключиться в режим редактирования фото прямо из подсказки."""
    await state.set_data({"media_type": "photo_edit"})
    models = get_models_for_task(TaskType.IMAGE_EDIT)
    await callback.message.edit_text(
        model_select_text("Изменить фото", models),
        parse_mode="HTML",
        reply_markup=model_top_kb(models),
    )
    await state.set_state(MediaStates.select_model)
    await callback.answer()


@router.callback_query(F.data == "menu:media")
async def menu_to_media(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.delete()
    await state.clear()
    await state.set_state(MediaStates.select_type)
    await callback.message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())


@router.callback_query(F.data == "media:again")
async def generate_again(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к карточке той же модели с очищенным промптом и референсами."""
    data = await state.get_data()

    if not data.get("model_slug") or not data.get("media_type"):
        # Сессия устарела — возвращаем в выбор типа медиа
        await state.clear()
        await state.set_state(MediaStates.select_type)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())
        await callback.answer()
        return

    # В режиме быстрого редактирования «Попробовать снова» возвращает к вводу описания
    if data.get("quick_edit"):
        await state.update_data(prompt=None, _tracked_msg_ids=None, _is_generating=None, _cleanup_warned_msg_id=None)
        await state.set_state(MediaStates.enter_prompt)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        sent = await callback.message.answer("Опиши, что нужно изменить на фото:", reply_markup=back_to_confirm_kb())
        await _track_msg(state, sent.message_id)
        await callback.answer()
        return

    # Очищаем контент (промпт, референсы, ориентиры), сохраняем модель и формат
    await state.update_data(
        prompt=None,
        reference_file_id=None, reference_type=None,
        style_reference_file_ids=None,
        generated_file_id=None,
        video_first_frame_file_id=None, video_last_frame_file_id=None,
        confirm_msg_id=None,
        quick_edit=None,
        _gen_snapshot=None,
        adding_style_ref=None,
        managing_style_ref=None, managing_style_ref_index=None,
        video_frames_expanded=None,
        entering_duration=None,
        _tracked_msg_ids=None,
        _is_generating=None,
        _cleanup_warned_msg_id=None,
    )
    await state.set_state(MediaStates.confirm)
    data = await state.get_data()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    sent = await callback.message.answer(
        _confirm_card_text(data),
        parse_mode="HTML",
        reply_markup=_confirm_kb(data),
    )
    await _track_msg(state, sent.message_id)
    await state.update_data(confirm_msg_id=sent.message_id)
    await callback.answer()


def _find_edit_model(gen_model_id: str, gen_slug: str = "") -> dict | None:
    """Возвращает edit-модель для сгенерированного фото.

    Порядок поиска:
    1. Точное совпадение model_id (работает когда gen и edit используют одинаковый model_id).
    2. Slug-конвенция: gen_slug + "-edit" (основной способ, требует соблюдения именования).
    3. Первая доступная edit-модель как запасной вариант.
    """
    edit_models = get_models_for_task(TaskType.IMAGE_EDIT)
    by_id = next((m for m in edit_models if m.get("model_id") == gen_model_id), None)
    if by_id:
        return by_id
    if gen_slug:
        by_slug = next((m for m in edit_models if m.get("id") == gen_slug + "-edit"), None)
        if by_slug:
            return by_slug
    return edit_models[0] if edit_models else None


async def _apply_edit_model_to_state(state: FSMContext, edit_prompt: str | None = None) -> dict | None:
    """Переключает стейт в photo_edit для сгенерированного фото. Возвращает edit_model или None."""
    data = await state.get_data()
    edit_model = _find_edit_model(data.get("model_actual_id", ""), data.get("model_slug", ""))
    if not edit_model:
        return None
    logger.info(
        "_apply_edit_model_to_state: saving snapshot slug=%s label=%s media_type=%s prompt=%r",
        data.get("model_slug"), data.get("model_label"), data.get("media_type"),
        (data.get("prompt") or "")[:40],
    )
    # Сохраняем снапшот генерации, чтобы «Назад» мог вернуть пользователя к ней
    await state.update_data(
        _gen_snapshot={
            "media_type": data.get("media_type"),
            "model_slug": data.get("model_slug"),
            "model_label": data.get("model_label"),
            "model_description": data.get("model_description"),
            "model_variant_description": data.get("model_variant_description"),
            "model_has_group": data.get("model_has_group"),
            "model_actual_id": data.get("model_actual_id"),
            "model_aspect_ratios": data.get("model_aspect_ratios"),
            "model_resolutions": data.get("model_resolutions"),
            "model_max_style_refs": data.get("model_max_style_refs"),
            "prompt": data.get("prompt"),
            "confirm_msg_id": data.get("confirm_msg_id"),
        },
        media_type="photo_edit",
        reference_file_id=data["generated_file_id"],
        reference_type="photo",
        model_slug=edit_model["id"],
        model_label=edit_model["label"],
        model_description=edit_model.get("description", ""),
        model_variant_description=edit_model.get("variant_description", ""),
        model_has_group=bool(edit_model.get("group")),
        model_actual_id=edit_model["model_id"],
        model_aspect_ratios=None,
        **({"prompt": edit_prompt} if edit_prompt is not None else {}),
    )
    return edit_model


@router.callback_query(F.data == "media:edit_generated")
async def edit_generated_image(callback: CallbackQuery, state: FSMContext) -> None:
    """Редактировать только что сгенерированное фото — подставляет его как референс."""
    data = await state.get_data()
    if not data.get("generated_file_id"):
        await callback.answer("Изображение не найдено, попробуй снова.", show_alert=True)
        return

    edit_model = await _apply_edit_model_to_state(state)
    if not edit_model:
        await callback.answer("Нет доступных моделей для редактирования.", show_alert=True)
        return

    # Флаг: после ввода описания сразу запустить редактирование (без confirm-карточки)
    await state.update_data(quick_edit=True)
    await state.set_state(MediaStates.enter_prompt)
    sent = await callback.message.answer(
        "Опиши, что нужно изменить на фото:",
        reply_markup=back_to_confirm_kb(),
    )
    await _track_msg(state, sent.message_id)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_reference")
async def add_reference_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Добавить фото/видео' на карточке редактирования."""
    data = await state.get_data()
    media_type = data.get("media_type", "")
    hint = "Отправь фото для редактирования:" if media_type == "photo_edit" else "Отправь видео для редактирования:"
    # Сбрасываем флаги ориентиров — здесь добавляется основное фото, не стиль-реф
    await state.update_data(adding_style_ref=None, managing_style_ref=None, managing_style_ref_index=None)
    await callback.message.edit_text(hint, reply_markup=back_to_confirm_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_style_ref")
async def add_style_ref_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Добавить ориентир' — запрашивает фото-ориентиры (до 14 шт.)."""
    data = await state.get_data()
    count = len(data.get("style_reference_file_ids") or [])
    max_refs = data.get("model_max_style_refs", 14)
    hint = (
        f"📎 Уже добавлено {count} фото. Пришли ещё (до {max_refs} всего):"
        if count else
        f"📎 Пришли фото-ориентиры (до {max_refs} штук). Нейросеть будет ориентироваться на них при генерации:"
    )
    await callback.message.edit_text(hint, reply_markup=style_ref_collecting_kb(count, max_refs))
    await state.update_data(adding_style_ref=True, _sref_msg_id=callback.message.message_id)
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(F.data == "media:style_ref_done")
async def style_ref_done(callback: CallbackQuery, state: FSMContext) -> None:
    """Завершение сбора ориентиров — возврат к карточке подтверждения."""
    await state.update_data(adding_style_ref=None)
    await state.set_state(MediaStates.confirm)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await state.update_data(confirm_msg_id=callback.message.message_id)
    await callback.answer()


@router.callback_query(F.data == "media:style_ref_clear")
async def style_ref_clear(callback: CallbackQuery, state: FSMContext) -> None:
    """Очистить все ориентиры и остаться в режиме сбора."""
    data = await state.get_data()
    max_refs = data.get("model_max_style_refs", 14)
    await state.update_data(style_reference_file_ids=[], managing_style_ref=None, managing_style_ref_index=None)
    await callback.message.edit_text(
        f"📎 Пришли фото-ориентиры (до {max_refs} штук). Нейросеть будет ориентироваться на них при генерации:",
        reply_markup=style_ref_collecting_kb(0, max_refs),
    )
    await callback.answer("Ориентиры очищены", show_alert=False)


@router.callback_query(F.data == "media:style_ref_delete")
async def style_ref_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Запрашивает номер фото для удаления."""
    data = await state.get_data()
    count = len(data.get("style_reference_file_ids") or [])
    max_refs = data.get("model_max_style_refs", 14)
    await state.update_data(managing_style_ref="delete")
    await callback.message.edit_text(
        f"Введи номер фото для удаления (1–{count}):",
        reply_markup=style_ref_collecting_kb(count, max_refs),
    )
    await callback.answer()


@router.callback_query(F.data == "media:style_ref_replace")
async def style_ref_replace_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Запрашивает номер фото для замены."""
    data = await state.get_data()
    count = len(data.get("style_reference_file_ids") or [])
    max_refs = data.get("model_max_style_refs", 14)
    await state.update_data(managing_style_ref="replace")
    await callback.message.edit_text(
        f"Введи номер фото для замены (1–{count}):",
        reply_markup=style_ref_collecting_kb(count, max_refs),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:toggle_frames")
async def toggle_frames(callback: CallbackQuery, state: FSMContext) -> None:
    """Раскрывает кнопки выбора кадров на карточке видео."""
    await state.update_data(video_frames_expanded=True)
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_first_frame")
async def add_first_frame(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Первый кадр' на карточке видео-генерации."""
    await state.update_data(adding_video_frame="first")
    data = await state.get_data()
    has = bool(data.get("video_first_frame_file_id"))
    hint = "📎 Отправь другое фото для первого кадра:" if has else "📎 Отправь фото — оно станет первым кадром видео:"
    await callback.message.edit_text(hint, reply_markup=back_to_confirm_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_last_frame")
async def add_last_frame(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Последний кадр' на карточке видео-генерации."""
    await state.update_data(adding_video_frame="last")
    data = await state.get_data()
    has = bool(data.get("video_last_frame_file_id"))
    hint = "📎 Отправь другое фото для последнего кадра:" if has else "📎 Отправь фото — оно станет последним кадром видео:"
    await callback.message.edit_text(hint, reply_markup=back_to_confirm_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(F.data == "media:back:confirm")
async def back_to_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к карточке настроек — работает после генерации и из enter_reference."""
    data = await state.get_data()

    # В режиме быстрого редактирования «Назад» возвращает к карточке генерации
    if data.get("quick_edit") and await _restore_gen_snapshot(callback, state):
        return

    await state.update_data(adding_style_ref=None, managing_style_ref=None, managing_style_ref_index=None)
    await state.set_state(MediaStates.confirm)
    text = _confirm_card_text(data)
    kb = _confirm_kb(data)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await state.update_data(confirm_msg_id=callback.message.message_id)
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        sent = await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await _track_msg(state, sent.message_id)
        await state.update_data(confirm_msg_id=sent.message_id)
    await callback.answer()


# ─── Загрузка референса (только для edit) ────────────────────────────────────

async def _show_confirm_after_reference(message: Message, state: FSMContext) -> None:
    """Переходит в confirm state и показывает карточку после загрузки референса."""
    data = await state.get_data()
    await state.set_state(MediaStates.confirm)
    sent = await message.answer(
        _confirm_card_text(data),
        parse_mode="HTML",
        reply_markup=_confirm_kb(data),
    )
    await _track_msg(state, sent.message_id)
    await state.update_data(confirm_msg_id=sent.message_id)


@router.message(MediaStates.enter_reference, F.photo)
async def receive_reference_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("media_type") == "video_edit":
        await message.answer("Для редактирования видео пришли видеофайл, а не фото.", reply_markup=back_to_model_kb())
        return
    photo = message.photo[-1]
    if data.get("adding_style_ref"):
        await message.delete()
        srefs = list(data.get("style_reference_file_ids") or [])
        max_refs = data.get("model_max_style_refs", 14)
        if data.get("managing_style_ref") == "replace_photo":
            idx = data.get("managing_style_ref_index", 0)
            if 0 <= idx < len(srefs):
                srefs[idx] = photo.file_id
            await state.update_data(
                style_reference_file_ids=srefs,
                managing_style_ref=None, managing_style_ref_index=None,
            )
            await _update_sref_status(
                message.bot, message.chat.id, state,
                f"✅ Фото #{idx + 1} заменено. Всего: {len(srefs)}/{max_refs}",
                style_ref_collecting_kb(len(srefs), max_refs),
            )
            return
        if len(srefs) < max_refs:
            srefs.append(photo.file_id)
        await state.update_data(style_reference_file_ids=srefs)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"✅ Добавлено! Всего ориентиров: {len(srefs)}/{max_refs}",
            style_ref_collecting_kb(len(srefs), max_refs),
        )
        return
    if data.get("media_type") == "video":
        # Кадр для image-to-video: first или last в зависимости от нажатой кнопки
        frame_slot = data.get("adding_video_frame", "first")
        key = "video_first_frame_file_id" if frame_slot == "first" else "video_last_frame_file_id"
        await state.update_data(**{key: photo.file_id, "adding_video_frame": None})
        await _show_confirm_after_reference(message, state)
        return
    await state.update_data(reference_file_id=photo.file_id, reference_type="photo")
    await _show_confirm_after_reference(message, state)


@router.message(MediaStates.enter_reference, F.video)
async def receive_reference_video(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("media_type") == "photo_edit":
        await message.answer("Для редактирования фото пришли изображение, а не видео.", reply_markup=back_to_model_kb())
        return
    await state.update_data(reference_file_id=message.video.file_id, reference_type="video")
    await _show_confirm_after_reference(message, state)


@router.message(MediaStates.enter_reference, F.document)
async def receive_reference_document(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mime = message.document.mime_type or ""
    if mime.startswith("video/"):
        if data.get("media_type") == "photo_edit":
            await message.answer("Для редактирования фото пришли изображение, а не видео.", reply_markup=back_to_model_kb())
            return
        await state.update_data(reference_file_id=message.document.file_id, reference_type="video")
        await _show_confirm_after_reference(message, state)
    elif mime.startswith("image/"):
        if data.get("media_type") == "video_edit":
            await message.answer("Для редактирования видео пришли видеофайл, а не фото.", reply_markup=back_to_model_kb())
            return
        await state.update_data(reference_file_id=message.document.file_id, reference_type="photo")
        await _show_confirm_after_reference(message, state)
    else:
        await message.answer(
            "Пожалуйста, пришли подходящий файл для редактирования.",
            reply_markup=back_to_model_kb(),
        )


@router.message(MediaStates.enter_reference, F.text, ~F.text.in_(MENU_BUTTONS))
async def enter_reference_text_input(message: Message, state: FSMContext) -> None:
    """Ввод номера фото для удаления/замены в режиме сбора ориентиров."""
    data = await state.get_data()
    managing = data.get("managing_style_ref")

    if data.get("adding_style_ref") and not managing:
        await message.answer("Текст не принимается в качестве ориентира — пришли фото 📎")
        return

    if not managing or not data.get("adding_style_ref"):
        await message.answer(
            "Пожалуйста, пришли фото или видео для редактирования.",
            reply_markup=back_to_model_kb(),
        )
        return

    srefs = list(data.get("style_reference_file_ids") or [])
    max_refs = data.get("model_max_style_refs", 14)

    try:
        idx = int(message.text.strip()) - 1
        if idx < 0 or idx >= len(srefs):
            await message.answer(f"Номер должен быть от 1 до {len(srefs)}.")
            return
    except ValueError:
        await message.answer(f"Введи число от 1 до {len(srefs)}.")
        return

    if managing == "delete":
        srefs.pop(idx)
        await state.update_data(style_reference_file_ids=srefs, managing_style_ref=None)
        text = (
            f"✅ Фото #{idx + 1} удалено. Осталось: {len(srefs)}/{max_refs}"
            if srefs else
            f"✅ Фото #{idx + 1} удалено. Список ориентиров пуст."
        )
        await _update_sref_status(
            message.bot, message.chat.id, state,
            text, style_ref_collecting_kb(len(srefs), max_refs),
        )
    elif managing == "replace":
        await state.update_data(managing_style_ref="replace_photo", managing_style_ref_index=idx)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"Пришли новое фото для замены #{idx + 1}:",
            style_ref_collecting_kb(len(srefs), max_refs),
        )


@router.message(MediaStates.enter_reference, ~F.text.in_(MENU_BUTTONS))
async def reference_wrong_type(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("adding_style_ref"):
        count = len(data.get("style_reference_file_ids") or [])
        max_refs = data.get("model_max_style_refs", 14)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            "Пожалуйста, пришли фото (изображение).",
            style_ref_collecting_kb(count, max_refs),
        )
    else:
        await message.answer(
            "Пожалуйста, пришли фото или видео для редактирования.",
            reply_markup=back_to_model_kb(),
        )


# ─── Ввод описания ───────────────────────────────────────────────────────────

async def _update_confirm_card(message: Message, state: FSMContext) -> None:
    """Обновляет карточку: редактирует существующее сообщение или отправляет новое."""
    data = await state.get_data()
    confirm_msg_id = data.get("confirm_msg_id")
    text = _confirm_card_text(data)
    kb = _confirm_kb(data)
    if confirm_msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=confirm_msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    sent = await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await _track_msg(state, sent.message_id)
    await state.update_data(confirm_msg_id=sent.message_id)


@router.message(MediaStates.enter_prompt, F.text, ~F.text.in_(MENU_BUTTONS))
async def receive_prompt(message: Message, state: FSMContext) -> None:
    await state.update_data(prompt=message.text)
    data = await state.get_data()

    if data.get("quick_edit"):
        # Быстрое редактирование сгенерированного фото — сразу запускаем без confirm-карточки
        # quick_edit НЕ сбрасываем, чтобы «Назад» после результата вернул в генерацию
        await state.set_state(MediaStates.confirm)
        await message.delete()
        waiting = await message.answer(
            "⏳ Редактирую, подожди немного...",
            reply_markup=gen_waiting_kb(),
        )
        try:
            await _start_tracked(message.from_user.id, message, message.from_user, state, data)
            await waiting.delete()
        except GenerationCancelledError:
            await _delete_msgs_below(message.bot, message.chat.id, state, waiting.message_id)
            await waiting.delete()
        except InsufficientCreditsError as e:
            await state.update_data(pending_retry_type="media")
            await waiting.edit_text(
                f"❌ {e}\n\nПополни баланс — редактирование продолжится автоматически:",
                reply_markup=quick_topup_kb(),
            )
        except RateLimitError as e:
            await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
        except ProviderContentPolicyError:
            await waiting.edit_text("❌ Запрос не прошёл проверку безопасности. Попробуй изменить описание.", reply_markup=error_kb())
        except ProviderError:
            await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
        except Exception:
            await waiting.edit_text("⚠️ Произошла непредвиденная ошибка. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
        return

    await state.set_state(MediaStates.confirm)
    await message.delete()
    await _update_confirm_card(message, state)


@router.message(MediaStates.enter_prompt, ~F.text.in_(MENU_BUTTONS))
async def enter_prompt_wrong_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    media_type = data.get("media_type", "")

    if message.photo and media_type == "image":
        await message.answer(
            "Этот раздел создаёт фото с нуля по текстовому описанию. "
            "Если хочешь изменить готовое фото — перейди в редактирование:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Перейти в редактирование фото", callback_data="media:switch_to_photo_edit")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm")],
            ]),
        )
    elif (message.video or message.video_note) and media_type == "video":
        await message.answer(
            "Этот раздел создаёт видео с нуля по описанию. "
            "Если хочешь изменить готовое видео — используй раздел «✏️ Редактировать видео».\n\n"
            + _PROMPT_HINTS["video"],
            reply_markup=back_to_confirm_kb(),
        )
    else:
        await message.answer(
            _PROMPT_HINTS.get(media_type, "Введи текстовое описание:"),
            reply_markup=back_to_confirm_kb(),
        )


# ─── Ввод/изменение описания прямо из confirm карточки ───────────────────────

@router.message(MediaStates.confirm, F.text, ~F.text.in_(MENU_BUTTONS))
async def update_prompt_in_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    if data.get("_is_generating"):
        await message.delete()
        asyncio.create_task(_notify_generating(message.bot, message.chat.id))
        return

    # Ввод кастомной длительности видео
    if data.get("entering_duration"):
        min_d = data.get("model_min_duration") or 1
        max_d = data.get("model_max_duration") or 60
        try:
            val = int(message.text.strip())
            if not (min_d <= val <= max_d):
                await message.answer(f"Введи целое число от {min_d} до {max_d} секунд:")
                return
            await state.update_data(duration=val, entering_duration=False)
            await message.delete()
            await _update_confirm_card(message, state)
        except ValueError:
            await message.answer(f"Нужно целое число от {min_d} до {max_d} секунд:")
        return

    # Повторное редактирование уже отредактированного фото
    if data.get("generated_file_id") and data.get("media_type") == "photo_edit":
        await state.update_data(prompt=message.text)
        await state.set_state(MediaStates.confirm)
        await message.delete()
        edit_data = await state.get_data()
        waiting = await message.answer(
            "⏳ Редактирую, подожди немного...",
            reply_markup=gen_waiting_kb(),
        )
        try:
            await _start_tracked(message.from_user.id, message, message.from_user, state, edit_data)
            await waiting.delete()
        except GenerationCancelledError:
            await _delete_msgs_below(message.bot, message.chat.id, state, waiting.message_id)
            await waiting.delete()
        except InsufficientCreditsError as e:
            await state.update_data(pending_retry_type="media")
            await waiting.edit_text(
                f"❌ {e}\n\nПополни баланс — редактирование продолжится автоматически:",
                reply_markup=quick_topup_kb(),
            )
        except RateLimitError as e:
            await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
        except ProviderContentPolicyError:
            await waiting.edit_text("❌ Запрос не прошёл проверку безопасности. Попробуй изменить описание.", reply_markup=error_kb())
        except ProviderError:
            await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
        except Exception:
            await waiting.edit_text("⚠️ Произошла непредвиденная ошибка. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
        return

    # После генерации фото — текст сразу запускает редактирование
    if data.get("generated_file_id") and data.get("media_type") == "image":
        edit_model = await _apply_edit_model_to_state(state, edit_prompt=message.text)
        if edit_model:
            await state.set_state(MediaStates.confirm)
            await message.delete()
            edit_data = await state.get_data()
            waiting = await message.answer(
                "⏳ Редактирую, подожди немного...",
                reply_markup=gen_waiting_kb(),
            )
            try:
                await _run_generation(message, message.from_user, state, edit_data)
                await waiting.delete()
            except InsufficientCreditsError as e:
                await state.update_data(pending_retry_type="media")
                await waiting.edit_text(
                    f"❌ {e}\n\nПополни баланс — редактирование продолжится автоматически:",
                    reply_markup=quick_topup_kb(),
                )
            except RateLimitError as e:
                await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
            except ProviderContentPolicyError:
                await waiting.edit_text("❌ Запрос не прошёл проверку безопасности. Попробуй изменить описание.", reply_markup=error_kb())
            except ProviderError:
                await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
            except Exception:
                await waiting.edit_text("⚠️ Произошла непредвиденная ошибка. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
            return
    await state.update_data(prompt=message.text)
    await message.delete()
    await _update_confirm_card(message, state)


@router.message(MediaStates.confirm, ~F.text)
async def confirm_unknown_input(message: Message, state: FSMContext) -> None:
    """Нетекстовый ввод в confirm state."""
    data = await state.get_data()
    media_type = data.get("media_type", "")

    if data.get("_is_generating"):
        await message.delete()
        asyncio.create_task(_notify_generating(message.bot, message.chat.id))
        return

    # Видео для редактирования
    if media_type == "video_edit" and message.video:
        await state.update_data(reference_file_id=message.video.file_id, reference_type="video")
        await _update_confirm_card(message, state)
        return
    if media_type == "photo_edit" and (message.video or message.video_note):
        await message.delete()
        await message.answer("Для редактирования фото пришли изображение, а не видео.")
        return
    if media_type == "video_edit" and message.photo:
        await message.delete()
        await message.answer("Для редактирования видео пришли видеофайл, а не фото.")
        return

    # Фото в confirm state
    if message.photo and media_type in ("image", "photo_edit"):
        photo = message.photo[-1]
        max_refs = data.get("model_max_style_refs", 0)

        # photo_edit: прямая отправка фото всегда заменяет основное редактируемое фото
        # (ориентиры добавляются только через кнопку «Добавить ориентир»)
        if media_type == "photo_edit":
            await state.update_data(reference_file_id=photo.file_id, reference_type="photo")
            await _update_confirm_card(message, state)
            return

        # image: если модель поддерживает ориентиры — добавляем туда.
        # Фото удаляем молча, никаких сообщений не показываем.
        if max_refs > 0:
            await message.delete()
            srefs = list(data.get("style_reference_file_ids") or [])
            if len(srefs) < max_refs:
                srefs.append(photo.file_id)
                await state.update_data(style_reference_file_ids=srefs)
            return

        # image без поддержки ориентиров → подсказываем про редактирование
        if media_type == "image":
            await message.delete()
            await message.answer(
                "Этот раздел создаёт фото с нуля по текстовому описанию. "
                "Если хочешь изменить готовое фото — перейди в редактирование:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Перейти в редактирование фото", callback_data="media:switch_to_photo_edit")],
                ]),
            )
            return

        # photo_edit, ориентиры не поддерживаются → заменяем основное фото
        await state.update_data(reference_file_id=photo.file_id, reference_type="photo")
        await _update_confirm_card(message, state)
        return
    elif (message.video or message.video_note) and media_type == "video":
        hint = (
            "Этот раздел создаёт видео с нуля по описанию. "
            "Если хочешь изменить готовое видео — используй раздел «✏️ Редактировать видео»."
        )
    else:
        hint = "Не понял запроса. Введи текстовое описание или воспользуйся кнопками."

    await message.delete()
    await message.answer(hint)
    sent = await message.answer(
        _confirm_card_text(data),
        parse_mode="HTML",
        reply_markup=_confirm_kb(data),
    )
    await state.update_data(confirm_msg_id=sent.message_id)


# ─── Фото с подписью — быстрый запуск редактирования ─────────────────────────

@router.message(F.photo, F.caption)
async def photo_with_caption_shortcut(message: Message, state: FSMContext) -> None:
    """Фото + подпись из любого контекста → редактирование фото без лишних шагов."""
    photo = message.photo[-1]
    caption = message.caption.strip()

    task_type = _TYPE_TO_TASK.get("photo_edit")
    models = get_models_for_task(task_type) if task_type else []

    if not models:
        await message.answer("⚠️ Редактирование фото временно недоступно.")
        return

    await state.set_data({
        "media_type": "photo_edit",
        "reference_file_id": photo.file_id,
        "reference_type": "photo",
        "prompt": caption,
    })

    if len(models) == 1:
        model_cfg = models[0]
        await state.update_data(
            model_slug=model_cfg["id"],
            model_label=model_cfg["label"],
            model_description=model_cfg.get("description", ""),
            model_variant_description=model_cfg.get("variant_description", ""),
            model_has_group=bool(model_cfg.get("group")),
            model_actual_id=model_cfg["model_id"],
            model_aspect_ratios=model_cfg.get("aspect_ratios", []),
            model_duration_options=model_cfg.get("duration_options", []),
            model_min_duration=None,
            model_max_duration=None,
        )
        await state.set_state(MediaStates.confirm)
        data = await state.get_data()
        await message.delete()
        sent = await message.answer(
            _confirm_card_text(data),
            parse_mode="HTML",
            reply_markup=_confirm_kb(data),
        )
        await state.update_data(confirm_msg_id=sent.message_id)
    else:
        await state.set_state(MediaStates.select_model)
        await message.delete()
        await message.answer(
            model_select_text("Редактирование фото", models),
            parse_mode="HTML",
            reply_markup=model_top_kb(models),
        )


# ─── Изменение параметров в карточке ─────────────────────────────────────────

@router.callback_query(MediaStates.confirm, F.data == "media:pick_ratio")
async def pick_ratio(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    allowed = data.get("model_aspect_ratios") or None
    await callback.message.edit_text(
        "<b>Выбери соотношение сторон:</b>",
        parse_mode="HTML",
        reply_markup=image_ratio_kb(data.get("aspect_ratio", "1:1"), allowed),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:pick_resolution")
async def pick_resolution(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.message.edit_text(
        "<b>Выбери качество:</b>",
        parse_mode="HTML",
        reply_markup=image_resolution_kb(data.get("resolution", "1K"), data.get("model_resolutions")),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:ratio:"))
async def set_ratio(callback: CallbackQuery, state: FSMContext) -> None:
    ratio = callback.data[len("media:ratio:"):]
    await state.update_data(aspect_ratio=ratio)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:resolution:"))
async def set_resolution(callback: CallbackQuery, state: FSMContext) -> None:
    resolution = callback.data.split(":")[2]
    await state.update_data(resolution=resolution)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:duration:"))
async def set_duration(callback: CallbackQuery, state: FSMContext) -> None:
    duration = int(callback.data.split(":")[2])
    await state.update_data(duration=duration, entering_duration=False)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:pick_duration")
async def pick_duration(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    options = data.get("model_duration_options") or [5, 10]
    min_d = data.get("model_min_duration") or min(options)
    max_d = data.get("model_max_duration") or max(options)
    current = data.get("duration", options[0])
    await callback.message.edit_text(
        "⏱ <b>Выбери длительность видео:</b>",
        parse_mode="HTML",
        reply_markup=video_duration_picker_kb(current, options, min_d, max_d),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:duration_custom")
async def duration_custom(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    min_d = data.get("model_min_duration") or 1
    max_d = data.get("model_max_duration") or 60
    await state.update_data(entering_duration=True)
    await callback.message.edit_text(
        f"Введи длительность в секундах ({min_d}–{max_d}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:pick_duration"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:edit_prompt")
async def edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(MediaStates.enter_prompt)
    await callback.message.edit_text(
        _PROMPT_HINTS.get(data.get("media_type", "edit"), "Введи новое описание:"),
        reply_markup=back_to_confirm_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "media:gen_why_long")
async def gen_why_long(callback: CallbackQuery) -> None:
    await callback.answer(
        "Нейросети иногда думают дольше обычного — это нормально.\n\n"
        "Результат придёт автоматически, как только будет готов. Просто подожди немного 🙏",
        show_alert=True,
    )


@router.callback_query(F.data == "media:cancel_generation")
async def cancel_generation(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    task = _active_tasks.get(user_id)
    elapsed = time.monotonic() - _task_start_times.get(user_id, 0)

    if task and not task.done():
        if elapsed < _CANCEL_WINDOW_SEC:
            task.cancel()
            await callback.answer("Отменяем...", show_alert=False)
        else:
            await callback.answer(
                "Уже загружаю результат к тебе в чат — подожди немного 🙏",
                show_alert=True,
            )
    else:
        await callback.answer("Генерация уже завершена", show_alert=False)


@router.callback_query(F.data == "media:show_prompt")
async def show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    prompt = (data.get("prompt") or "").strip()
    if prompt:
        # Telegram ограничивает текст алерта до 200 символов
        text = prompt if len(prompt) <= 200 else prompt[:197] + "..."
        await callback.answer(text, show_alert=True)
    else:
        await callback.answer("Описание не задано.", show_alert=True)


# ─── Запуск генерации ─────────────────────────────────────────────────────────

async def _tg_file_url(bot, file_id: str | None, bot_token: str) -> str | None:
    """Конвертирует Telegram file_id в прямой URL для передачи в провайдеры."""
    if not file_id:
        return None
    fi = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{bot_token}/{fi.file_path}"


async def _run_generation(send_msg: Message, tg_user, state: FSMContext, data: dict) -> None:
    """Выполняет генерацию и отправляет результат. Пробрасывает исключения наверх."""
    from config import config as _cfg
    media_type = data["media_type"]
    prompt = (data.get("prompt") or "").strip()
    model_slug = data.get("model_slug", "")

    _sref_ids = data.get("style_reference_file_ids") or []
    style_reference_urls = [
        u for u in [await _tg_file_url(send_msg.bot, fid, _cfg.bot_token) for fid in _sref_ids] if u
    ] or None

    if media_type == "image":
        result = await media_service.generate_image(
            tg_user.id, tg_user.username, prompt,
            data.get("aspect_ratio", "1:1"), data.get("resolution", "1K"), model_slug,
            style_reference_urls=style_reference_urls,
        )
        gen_nonce = data.get("_gen_nonce")
        if result.variants:
            all_images = [result.data] + result.variants
            gen_file_id = None
            for i, img_bytes in enumerate(all_images):
                f = BufferedInputFile(img_bytes, filename=f"image_{i+1}.png")
                is_last = (i == len(all_images) - 1)
                sent = await send_msg.answer_photo(
                    f,
                    reply_markup=after_generation_kb(is_image=True) if is_last else None,
                )
                await _track_msg(state, sent.message_id)
                if i == 0:
                    gen_file_id = sent.photo[-1].file_id
            current = await state.get_data()
            if current.get("_gen_nonce") == gen_nonce:
                await state.update_data(generated_file_id=gen_file_id)
        else:
            file = BufferedInputFile(result.data, filename=result.filename)
            sent = await send_msg.answer_photo(file, reply_markup=after_generation_kb(is_image=True))
            await _track_msg(state, sent.message_id)
            current = await state.get_data()
            if current.get("_gen_nonce") == gen_nonce:
                await state.update_data(generated_file_id=sent.photo[-1].file_id)

    elif media_type == "video":
        first_frame_url = await _tg_file_url(send_msg.bot, data.get("video_first_frame_file_id"), _cfg.bot_token)
        last_frame_url = await _tg_file_url(send_msg.bot, data.get("video_last_frame_file_id"), _cfg.bot_token)
        result = await media_service.generate_video(
            tg_user.id, tg_user.username, prompt, data.get("duration", 5), model_slug,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_video(file, reply_markup=after_generation_kb())
        await _track_msg(state, sent.message_id)

    elif media_type == "audio":
        result = await media_service.generate_audio(
            tg_user.id, tg_user.username, prompt, data.get("audio_type", "voice"), model_slug
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_audio(file, reply_markup=after_generation_kb())
        await _track_msg(state, sent.message_id)

    elif media_type == "photo_edit":
        file_info = await send_msg.bot.get_file(data["reference_file_id"])
        image_url = f"https://api.telegram.org/file/bot{_cfg.bot_token}/{file_info.file_path}"
        file_bytes = await send_msg.bot.download_file(file_info.file_path)
        media_bytes = file_bytes.read()
        result = await media_service.edit_image(
            tg_user.id, tg_user.username, media_bytes, prompt, model_slug,
            image_url=image_url, style_reference_urls=style_reference_urls,
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_photo(file, reply_markup=after_generation_kb(is_image=True))
        await _track_msg(state, sent.message_id)
        # Сохраняем file_id результата, чтобы пользователь мог сразу редактировать снова
        new_file_id = sent.photo[-1].file_id
        await state.update_data(generated_file_id=new_file_id, reference_file_id=new_file_id)

    elif media_type == "video_edit":
        file_info = await send_msg.bot.get_file(data["reference_file_id"])
        file_bytes = await send_msg.bot.download_file(file_info.file_path)
        media_bytes = file_bytes.read()
        result = await media_service.edit_video(
            tg_user.id, tg_user.username, media_bytes, prompt, model_slug
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_video(file, reply_markup=after_generation_kb())
        await _track_msg(state, sent.message_id)


@router.callback_query(MediaStates.confirm, F.data == "media:start")
async def start_generation(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    tg_user = callback.from_user
    media_type = data["media_type"]
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        await callback.answer("Сначала введи описание ✏️", show_alert=True)
        return

    if media_type in ("photo_edit", "video_edit") and not data.get("reference_file_id"):
        label = "фото" if media_type == "photo_edit" else "видео"
        await callback.answer(f"Сначала добавь {label} для редактирования 📎", show_alert=True)
        return

    await callback.message.edit_text(
        "⏳ Генерирую, подожди немного...",
        reply_markup=gen_waiting_kb(),
    )
    await callback.answer()

    gen_nonce = uuid.uuid4().hex
    await state.update_data(_is_generating=True, _gen_nonce=gen_nonce)
    data = {**data, "_gen_nonce": gen_nonce}
    try:
        await _start_tracked(tg_user.id, callback.message, tg_user, state, data)
        await callback.message.delete()
        # state не очищаем — данные нужны для кнопки "Назад"

    except GenerationCancelledError:
        # Удаляем всё, что успело появиться ниже waiting-сообщения, и восстанавливаем карточку
        await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
        data = await state.get_data()
        await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    except InsufficientCreditsError as e:
        await state.update_data(pending_retry_type="media")
        await callback.message.edit_text(
            f"❌ {e}\n\nПополни баланс — генерация продолжится автоматически:",
            reply_markup=quick_topup_kb(),
        )
    except RateLimitError as e:
        await callback.message.edit_text(f"⏱ {e}", reply_markup=error_kb())
    except ProviderContentPolicyError:
        await callback.message.edit_text(
            "❌ Запрос не прошёл проверку безопасности. Попробуй изменить описание.",
            reply_markup=error_kb(),
        )
    except ProviderError as e:
        logger.error("ProviderError in start_generation: %s", e)
        await callback.message.edit_text(
            "⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.",
            reply_markup=error_kb(),
        )
    except Exception:
        logger.exception("Unexpected error in start_generation")
        await callback.message.edit_text(
            "⚠️ Произошла непредвиденная ошибка. Кредиты не списаны — попробуй ещё раз.",
            reply_markup=error_kb(),
        )
    finally:
        await state.update_data(_is_generating=None, _gen_nonce=None)


async def resume_generation_after_topup(message: Message, state: FSMContext) -> None:
    """Вызывается из billing после успешной оплаты — возобновляет медиа-генерацию."""
    data = await state.get_data()
    waiting = await message.answer(
        "⏳ Генерирую, подожди немного...",
        reply_markup=gen_waiting_kb(),
    )
    try:
        await _start_tracked(message.from_user.id, message, message.from_user, state, data)
        await waiting.delete()
    except GenerationCancelledError:
        await waiting.edit_text("❌ Генерация отменена. Кредиты не списаны.", reply_markup=error_kb())
    except InsufficientCreditsError as e:
        await waiting.edit_text(f"❌ {e}\n\nПополни баланс в разделе «Мой баланс».")
    except RateLimitError as e:
        await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
    except ProviderContentPolicyError:
        await waiting.edit_text("❌ Запрос не прошёл проверку безопасности. Попробуй изменить описание.", reply_markup=error_kb())
    except ProviderError:
        await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
    except Exception:
        await waiting.edit_text("⚠️ Произошла непредвиденная ошибка. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
