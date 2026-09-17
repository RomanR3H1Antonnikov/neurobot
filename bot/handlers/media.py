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
from bot.utils import cleanup_tracked_messages, safe_delete
from bot.keyboards.billing import quick_topup_kb
from config import config as _billing_cfg


def _has_yookassa() -> bool:
    return bool(_billing_cfg.yookassa_provider_token)
from bot.keyboards.media import (
    media_type_kb, media_edit_kb, media_info_kb, model_top_kb, model_variant_kb,
    model_select_text, model_variant_text, back_to_model_kb, back_to_confirm_kb, back_to_frames_kb,
    image_confirm_kb, image_ratio_kb, image_resolution_kb,
    video_confirm_kb, video_format_kb, video_frames_menu_kb, video_duration_picker_kb, audio_confirm_kb, edit_confirm_kb,
    music_confirm_kb, music_format_kb,
    udio_confirm_kb, udio_lyrics_type_kb, udio_model_type_kb,
    voice_picker_kb,
    style_ref_collecting_kb, after_generation_kb, gen_waiting_kb, error_kb,
)
from providers.base import ProviderError, ProviderContentPolicyError, TaskType
from providers.router import get_models_for_task
from services import media_service
from services.media_service import InsufficientCreditsError, RateLimitError
from db.queries import save_generation

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


async def _back_to_frames_menu(bot, chat_id: int, state: FSMContext) -> None:
    """После добавления кадра редактирует hint-сообщение обратно в меню кадров."""
    await state.set_state(MediaStates.confirm)
    data = await state.get_data()
    motion_control = bool(data.get("model_motion_control"))
    if data.get("video_frames_mode") == "constructor":
        constructor_video = data.get("model_constructor_video", False)
        max_video_refs = data.get("model_max_video_refs", 0) if constructor_video else 0
        title = (
            "🎬 <b>Конструктор видео</b>\n\nДобавь ориентиры для генерации:"
            if constructor_video else
            "🎬 <b>Конструктор видео</b>\n\nДобавь ориентиры для генерации:"
        )
        frames_kb = video_frames_menu_kb(
            extra_ref_count=len(data.get("style_reference_file_ids") or []),
            max_extra_refs=data.get("model_max_style_refs", 0),
            show_first_frame=False,
            show_last_frame=False,
            show_video_refs=constructor_video,
            max_video_refs=max_video_refs,
            video_ref_count=len(data.get("video_style_reference_file_ids") or []),
        )
    else:
        max_extra_refs = data.get("model_max_style_refs", 0) if not motion_control else 0
        title = (
            "📎 <b>Motion Control</b>\n\nДобавь фото (начальный кадр) и видео (задаёт характер движения):"
            if motion_control else
            "📎 <b>Кадры видео</b>\n\nДобавь фото для первого и/или последнего кадра:"
        )
        frames_kb = video_frames_menu_kb(
            has_first_frame=bool(data.get("video_first_frame_file_id")),
            has_last_frame=bool(data.get("video_last_frame_file_id")),
            motion_control=motion_control,
            extra_ref_count=len(data.get("style_reference_file_ids") or []),
            max_extra_refs=max_extra_refs,
            show_first_frame=data.get("model_has_first_frame", True),
            show_last_frame=data.get("model_has_last_frame", True),
        )
    msg_id = data.get("_sref_msg_id")
    if msg_id:
        try:
            await bot.edit_message_text(
                title, chat_id=chat_id, message_id=msg_id,
                parse_mode="HTML", reply_markup=frames_kb,
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, title, parse_mode="HTML", reply_markup=frames_kb)
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
        _v_ratio = data.get("aspect_ratio")
        _v_res = data.get("resolution")
        if data.get("model_aspect_ratios") and data.get("model_resolutions") and _v_ratio and _v_res:
            lines.append(f"<b>Масштаб:</b> {_v_ratio}  |  <b>Качество:</b> {_v_res}")
        elif data.get("model_aspect_ratios") and _v_ratio:
            lines.append(f"<b>Масштаб:</b> {_v_ratio}")
        elif data.get("model_resolutions") and _v_res:
            lines.append(f"<b>Качество:</b> {_v_res}")
        _srefs = data.get("style_reference_file_ids") or []
        if _srefs:
            lines.append(f"<b>Доп. кадры:</b> {len(_srefs)} фото ✅")
        _audio_refs = data.get("audio_reference_file_ids") or []
        if _audio_refs:
            lines.append(f"<b>Аудио:</b> {len(_audio_refs)} файл(а) ✅")
        _video_refs = data.get("video_style_reference_file_ids") or []
        if _video_refs:
            lines.append(f"<b>Видео-реф:</b> {len(_video_refs)} файл(а) ✅")
    elif media_type == "audio":
        t = "Озвучка" if data.get("audio_type", "voice") == "voice" else "Музыка"
        lines.append(f"<b>Тип аудио:</b> {t}")
        if data.get("selected_voice_label"):
            lines.append(f"<b>Голос:</b> {data['selected_voice_label']}")
        if data.get("has_music_settings"):
            lines.append(f"<b>Длительность:</b> {data.get('music_duration', 30)} сек")
            if data.get("music_show_advanced"):
                pos_styles = data.get("music_positive_styles") or []
                neg_styles = data.get("music_negative_styles") or []
                sections = data.get("music_sections") or []
                if pos_styles:
                    _ps = ", ".join(pos_styles[:3]) + ("..." if len(pos_styles) > 3 else "")
                    lines.append(f"<b>Стили (+):</b> {_ps}")
                if neg_styles:
                    _ns = ", ".join(neg_styles[:3]) + ("..." if len(neg_styles) > 3 else "")
                    lines.append(f"<b>Стили (-):</b> {_ns}")
                if sections:
                    lines.append(f"<b>Секции:</b> {len(sections)} шт.")
                if data.get("music_instrumental"):
                    lines.append("<b>Только инструментал:</b> Да")
                if data.get("music_respect_durations"):
                    lines.append("<b>Длит. секций:</b> Соблюдать")
                lines.append(f"<b>Формат:</b> {data.get('music_format', 'mp3_44100_128')}")
        if data.get("has_udio_settings") and data.get("udio_show_advanced"):
            lt = data.get("udio_lyrics_type", "generate")
            lt_label = {"generate": "Авто", "custom": "Свои тексты", "instrumental": "Инструментал"}.get(lt, lt)
            if data.get("udio_translate_input"):
                lines.append("<b>Перевод ввода:</b> Да")
            lines.append(f"<b>Тип лирики:</b> {lt_label}")
            if lt == "custom" and data.get("udio_lyrics"):
                lyr = data["udio_lyrics"]
                lines.append(f"<b>Текст песни:</b> <code>{lyr[:60]}{'...' if len(lyr) > 60 else ''}</code>")
            lines.append(
                f"<b>Сила промпта:</b> {data.get('udio_prompt_strength', 0.5):.2f}  |  "
                f"<b>Сила лирики:</b> {data.get('udio_lyrics_strength', 0.5):.2f}"
            )
            lines.append(
                f"<b>Качество:</b> {data.get('udio_generation_quality', 0.75):.2f}  |  "
                f"<b>Чёткость:</b> {data.get('udio_clarity_strength', 0.25):.2f}"
            )
            lines.append(
                f"<b>Лирика:</b> {data.get('udio_lyrics_placement_start', 0.2):.2f} – "
                f"{data.get('udio_lyrics_placement_end', 0.9):.2f}"
            )
            lines.append(f"<b>Модель:</b> {data.get('udio_model_type', 'udio130-v1.5')}")
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
    prompt_display = f"<code>{prompt}</code>" if data.get("prompt") else "не задано"
    lines.append(f"<b>{prompt_label}:</b> {prompt_display}")
    return "\n".join(lines)


def _get_generation_cost(data: dict) -> int | None:
    from providers.router import get_provider_by_model_id
    from providers.base import TaskType
    _task_map = {
        "image": TaskType.IMAGE_GENERATION,
        "video": TaskType.VIDEO_GENERATION,
        "audio": TaskType.AUDIO_GENERATION,
        "photo_edit": TaskType.IMAGE_EDIT,
        "video_edit": TaskType.VIDEO_EDIT,
    }
    task_type = _task_map.get(data.get("media_type"))
    model_slug = data.get("model_slug")
    if not task_type or not model_slug:
        return None
    try:
        _, model_cfg = get_provider_by_model_id(task_type, model_slug)
        return media_service.get_cost(model_cfg, data.get("resolution"), data.get("duration"))
    except Exception:
        return None


def _confirm_kb(data: dict):
    cost = _get_generation_cost(data)
    has_prompt = bool(data.get("prompt"))
    style_ref_count = len(data.get("style_reference_file_ids") or [])
    media_type = data.get("media_type")
    if media_type == "image":
        return image_confirm_kb(
            data.get("aspect_ratio", "1:1"), data.get("resolution", "1K"),
            has_prompt=has_prompt, style_ref_count=style_ref_count,
            max_style_refs=data.get("model_max_style_refs", 14),
            cost_credits=cost,
        )
    elif media_type == "video":
        _show_first = data.get("model_has_first_frame", True)
        _show_last = data.get("model_has_last_frame", True)
        _max_extra = data.get("model_max_style_refs", 0)
        _max_video = data.get("model_max_video_refs", 0)
        _show_animate = bool(_show_first or _show_last)
        _show_constructor = bool(_max_extra > 0)
        _show_frames = bool(_show_animate or _show_constructor)
        return video_confirm_kb(
            data.get("duration", 5),
            has_prompt=has_prompt,
            duration_options=data.get("model_duration_options"),
            has_first_frame=bool(data.get("video_first_frame_file_id")),
            has_last_frame=bool(data.get("video_last_frame_file_id")),
            has_extra_refs=bool(data.get("style_reference_file_ids")),
            aspect_ratio=data.get("aspect_ratio"),
            has_aspect_ratios=bool(data.get("model_aspect_ratios")),
            resolution=data.get("resolution"),
            has_resolutions=bool(data.get("model_resolutions")),
            audio_ref_count=len(data.get("audio_reference_file_ids") or []),
            max_audio_refs=data.get("model_max_audio_refs", 0),
            video_ref_count=len(data.get("video_style_reference_file_ids") or []),
            max_video_refs=_max_video,
            show_frames_button=_show_frames,
            show_first_frame_btn=_show_animate,
            show_constructor_btn=_show_constructor,
            frames_mode=data.get("video_frames_mode"),
            output_format=data.get("video_output_format"),
            output_formats=data.get("model_output_formats"),
            audio_enabled=data.get("video_audio_enabled", True),
            cost_credits=cost,
        )
    elif media_type == "audio":
        if data.get("has_music_settings"):
            return music_confirm_kb(data, cost_credits=cost)
        if data.get("has_udio_settings"):
            return udio_confirm_kb(data, cost_credits=cost)
        voice_label = data.get("selected_voice_label") if data.get("model_voices") else None
        return audio_confirm_kb(has_prompt=has_prompt, cost_credits=cost, voice_label=voice_label)
    elif media_type in ("photo_edit", "video_edit"):
        return edit_confirm_kb(
            has_prompt=has_prompt,
            has_reference=bool(data.get("reference_file_id")),
            media_type=media_type,
            style_ref_count=style_ref_count,
            cost_credits=cost,
        )


# ─── Вход в раздел ───────────────────────────────────────────────────────────

MEDIA_MENU_TEXT = (
    "✨ <b>Генерация</b> — создать новый контент с нуля\n"
    "✏️ <b>Редактирование</b> — улучшение качества, замена лиц и т.д.\n\n"
    "Выбери действие:"
)

_INFO_COLLAPSED = (
    "💡 <b>Краткий гайд по работе с ИИ-генерацией</b>\n\n"
    "Чтобы получить качественный результат — начни с детального промпта: опиши объект, "
    "действие, освещение, ракурс и движение камеры. Вместо «красиво» — конкретика: "
    "«кофейная кружка на столе, тёплый утренний свет, камера медленно приближается, "
    "кинематографический стиль»..."
)

_INFO_FULL = (
    "💡 <b>Гайд по работе с ИИ-генерацией</b>\n\n"
    "<b>1. Пиши детальные промпты</b>\n"
    "Описывай объект, действие, освещение, ракурс, стиль и движение камеры. Вместо «красиво» — "
    "«кофейная кружка на деревянном столе, тёплый утренний свет из окна, камера медленно "
    "приближается, кинематографический стиль, 16:9».\n\n"
    "<b>2. Разбивай сложные сцены</b>\n"
    "Не пытайся сгенерировать всё за раз — разбивай на короткие фрагменты и склеивай "
    "в редакторе, так избежишь артефактов и потери логики между кадрами.\n\n"
    "<b>3. Используй референсы и негативные указания</b>\n"
    "Добавляй фото-ориентиры нужного стиля и исключения в промпт: "
    "«без лишних пальцев», «без текста на заднем плане».\n\n"
    "<b>4. Прописывай движение</b>\n"
    "Если не указать как движется камера или объект — сцена получится статичной.\n\n"
    "<b>5. Делай итерации</b>\n"
    "Не жди идеального результата с первого раза — меняй по одному параметру и перегенерируй.\n\n"
    "<b>6. Дорабатывай результат</b>\n"
    "После генерации улучшай качество, добавляй звук, цветокоррекцию или титры.\n\n"
    "<i>ИИ — это инструмент, не финал. Всегда проверяй результат и используй в рамках правил сервиса.</i>"
)


@router.message(F.text == BTN_MEDIA)
async def media_menu(message: Message, state: FSMContext) -> None:
    logger.info("[NAV] media_menu called, chat=%s user=%s", message.chat.id, message.from_user.id)
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await safe_delete(message, "BTN_MEDIA")
    await state.set_state(MediaStates.select_type)
    sent = await message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())
    await state.update_data(_tracked_msg_ids=[sent.message_id])
    logger.info("[NAV] media_menu sent msg_id=%s", sent.message_id)


# ─── Выбор типа ──────────────────────────────────────────────────────────────

@router.callback_query(MediaStates.select_type, F.data == "media:edit_menu")
async def edit_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    await callback.message.edit_text(
        "Выбери, что нужно изменить:",
        reply_markup=media_edit_kb(),
    )
    await callback.answer()


@router.callback_query(MediaStates.select_type, F.data == "media:info")
async def media_info(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_INFO_COLLAPSED, parse_mode="HTML", reply_markup=media_info_kb(expanded=False))
    await callback.answer()


@router.callback_query(MediaStates.select_type, F.data == "media:info:expand")
async def media_info_expand(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_INFO_FULL, parse_mode="HTML", reply_markup=media_info_kb(expanded=True))
    await callback.answer()


@router.callback_query(MediaStates.select_type, F.data.startswith("media:type:"))
async def select_type(callback: CallbackQuery, state: FSMContext) -> None:
    media_type = callback.data.split(":")[2]
    defaults = {
        "image": {"aspect_ratio": "1:1", "resolution": "1K"},
        "video": {"duration": 5},
        "audio": {"audio_type": "voice"},
    }
    # Удаляем сообщения ниже текущего (верхнего) прежде чем переходить дальше.
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    # Сбрасываем данные предыдущего раздела, сохраняя обновлённый трекинг.
    _curr = await state.get_data()
    await state.set_data({
        "_tracked_msg_ids": _curr.get("_tracked_msg_ids"),
        "media_type": media_type,
        **defaults.get(media_type, {}),
    })

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
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
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
        "model_max_audio_refs": model_cfg.get("max_audio_refs", 0),
        "model_max_video_refs": model_cfg.get("max_video_refs", 0),
        "model_resolutions": model_cfg.get("resolutions"),
        "model_motion_control": model_cfg.get("motion_control", False),
        "model_has_first_frame": model_cfg.get("first_frame", True),
        "model_has_last_frame": model_cfg.get("last_frame", True),
        "model_output_formats": model_cfg.get("output_formats"),
        "model_constructor_video": model_cfg.get("constructor_includes_video", False),
        "video_audio_enabled": True,
        "video_frames_mode": None,
    }
    # если output_format не задан или недоступен у новой модели — сбрасываем
    allowed_formats = model_cfg.get("output_formats")
    if allowed_formats:
        current_fmt = data.get("video_output_format")
        if not current_fmt or current_fmt not in allowed_formats:
            update["video_output_format"] = allowed_formats[0]
    else:
        update["video_output_format"] = None
    # если текущий ratio недоступен у новой модели (или не задан) — сбрасываем на первый из списка
    if aspect_ratios:
        current_ratio = data.get("aspect_ratio")
        if not current_ratio or current_ratio not in aspect_ratios:
            update["aspect_ratio"] = aspect_ratios[0]
    # если текущее разрешение недоступно у новой модели (или не задано) — сбрасываем на первое
    allowed_res = model_cfg.get("resolutions")
    if allowed_res:
        current_res = data.get("resolution")
        if media_type == "video" or not current_res or current_res not in allowed_res:
            update["resolution"] = allowed_res[0]
    # для видео — всегда ставим минимальную длительность (самый дешёвый дефолт)
    if media_type == "video" and duration_options:
        update["duration"] = duration_options[0]
    # для аудио — тип (voice/music) берём из конфига модели
    if media_type == "audio":
        update["audio_type"] = model_cfg.get("audio_type", "voice")
        update["has_music_settings"] = bool(model_cfg.get("has_music_settings"))
        update["has_udio_settings"] = bool(model_cfg.get("has_udio_settings"))
        if model_cfg.get("has_music_settings"):
            update["music_duration"] = 30
            update["music_positive_styles"] = []
            update["music_negative_styles"] = []
            update["music_sections"] = []
            update["music_instrumental"] = False
            update["music_respect_durations"] = False
            update["music_format"] = "mp3_44100_128"
            update["music_show_advanced"] = False
            update["entering_music_duration"] = False
            update["entering_music_pos_styles"] = False
            update["entering_music_neg_styles"] = False
            update["entering_music_sections"] = False
        if model_cfg.get("has_udio_settings"):
            update["udio_show_advanced"] = False
            update["udio_translate_input"] = False
            update["udio_lyrics"] = ""
            update["udio_lyrics_type"] = "generate"
            update["udio_prompt_strength"] = 0.5
            update["udio_lyrics_strength"] = 0.5
            update["udio_generation_quality"] = 0.75
            update["udio_model_type"] = "udio130-v1.5"
            update["udio_lyrics_placement_start"] = 0.2
            update["udio_lyrics_placement_end"] = 0.9
            update["udio_clarity_strength"] = 0.25
            update["entering_udio_float"] = None
            update["entering_udio_lyrics"] = False
        # Голоса для озвучки (elevenlabs-v3 и подобные)
        voices = model_cfg.get("voices") or []
        update["model_voices"] = voices if voices else None
        if voices:
            update["selected_voice_id"] = voices[0]["id"]
            update["selected_voice_label"] = voices[0]["label"]
        else:
            update["selected_voice_id"] = None
            update["selected_voice_label"] = None

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
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
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
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
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

    # Если есть снапшот генерации — возвращаем к карточке генерации
    # (quick_edit = явное нажатие «Редактировать», _gen_snapshot без quick_edit = авто-редактирование)
    if data.get("quick_edit") or data.get("_gen_snapshot"):
        logger.info(
            "back_to_model: quick_edit=%s, has_snapshot=%s, media_type=%s",
            bool(data.get("quick_edit")), bool(data.get("_gen_snapshot")), data.get("media_type"),
        )
        if await _restore_gen_snapshot(callback, state):
            return
        if data.get("quick_edit"):
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
        # _gen_snapshot был, но восстановление не удалось — переходим к обычной логике

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

    # Если модель входит в группу — сначала возвращаем к списку вариантов группы,
    # а не сразу к полному списку моделей
    model_slug = data.get("model_slug")
    if model_slug and data.get("model_has_group"):
        model_cfg_cur = next((m for m in models if m["id"] == model_slug), None)
        group_id = model_cfg_cur.get("group") if model_cfg_cur else None
        if group_id:
            variants = [m for m in models if m.get("group") == group_id]
            if variants:
                group_label = variants[0].get("group_label", group_id)
                description = variants[0].get("description", "")
                # Сбрасываем данные модели — при повторном нажатии "Назад"
                # из списка вариантов model_has_group уже будет None → пойдём к полному списку
                await state.update_data(
                    model_slug=None, model_label=None, model_description=None,
                    model_variant_description=None, model_has_group=None,
                    model_actual_id=None, model_aspect_ratios=None,
                    model_duration_options=None, model_min_duration=None,
                    model_max_duration=None, model_max_style_refs=None,
                    model_resolutions=None, model_motion_control=None,
                    entering_duration=None, confirm_msg_id=None,
                    prompt=None, reference_file_id=None, reference_type=None,
                    generated_file_id=None, style_reference_file_ids=None,
                    adding_style_ref=None, video_first_frame_file_id=None,
                    video_last_frame_file_id=None,
                )
                await state.set_state(MediaStates.select_model)
                try:
                    await callback.message.edit_text(
                        model_variant_text(group_label, description),
                        parse_mode="HTML",
                        reply_markup=model_variant_kb(variants),
                    )
                    await _track_msg(state, callback.message.message_id)
                except Exception:
                    try:
                        await callback.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                    sent = await callback.message.answer(
                        model_variant_text(group_label, description),
                        parse_mode="HTML",
                        reply_markup=model_variant_kb(variants),
                    )
                    await _track_msg(state, sent.message_id)
                await callback.answer()
                return

    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, 0)
    await state.update_data(
        quick_edit=None,
        _gen_snapshot=None,
        _gen_nonce=None,
        kie_gen_task_id=None,
        prompt=None, model_slug=None, model_label=None,
        model_description=None, model_variant_description=None,
        model_has_group=None, model_aspect_ratios=None,
        model_duration_options=None, model_min_duration=None, model_max_duration=None,
        model_max_style_refs=None, model_resolutions=None, model_motion_control=None,
        entering_duration=None, confirm_msg_id=None,
        reference_file_id=None, reference_type=None,
        generated_file_id=None,
        style_reference_file_ids=None, adding_style_ref=None, adding_video_extra_frame=None,
        audio_reference_file_ids=None, adding_audio_ref=None, model_max_audio_refs=None,
        video_style_reference_file_ids=None, adding_video_ref=None, model_max_video_refs=None,
        model_has_first_frame=None, model_has_last_frame=None,
        managing_style_ref=None, managing_style_ref_index=None,
        video_first_frame_file_id=None, video_last_frame_file_id=None,
        video_frames_expanded=None,
    )
    await state.set_state(MediaStates.select_model)
    text = model_select_text(type_label, models)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=model_top_kb(models))
        await _track_msg(state, callback.message.message_id)
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
    _curr = await state.get_data()
    await state.set_data({"_tracked_msg_ids": _curr.get("_tracked_msg_ids"), "media_type": "photo_edit"})
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
    await cleanup_tracked_messages(callback.bot, callback.message.chat.id, state)
    await callback.message.delete()
    await state.clear()
    await state.set_state(MediaStates.select_type)
    sent = await callback.message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())
    await state.update_data(_tracked_msg_ids=[sent.message_id])


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
        await state.update_data(prompt=None, _is_generating=None, _cleanup_warned_msg_id=None)
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
        audio_reference_file_ids=None,
        generated_file_id=None,
        kie_gen_task_id=None,
        video_first_frame_file_id=None, video_last_frame_file_id=None,
        video_frames_mode=None,
        confirm_msg_id=None,
        quick_edit=None,
        _gen_snapshot=None,
        adding_style_ref=None,
        adding_video_extra_frame=None,
        adding_audio_ref=None,
        adding_video_ref=None,
        video_style_reference_file_ids=None,
        managing_style_ref=None, managing_style_ref_index=None,
        video_frames_expanded=None,
        entering_duration=None,
        entering_music_duration=False, entering_music_pos_styles=False,
        entering_music_neg_styles=False, entering_music_sections=False,
        entering_udio_float=None, entering_udio_lyrics=False,
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
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
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


@router.callback_query(MediaStates.confirm, F.data == "media:animate_photo")
async def animate_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Оживить фото' — первый и/или последний кадр."""
    data = await state.get_data()

    has_constructor_files = bool(data.get("style_reference_file_ids")) or bool(data.get("video_style_reference_file_ids"))
    if has_constructor_files and data.get("video_frames_mode") != "animate":
        if data.get("confirm_mode_switch") != "animate":
            await state.update_data(confirm_mode_switch="animate")
            await callback.answer(
                "⚠️ «Оживить фото» и «Конструктор видео» — взаимоисключающие разделы.\n"
                "Файлы из конструктора будут удалены. Нажми ещё раз для подтверждения.",
                show_alert=True,
            )
            return
        await state.update_data(
            style_reference_file_ids=None,
            video_style_reference_file_ids=None,
            confirm_mode_switch=None,
        )
    else:
        await state.update_data(confirm_mode_switch=None)

    await state.update_data(video_frames_mode="animate")
    data = await state.get_data()
    motion_control = bool(data.get("model_motion_control"))
    show_first = data.get("model_has_first_frame", True)
    show_last = data.get("model_has_last_frame", True)
    title = (
        "📎 <b>Motion Control</b>\n\nДобавь фото (начальный кадр) и видео (задаёт характер движения):"
        if motion_control else
        "🖼 <b>Оживить фото</b>\n\nДобавь первый и/или последний кадр:"
    )
    await callback.message.edit_text(
        title,
        parse_mode="HTML",
        reply_markup=video_frames_menu_kb(
            has_first_frame=bool(data.get("video_first_frame_file_id")),
            has_last_frame=bool(data.get("video_last_frame_file_id")),
            motion_control=motion_control,
            max_extra_refs=0,
            show_first_frame=show_first,
            show_last_frame=show_last,
        ),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:toggle_audio")
async def toggle_audio(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка переключения звука в видео."""
    data = await state.get_data()
    new_audio = not data.get("video_audio_enabled", True)
    await state.update_data(video_audio_enabled=new_audio)
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:toggle_frames")
async def toggle_frames(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Конструктор видео' — референсные фото и/или видео."""
    data = await state.get_data()

    has_animate_files = bool(data.get("video_first_frame_file_id")) or bool(data.get("video_last_frame_file_id"))
    if has_animate_files and data.get("video_frames_mode") != "constructor":
        if data.get("confirm_mode_switch") != "constructor":
            await state.update_data(confirm_mode_switch="constructor")
            await callback.answer(
                "⚠️ «Конструктор видео» и «Оживить фото» — взаимоисключающие разделы.\n"
                "Файлы из «Оживить фото» будут удалены. Нажми ещё раз для подтверждения.",
                show_alert=True,
            )
            return
        await state.update_data(
            video_first_frame_file_id=None,
            video_last_frame_file_id=None,
            confirm_mode_switch=None,
        )
    else:
        await state.update_data(confirm_mode_switch=None)

    await state.update_data(video_frames_mode="constructor")
    data = await state.get_data()
    max_extra_refs = data.get("model_max_style_refs", 0)
    extra_ref_count = len(data.get("style_reference_file_ids") or [])
    constructor_video = data.get("model_constructor_video", False)
    max_video_refs = data.get("model_max_video_refs", 0) if constructor_video else 0
    title = (
        "🎬 <b>Конструктор видео</b>\n\nДобавь ориентиры для генерации:"
        if constructor_video else
        "🎬 <b>Конструктор видео</b>\n\nДобавь ориентиры для генерации:"
    )
    await callback.message.edit_text(
        title,
        parse_mode="HTML",
        reply_markup=video_frames_menu_kb(
            extra_ref_count=extra_ref_count,
            max_extra_refs=max_extra_refs,
            show_first_frame=False,
            show_last_frame=False,
            show_video_refs=constructor_video,
            max_video_refs=max_video_refs,
            video_ref_count=len(data.get("video_style_reference_file_ids") or []),
        ),
    )
    await callback.answer()



@router.callback_query(MediaStates.confirm, F.data == "media:add_extra_frames")
async def add_extra_frames(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Остальные кадры' — принимает фото без отдельного раздела управления."""
    data = await state.get_data()
    count = len(data.get("style_reference_file_ids") or [])
    max_refs = data.get("model_max_style_refs", 0)
    hint = (
        f"📎 Добавлено {count} фото. Отправь ещё (до {max_refs} всего). Когда закончишь — нажми «Назад»."
        if count else
        f"📎 Отправь фото — оно добавится как дополнительный кадр. Можно добавить до {max_refs} штук.\nКогда закончишь — нажми «Назад»."
    )
    await state.update_data(adding_video_extra_frame=True, _sref_msg_id=callback.message.message_id)
    await callback.message.edit_text(hint, reply_markup=back_to_frames_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_audio_ref")
async def add_audio_ref(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Аудио' на карточке видео-генерации."""
    data = await state.get_data()
    if not data.get("video_audio_enabled", True):
        await callback.answer(
            "Аудиореференсы недоступны при отключённом звуке.\nВключи «Со звуком», чтобы загрузить аудио.",
            show_alert=True,
        )
        return
    count = len(data.get("audio_reference_file_ids") or [])
    max_refs = data.get("model_max_audio_refs", 0)
    hint = (
        f"🎵 Добавлено {count} аудио. Отправь ещё (до {max_refs} всего). Когда закончишь — нажми «Назад»."
        if count else
        f"🎵 Отправь аудиофайл — он добавится как звуковой референс для видео. Можно добавить до {max_refs} файлов.\nКогда закончишь — нажми «Назад»."
    )
    await state.update_data(adding_audio_ref=True, _sref_msg_id=callback.message.message_id)
    await callback.message.edit_text(hint, reply_markup=back_to_confirm_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_video_ref")
async def add_video_ref(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка '🎬 Видео' на карточке видео-генерации."""
    data = await state.get_data()
    count = len(data.get("video_style_reference_file_ids") or [])
    max_refs = data.get("model_max_video_refs", 0)
    hint = (
        f"🎬 Добавлено {count} видео. Отправь ещё (до {max_refs} всего). Когда закончишь — нажми «Назад»."
        if count else
        f"🎬 Отправь видеофайл — он добавится как видеореференс. Можно добавить до {max_refs} файлов.\nКогда закончишь — нажми «Назад»."
    )
    await state.update_data(adding_video_ref=True, _sref_msg_id=callback.message.message_id)
    await callback.message.edit_text(hint, reply_markup=back_to_frames_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_first_frame")
async def add_first_frame(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Первый кадр' / 'Фото' на карточке видео-генерации."""
    await state.update_data(adding_video_frame="first", _sref_msg_id=callback.message.message_id, video_frames_mode="animate")
    data = await state.get_data()
    has = bool(data.get("video_first_frame_file_id"))
    if bool(data.get("model_motion_control")):
        hint = "📎 Отправь другое фото (начальный кадр):" if has else "📎 Отправь фото — оно станет начальным кадром для Motion Control:"
    else:
        hint = "📎 Отправь другое фото для начала видео:" if has else "📎 Отправь фото — оно будет первым кадром (начало видео):"
    await callback.message.edit_text(hint, reply_markup=back_to_frames_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_last_frame")
async def add_last_frame(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Конец видео' / 'Видео' на карточке видео-генерации."""
    await state.update_data(adding_video_frame="last", _sref_msg_id=callback.message.message_id)
    data = await state.get_data()
    has = bool(data.get("video_last_frame_file_id"))
    if bool(data.get("model_motion_control")):
        hint = "📎 Отправь другое видео (движение):" if has else "📎 Отправь видео — оно задаст характер движения для Motion Control:"
    else:
        hint = "📎 Отправь другое фото для конца видео:" if has else "📎 Отправь фото — оно будет последним кадром (конец видео):"
    await callback.message.edit_text(hint, reply_markup=back_to_frames_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(F.data == "media:back:confirm")
async def back_to_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к карточке настроек — работает после генерации и из enter_reference."""
    data = await state.get_data()

    # В режиме быстрого редактирования «Назад» возвращает к карточке генерации
    if data.get("quick_edit") and await _restore_gen_snapshot(callback, state):
        return

    await state.update_data(
        adding_style_ref=None, adding_video_extra_frame=None, adding_audio_ref=None,
        adding_video_ref=None,
        managing_style_ref=None, managing_style_ref_index=None,
        entering_duration=None,
        entering_music_duration=False, entering_music_pos_styles=False,
        entering_music_neg_styles=False, entering_music_sections=False,
        entering_udio_float=None, entering_udio_lyrics=False,
        confirm_mode_switch=None,
    )
    await state.set_state(MediaStates.confirm)
    data = await state.get_data()
    text = _confirm_card_text(data)
    kb = _confirm_kb(data)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await state.update_data(confirm_msg_id=callback.message.message_id)
        await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        sent = await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await _track_msg(state, sent.message_id)
        await state.update_data(confirm_msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "media:back:frames")
async def back_to_frames(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в меню кадров из hint-сообщения (когда нажали 'Начало/Конец видео')."""
    await state.update_data(adding_video_frame=None)
    await state.set_state(MediaStates.confirm)
    await _back_to_frames_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:delete_first_frame")
async def delete_first_frame(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(video_first_frame_file_id=None, _sref_msg_id=callback.message.message_id)
    await _back_to_frames_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer("Фото удалено")


@router.callback_query(MediaStates.confirm, F.data == "media:delete_last_frame")
async def delete_last_frame(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(video_last_frame_file_id=None, _sref_msg_id=callback.message.message_id)
    await _back_to_frames_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer("Фото удалено")


@router.callback_query(MediaStates.confirm, F.data == "media:delete_extra_frames")
async def delete_extra_frames(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(style_reference_file_ids=[], _sref_msg_id=callback.message.message_id)
    await _back_to_frames_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer("Кадры удалены")


@router.callback_query(MediaStates.confirm, F.data == "media:delete_video_refs")
async def delete_video_refs(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(video_style_reference_file_ids=[], _sref_msg_id=callback.message.message_id)
    await _back_to_frames_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer("Видео удалены")


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
        sent = await message.answer("Для редактирования видео пришли видеофайл, а не фото.", reply_markup=back_to_model_kb())
        await _track_msg(state, sent.message_id)
        return
    photo = message.photo[-1]
    if data.get("adding_video_extra_frame"):
        await message.delete()
        srefs = list(data.get("style_reference_file_ids") or [])
        max_refs = data.get("model_max_style_refs", 0)
        if len(srefs) < max_refs:
            srefs.append(photo.file_id)
        await state.update_data(style_reference_file_ids=srefs)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"✅ Добавлено! Кадров: {len(srefs)}/{max_refs}. Отправь ещё или нажми «Назад».",
            back_to_frames_kb(),
        )
        return
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
        await message.delete()
        await _back_to_frames_menu(message.bot, message.chat.id, state)
        return
    await message.delete()
    _ref_upd: dict = {"reference_file_id": photo.file_id, "reference_type": "photo"}
    if (message.caption or "").strip():
        _ref_upd["prompt"] = message.caption.strip()
    await state.update_data(**_ref_upd)
    await _show_confirm_after_reference(message, state)


@router.message(MediaStates.enter_reference, F.video)
async def receive_reference_video(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("media_type") == "photo_edit":
        sent = await message.answer("Для редактирования фото пришли изображение, а не видео.", reply_markup=back_to_model_kb())
        await _track_msg(state, sent.message_id)
        return
    # Видео-референс для генерации видео
    if data.get("adding_video_ref"):
        await message.delete()
        video_refs = list(data.get("video_style_reference_file_ids") or [])
        max_refs = data.get("model_max_video_refs", 0)
        if len(video_refs) < max_refs:
            video_refs.append(message.video.file_id)
        await state.update_data(video_style_reference_file_ids=video_refs)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"✅ Видео добавлено! Всего: {len(video_refs)}/{max_refs}. Отправь ещё или нажми «Назад».",
            back_to_frames_kb(),
        )
        return
    # Motion control: принимаем видео в слот последнего кадра
    if (data.get("media_type") == "video"
            and data.get("adding_video_frame") == "last"
            and data.get("model_motion_control")):
        await message.delete()
        await state.update_data(video_last_frame_file_id=message.video.file_id, adding_video_frame=None)
        await _back_to_frames_menu(message.bot, message.chat.id, state)
        return
    await message.delete()
    await state.update_data(reference_file_id=message.video.file_id, reference_type="video")
    await _show_confirm_after_reference(message, state)


@router.message(MediaStates.enter_reference, F.audio | F.voice)
async def receive_reference_audio(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("adding_audio_ref"):
        sent = await message.answer(
            "Аудиофайл принимается только при добавлении аудио-референсов. "
            "Нажми кнопку «🎵 Аудио» на карточке.",
            reply_markup=back_to_confirm_kb(),
        )
        await _track_msg(state, sent.message_id)
        return
    await message.delete()
    file_id = message.audio.file_id if message.audio else message.voice.file_id
    audio_refs = list(data.get("audio_reference_file_ids") or [])
    max_refs = data.get("model_max_audio_refs", 0)
    if len(audio_refs) < max_refs:
        audio_refs.append(file_id)
    await state.update_data(audio_reference_file_ids=audio_refs)
    await _update_sref_status(
        message.bot, message.chat.id, state,
        f"✅ Аудио добавлено! Всего: {len(audio_refs)}/{max_refs}. Отправь ещё или нажми «Назад».",
        back_to_confirm_kb(),
    )


@router.message(MediaStates.enter_reference, F.document)
async def receive_reference_document(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mime = message.document.mime_type or ""
    if mime.startswith("video/"):
        if data.get("media_type") == "photo_edit":
            sent = await message.answer("Для редактирования фото пришли изображение, а не видео.", reply_markup=back_to_model_kb())
            await _track_msg(state, sent.message_id)
            return
        await message.delete()
        await state.update_data(reference_file_id=message.document.file_id, reference_type="video")
        await _show_confirm_after_reference(message, state)
    elif mime.startswith("image/"):
        if data.get("media_type") == "video_edit":
            sent = await message.answer("Для редактирования видео пришли видеофайл, а не фото.", reply_markup=back_to_model_kb())
            await _track_msg(state, sent.message_id)
            return
        await message.delete()
        await state.update_data(reference_file_id=message.document.file_id, reference_type="photo")
        await _show_confirm_after_reference(message, state)
    else:
        sent = await message.answer(
            "Пожалуйста, пришли подходящий файл для редактирования.",
            reply_markup=back_to_model_kb(),
        )
        await _track_msg(state, sent.message_id)


@router.message(MediaStates.enter_reference, F.text, ~F.text.in_(MENU_BUTTONS))
async def enter_reference_text_input(message: Message, state: FSMContext) -> None:
    """Ввод номера фото для удаления/замены в режиме сбора ориентиров."""
    data = await state.get_data()
    managing = data.get("managing_style_ref")

    if data.get("adding_style_ref") and not managing:
        sent = await message.answer("Текст не принимается в качестве ориентира — пришли фото 📎")
        await _track_msg(state, sent.message_id)
        return

    if not managing or not data.get("adding_style_ref"):
        sent = await message.answer(
            "Пожалуйста, пришли фото или видео для редактирования.",
            reply_markup=back_to_model_kb(),
        )
        await _track_msg(state, sent.message_id)
        return

    srefs = list(data.get("style_reference_file_ids") or [])
    max_refs = data.get("model_max_style_refs", 14)

    try:
        idx = int(message.text.strip()) - 1
        if idx < 0 or idx >= len(srefs):
            sent = await message.answer(f"Номер должен быть от 1 до {len(srefs)}.")
            await _track_msg(state, sent.message_id)
            return
    except ValueError:
        sent = await message.answer(f"Введи число от 1 до {len(srefs)}.")
        await _track_msg(state, sent.message_id)
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
    if data.get("adding_video_ref"):
        count = len(data.get("video_style_reference_file_ids") or [])
        max_refs = data.get("model_max_video_refs", 0)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"Пожалуйста, пришли видеофайл. Добавлено: {count}/{max_refs}.",
            back_to_confirm_kb(),
        )
        return
    if data.get("adding_audio_ref"):
        count = len(data.get("audio_reference_file_ids") or [])
        max_refs = data.get("model_max_audio_refs", 0)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"Пожалуйста, пришли аудиофайл. Добавлено: {count}/{max_refs}.",
            back_to_confirm_kb(),
        )
        return
    if data.get("adding_video_extra_frame"):
        count = len(data.get("style_reference_file_ids") or [])
        max_refs = data.get("model_max_style_refs", 0)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            f"Пожалуйста, пришли фото. Кадров: {count}/{max_refs}.",
            back_to_confirm_kb(),
        )
        return
    if data.get("adding_style_ref"):
        count = len(data.get("style_reference_file_ids") or [])
        max_refs = data.get("model_max_style_refs", 14)
        await _update_sref_status(
            message.bot, message.chat.id, state,
            "Пожалуйста, пришли фото (изображение).",
            style_ref_collecting_kb(count, max_refs),
        )
    else:
        sent = await message.answer(
            "Пожалуйста, пришли фото или видео для редактирования.",
            reply_markup=back_to_model_kb(),
        )
        await _track_msg(state, sent.message_id)


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
            await _delete_msgs_below(message.bot, message.chat.id, state, confirm_msg_id)
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
                reply_markup=quick_topup_kb(_has_yookassa()),
            )
        except RateLimitError as e:
            await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
        except ProviderContentPolicyError:
            await waiting.edit_text("❌ Запрос не прошёл проверку безопасности — попробуй изменить описание или использовать другое изображение.", reply_markup=error_kb())
        except ProviderError:
            await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
        except Exception:
            await waiting.edit_text("⚠️ Произошла непредвиденная ошибка при отправке результата. Если рубли были списаны — обратись в поддержку.", reply_markup=error_kb())
        return

    await state.set_state(MediaStates.confirm)
    await message.delete()
    await _update_confirm_card(message, state)


@router.message(MediaStates.enter_prompt, ~F.text.in_(MENU_BUTTONS))
async def enter_prompt_wrong_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    media_type = data.get("media_type", "")

    await message.delete()
    if message.photo and media_type == "image":
        sent = await message.answer(
            "Этот раздел создаёт фото с нуля по текстовому описанию. "
            "Если хочешь изменить готовое фото — перейди в редактирование:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Перейти в редактирование фото", callback_data="media:switch_to_photo_edit")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm")],
            ]),
        )
    elif (message.video or message.video_note) and media_type == "video":
        sent = await message.answer(
            "Этот раздел создаёт видео с нуля по описанию. "
            "Если хочешь изменить готовое видео — используй раздел «✏️ Редактировать видео».\n\n"
            + _PROMPT_HINTS["video"],
            reply_markup=back_to_confirm_kb(),
        )
    else:
        sent = await message.answer(
            _PROMPT_HINTS.get(media_type, "Введи текстовое описание:"),
            reply_markup=back_to_confirm_kb(),
        )
    await _track_msg(state, sent.message_id)


# ─── Ввод/изменение описания прямо из confirm карточки ───────────────────────

@router.message(MediaStates.confirm, F.text, ~F.text.in_(MENU_BUTTONS))
async def update_prompt_in_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    if data.get("_is_generating"):
        await message.delete()
        asyncio.create_task(_notify_generating(message.bot, message.chat.id))
        return

    # Ввод настроек ElevenLabs Music
    if data.get("entering_music_duration"):
        try:
            val = int(message.text.strip())
            if not (5 <= val <= 600):
                sent = await message.answer("Введи целое число от 5 до 600 секунд:")
                await _track_msg(state, sent.message_id)
                return
            await state.update_data(music_duration=val, entering_music_duration=False)
            await message.delete()
            await _update_confirm_card(message, state)
        except ValueError:
            sent = await message.answer("Нужно целое число (секунды, от 5 до 600):")
            await _track_msg(state, sent.message_id)
        return

    if data.get("entering_music_pos_styles"):
        raw = message.text.strip()
        styles = [] if raw.lower() in ("нет", "очистить", "-") else [
            s.strip() for s in raw.replace(";", ",").split(",") if s.strip()
        ]
        await state.update_data(music_positive_styles=styles, entering_music_pos_styles=False)
        await message.delete()
        await _update_confirm_card(message, state)
        return

    if data.get("entering_music_neg_styles"):
        raw = message.text.strip()
        styles = [] if raw.lower() in ("нет", "очистить", "-") else [
            s.strip() for s in raw.replace(";", ",").split(",") if s.strip()
        ]
        await state.update_data(music_negative_styles=styles, entering_music_neg_styles=False)
        await message.delete()
        await _update_confirm_card(message, state)
        return

    if data.get("entering_music_sections"):
        raw = message.text.strip()
        if raw.lower() in ("нет", "очистить", "-"):
            sections: list[str] = []
        else:
            # поддерживаем разделители: новая строка, точка с запятой
            sections = [s.strip() for s in raw.replace(";", "\n").split("\n") if s.strip()]
        await state.update_data(music_sections=sections, entering_music_sections=False)
        await message.delete()
        await _update_confirm_card(message, state)
        return

    # Ввод float-параметров Udio
    if data.get("entering_udio_float"):
        field = data["entering_udio_float"]
        try:
            val = float(message.text.strip().replace(",", "."))
            if not (0.0 <= val <= 1.0):
                sent = await message.answer("Введи число от 0.0 до 1.0:")
                await _track_msg(state, sent.message_id)
                return
            await state.update_data(**{f"udio_{field}": round(val, 2), "entering_udio_float": None})
            await message.delete()
            await _update_confirm_card(message, state)
        except ValueError:
            sent = await message.answer("Нужно число от 0.0 до 1.0 (например: 0.5):")
            await _track_msg(state, sent.message_id)
        return

    if data.get("entering_udio_lyrics"):
        lyrics = message.text.strip()
        if lyrics.lower() in ("нет", "очистить", "-"):
            lyrics = ""
        await state.update_data(udio_lyrics=lyrics, entering_udio_lyrics=False)
        await message.delete()
        await _update_confirm_card(message, state)
        return

    # Ввод кастомной длительности видео
    if data.get("entering_duration"):
        min_d = data.get("model_min_duration") or 1
        max_d = data.get("model_max_duration") or 60
        try:
            val = int(message.text.strip())
            if not (min_d <= val <= max_d):
                sent = await message.answer(f"Введи целое число от {min_d} до {max_d} секунд:")
                await _track_msg(state, sent.message_id)
                return
            await state.update_data(duration=val, entering_duration=False)
            await message.delete()
            await _update_confirm_card(message, state)
        except ValueError:
            sent = await message.answer(f"Нужно целое число от {min_d} до {max_d} секунд:")
            await _track_msg(state, sent.message_id)
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
                reply_markup=quick_topup_kb(_has_yookassa()),
            )
        except RateLimitError as e:
            await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
        except ProviderContentPolicyError:
            await waiting.edit_text("❌ Запрос не прошёл проверку безопасности — попробуй изменить описание или использовать другое изображение.", reply_markup=error_kb())
        except ProviderError:
            await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
        except Exception:
            await waiting.edit_text("⚠️ Произошла непредвиденная ошибка при отправке результата. Если рубли были списаны — обратись в поддержку.", reply_markup=error_kb())
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
                    reply_markup=quick_topup_kb(_has_yookassa()),
                )
            except RateLimitError as e:
                await waiting.edit_text(f"⏱ {e}", reply_markup=error_kb())
            except ProviderContentPolicyError:
                await waiting.edit_text("❌ Запрос не прошёл проверку безопасности — попробуй изменить описание или использовать другое изображение.", reply_markup=error_kb())
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
        await message.delete()
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
    if message.photo and media_type in ("image", "photo_edit", "video"):
        photo = message.photo[-1]
        max_refs = data.get("model_max_style_refs", 0)
        # Подпись к фото → используем как промпт в любом режиме
        _caption = (message.caption or "").strip()

        # photo_edit: прямая отправка фото всегда заменяет основное редактируемое фото
        # (ориентиры добавляются только через кнопку «Добавить ориентир»)
        if media_type == "photo_edit":
            await message.delete()
            _upd: dict = {"reference_file_id": photo.file_id, "reference_type": "photo"}
            if _caption:
                _upd["prompt"] = _caption
            await state.update_data(**_upd)
            await _update_confirm_card(message, state)
            return

        # video: последовательное авто-добавление кадров
        # 1-е фото → начало видео (если есть), 2-е → конец видео (если есть), остальные → доп. кадры
        if media_type == "video":
            await message.delete()
            has_first_support = data.get("model_has_first_frame", True)
            has_last_support = data.get("model_has_last_frame", True)
            if has_first_support and not data.get("video_first_frame_file_id"):
                await state.update_data(video_first_frame_file_id=photo.file_id)
            elif has_last_support and not data.get("video_last_frame_file_id"):
                await state.update_data(video_last_frame_file_id=photo.file_id)
            elif max_refs > 0:
                srefs = list(data.get("style_reference_file_ids") or [])
                if len(srefs) < max_refs:
                    srefs.append(photo.file_id)
                await state.update_data(style_reference_file_ids=srefs)
            if _caption:
                await state.update_data(prompt=_caption)
            await _update_confirm_card(message, state)
            return

        # image: если модель поддерживает ориентиры — добавляем туда и обновляем карточку.
        if max_refs > 0:
            await message.delete()
            srefs = list(data.get("style_reference_file_ids") or [])
            if len(srefs) < max_refs:
                srefs.append(photo.file_id)
            _upd = {"style_reference_file_ids": srefs}
            if _caption:
                _upd["prompt"] = _caption
            await state.update_data(**_upd)
            await _update_confirm_card(message, state)
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
        await message.delete()
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
    sent_hint = await message.answer(hint)
    await _track_msg(state, sent_hint.message_id)


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

    _curr = await state.get_data()
    await state.set_data({
        "_tracked_msg_ids": _curr.get("_tracked_msg_ids"),
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
    resolutions = data.get("model_resolutions") or []
    if len(resolutions) <= 1:
        await callback.answer()
        return
    await callback.message.edit_text(
        "<b>Выбери качество:</b>",
        parse_mode="HTML",
        reply_markup=image_resolution_kb(data.get("resolution", "1K"), resolutions),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:ratio:"))
async def set_ratio(callback: CallbackQuery, state: FSMContext) -> None:
    ratio = callback.data[len("media:ratio:"):]
    await state.update_data(aspect_ratio=ratio)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:resolution:"))
async def set_resolution(callback: CallbackQuery, state: FSMContext) -> None:
    resolution = callback.data.split(":")[2]
    await state.update_data(resolution=resolution)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:pick_format")
async def pick_format(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    formats = data.get("model_output_formats") or ["mp4"]
    current = data.get("video_output_format") or formats[0]
    await callback.message.edit_text(
        "<b>Выбери формат видео:</b>\n\n"
        "MP4 — универсальный формат, подходит для большинства платформ.\n"
        "MOV — высокое цветовое качество для профессионального монтажа.",
        parse_mode="HTML",
        reply_markup=video_format_kb(current, formats),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:format:"))
async def set_format(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data[len("media:format:"):]
    await state.update_data(video_output_format=fmt)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:duration:"))
async def set_duration(callback: CallbackQuery, state: FSMContext) -> None:
    duration = int(callback.data.split(":")[2])
    await state.update_data(duration=duration, entering_duration=False)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await _delete_msgs_below(callback.bot, callback.message.chat.id, state, callback.message.message_id)
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:pick_duration")
async def pick_duration(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(entering_duration=None)
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


# ─── ElevenLabs Music: дополнительные настройки ─────────────────────────────

@router.callback_query(MediaStates.confirm, F.data == "media:music_adv")
async def toggle_music_advanced(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(music_show_advanced=not bool(data.get("music_show_advanced")))
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:music_duration")
async def enter_music_duration(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(entering_music_duration=True)
    await callback.message.edit_text(
        "⏱ Введи длительность трека в секундах (5–600):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:music_pos_styles")
async def enter_music_pos_styles(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("music_positive_styles") or []
    hint = f"Текущие: <code>{', '.join(current)}</code>\n\n" if current else ""
    await state.update_data(entering_music_pos_styles=True)
    await callback.message.edit_text(
        f"{hint}🎼 Введи позитивные стили через запятую (например: <code>jazz, upbeat, piano</code>).\n"
        "Чтобы очистить — напиши «нет».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:music_neg_styles")
async def enter_music_neg_styles(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("music_negative_styles") or []
    hint = f"Текущие: <code>{', '.join(current)}</code>\n\n" if current else ""
    await state.update_data(entering_music_neg_styles=True)
    await callback.message.edit_text(
        f"{hint}🚫 Введи негативные стили через запятую (то, чего не должно быть в треке).\n"
        "Чтобы очистить — напиши «нет».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:music_sections")
async def enter_music_sections(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("music_sections") or []
    if current:
        hint = "Текущие секции:\n" + "\n".join(f"  • {s}" for s in current) + "\n\n"
    else:
        hint = ""
    await state.update_data(entering_music_sections=True)
    await callback.message.edit_text(
        f"{hint}📋 Введи секции — каждая с новой строки или через «;»\n"
        "(например: <code>Вступление\nКуплет с гитарой\nПрипев</code>).\n"
        "Чтобы очистить — напиши «нет».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:music_instrumental")
async def toggle_music_instrumental(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(music_instrumental=not bool(data.get("music_instrumental")))
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:music_respect_dur")
async def toggle_music_respect_dur(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(music_respect_durations=not bool(data.get("music_respect_durations")))
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:pick_music_format")
async def pick_music_format(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("music_format", "mp3_44100_128")
    await callback.message.edit_text(
        "📁 <b>Выбери формат аудио:</b>",
        parse_mode="HTML",
        reply_markup=music_format_kb(current),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:music_format:"))
async def set_music_format(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data[len("media:music_format:"):]
    await state.update_data(music_format=fmt)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


# ─── Udio: дополнительные настройки ─────────────────────────────────────────

_UDIO_FLOAT_DEFAULTS = {
    "prompt_strength": 0.5,
    "lyrics_strength": 0.5,
    "generation_quality": 0.75,
    "clarity_strength": 0.25,
    "lyrics_placement_start": 0.2,
    "lyrics_placement_end": 0.9,
}
_UDIO_FLOAT_NAMES = {
    "prompt_strength": "Сила промпта",
    "lyrics_strength": "Сила лирики",
    "generation_quality": "Качество генерации",
    "clarity_strength": "Приоритет чёткости",
    "lyrics_placement_start": "Начало размещения лирики",
    "lyrics_placement_end": "Конец размещения лирики",
}


@router.callback_query(MediaStates.confirm, F.data == "media:udio_adv")
async def toggle_udio_advanced(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(udio_show_advanced=not bool(data.get("udio_show_advanced")))
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:udio_translate")
async def toggle_udio_translate(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(udio_translate_input=not bool(data.get("udio_translate_input")))
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:udio_lyrics_type")
async def pick_udio_lyrics_type(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("udio_lyrics_type", "generate")
    await callback.message.edit_text(
        "🎤 <b>Выбери тип лирики:</b>",
        parse_mode="HTML",
        reply_markup=udio_lyrics_type_kb(current),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:udio_set_lyrics_type:"))
async def set_udio_lyrics_type(callback: CallbackQuery, state: FSMContext) -> None:
    lt = callback.data[len("media:udio_set_lyrics_type:"):]
    await state.update_data(udio_lyrics_type=lt)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:udio_lyrics")
async def enter_udio_lyrics(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("udio_lyrics") or ""
    hint = f"Текущий текст:\n<code>{current[:200]}</code>\n\n" if current else ""
    await state.update_data(entering_udio_lyrics=True)
    await callback.message.edit_text(
        f"{hint}📝 Введи текст песни (или «нет» чтобы очистить):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:udio_float:"))
async def enter_udio_float(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data[len("media:udio_float:"):]
    if field not in _UDIO_FLOAT_DEFAULTS:
        await callback.answer()
        return
    default = _UDIO_FLOAT_DEFAULTS[field]
    name = _UDIO_FLOAT_NAMES.get(field, field)
    current = (await state.get_data()).get(f"udio_{field}", default)
    await state.update_data(entering_udio_float=field)
    await callback.message.edit_text(
        f"<b>{name}</b>\nТекущее: {current:.2f}\n\nВведи число от 0.0 до 1.0:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        ]]),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:udio_model_type")
async def pick_udio_model_type(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("udio_model_type", "udio130-v1.5")
    await callback.message.edit_text(
        "🎛 <b>Выбери версию модели Udio:</b>",
        parse_mode="HTML",
        reply_markup=udio_model_type_kb(current),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:udio_set_model:"))
async def set_udio_model_type(callback: CallbackQuery, state: FSMContext) -> None:
    mt = callback.data[len("media:udio_set_model:"):]
    await state.update_data(udio_model_type=mt)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


# ─── Выбор голоса для озвучки ────────────────────────────────────────────────

@router.callback_query(MediaStates.confirm, F.data == "media:pick_voice")
async def pick_voice(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    voices = data.get("model_voices") or []
    current_id = data.get("selected_voice_id", "")
    await callback.message.edit_text(
        "🗣 <b>Выбери голос озвучки:</b>",
        parse_mode="HTML",
        reply_markup=voice_picker_kb(voices, current_id),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data.startswith("media:set_voice:"))
async def set_voice(callback: CallbackQuery, state: FSMContext) -> None:
    voice_id = callback.data[len("media:set_voice:"):]
    data = await state.get_data()
    voices = data.get("model_voices") or []
    voice = next((v for v in voices if v["id"] == voice_id), None)
    if voice:
        await state.update_data(selected_voice_id=voice_id, selected_voice_label=voice["label"])
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
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
    from providers.kie import set_pending_job_ctx
    media_type = data["media_type"]
    prompt = (data.get("prompt") or "").strip()
    model_slug = data.get("model_slug", "")

    # Сохраняем контекст в ContextVar — KIE provider запишет его в БД при создании job,
    # чтобы orphaned callback (после таймаута или перезапуска) мог доставить результат
    _storage_type = {"image": "photo", "photo_edit": "photo", "video_edit": "video"}.get(media_type, media_type)
    set_pending_job_ctx(
        telegram_id=tg_user.id,
        chat_id=send_msg.chat.id,
        model_label=data.get("model_label", ""),
        media_type=_storage_type,
        prompt=prompt or None,
    )

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
                fid = sent.photo[-1].file_id
                await save_generation(tg_user.id, "photo", fid, prompt=prompt, model_label=data.get("model_label"))
                if i == 0:
                    gen_file_id = fid
            current = await state.get_data()
            if current.get("_gen_nonce") == gen_nonce:
                await state.update_data(generated_file_id=gen_file_id,
                                        kie_gen_task_id=result.provider_image_url)
        else:
            file = BufferedInputFile(result.data, filename=result.filename)
            sent = await send_msg.answer_photo(file, reply_markup=after_generation_kb(is_image=True))
            await _track_msg(state, sent.message_id)
            await save_generation(tg_user.id, "photo", sent.photo[-1].file_id, prompt=prompt, model_label=data.get("model_label"))
            current = await state.get_data()
            if current.get("_gen_nonce") == gen_nonce:
                await state.update_data(generated_file_id=sent.photo[-1].file_id,
                                        kie_gen_task_id=result.provider_image_url)

    elif media_type == "video":
        first_frame_url = await _tg_file_url(send_msg.bot, data.get("video_first_frame_file_id"), _cfg.bot_token)
        last_frame_url = await _tg_file_url(send_msg.bot, data.get("video_last_frame_file_id"), _cfg.bot_token)
        _audio_ids = data.get("audio_reference_file_ids") or []
        audio_reference_urls = [
            u for u in [await _tg_file_url(send_msg.bot, fid, _cfg.bot_token) for fid in _audio_ids] if u
        ] or None
        _video_ref_ids = data.get("video_style_reference_file_ids") or []
        video_reference_urls = [
            u for u in [await _tg_file_url(send_msg.bot, fid, _cfg.bot_token) for fid in _video_ref_ids] if u
        ] or None
        result = await media_service.generate_video(
            tg_user.id, tg_user.username, prompt, data.get("duration", 5), model_slug,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            style_reference_urls=style_reference_urls,
            aspect_ratio=data.get("aspect_ratio"),
            resolution=data.get("resolution"),
            audio_reference_urls=audio_reference_urls,
            video_reference_urls=video_reference_urls,
            output_format=data.get("video_output_format"),
            audio=data.get("video_audio_enabled", True),
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        if result.mime_type == "video/quicktime":
            sent = await send_msg.answer_document(file, reply_markup=after_generation_kb())
            await _track_msg(state, sent.message_id)
            await save_generation(tg_user.id, "video", sent.document.file_id, prompt=prompt, model_label=data.get("model_label"))
        else:
            sent = await send_msg.answer_video(file, reply_markup=after_generation_kb())
            await _track_msg(state, sent.message_id)
            await save_generation(tg_user.id, "video", sent.video.file_id, prompt=prompt, model_label=data.get("model_label"))

    elif media_type == "audio":
        music_params: dict | None = None
        if data.get("has_music_settings"):
            music_params = {
                "_provider_model": "elevenlabs-music",
                "music_duration": data.get("music_duration", 30),
                "music_positive_styles": data.get("music_positive_styles") or [],
                "music_negative_styles": data.get("music_negative_styles") or [],
                "music_sections": data.get("music_sections") or [],
                "music_instrumental": bool(data.get("music_instrumental")),
                "music_respect_durations": bool(data.get("music_respect_durations")),
                "music_format": data.get("music_format", "mp3_44100_128"),
            }
        elif data.get("model_voices"):
            music_params = {
                "_provider_model": "elevenlabs-v3",
                "voice_id": data.get("selected_voice_id", "EkK5I93UQWFDigLMpZcX"),
            }
        elif data.get("has_udio_settings"):
            music_params = {
                "_provider_model": "udio",
                "translate_input": bool(data.get("udio_translate_input")),
                "lyrics": data.get("udio_lyrics") or None,
                "lyrics_type": data.get("udio_lyrics_type", "generate"),
                "prompt_strength": data.get("udio_prompt_strength", 0.5),
                "lyrics_strength": data.get("udio_lyrics_strength", 0.5),
                "generation_quality": data.get("udio_generation_quality", 0.75),
                "model_type": data.get("udio_model_type", "udio130-v1.5"),
                "lyrics_placement_start": data.get("udio_lyrics_placement_start", 0.2),
                "lyrics_placement_end": data.get("udio_lyrics_placement_end", 0.9),
                "clarity_strength": data.get("udio_clarity_strength", 0.25),
            }
        result = await media_service.generate_audio(
            tg_user.id, tg_user.username, prompt, data.get("audio_type", "voice"), model_slug,
            music_params=music_params,
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_audio(file, reply_markup=after_generation_kb())
        await _track_msg(state, sent.message_id)
        await save_generation(tg_user.id, "audio", sent.audio.file_id, prompt=prompt, model_label=data.get("model_label"))

    elif media_type == "photo_edit":
        file_info = await send_msg.bot.get_file(data["reference_file_id"])
        image_url = f"https://api.telegram.org/file/bot{_cfg.bot_token}/{file_info.file_path}"
        file_bytes = await send_msg.bot.download_file(file_info.file_path)
        media_bytes = file_bytes.read()
        result = await media_service.edit_image(
            tg_user.id, tg_user.username, media_bytes, prompt, model_slug,
            image_url=image_url, style_reference_urls=style_reference_urls,
            provider_task_id=data.get("kie_gen_task_id"),
            resolution=data.get("resolution"),
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_photo(file, reply_markup=after_generation_kb(is_image=True))
        await _track_msg(state, sent.message_id)
        new_file_id = sent.photo[-1].file_id
        await save_generation(tg_user.id, "photo", new_file_id, prompt=prompt, model_label=data.get("model_label"))
        # Сохраняем file_id результата, чтобы пользователь мог сразу редактировать снова
        await state.update_data(generated_file_id=new_file_id, reference_file_id=new_file_id)

    elif media_type == "video_edit":
        file_info = await send_msg.bot.get_file(data["reference_file_id"])
        video_url = f"https://api.telegram.org/file/bot{_cfg.bot_token}/{file_info.file_path}"
        file_bytes = await send_msg.bot.download_file(file_info.file_path)
        media_bytes = file_bytes.read()
        result = await media_service.edit_video(
            tg_user.id, tg_user.username, media_bytes, prompt, model_slug,
            video_url=video_url,
        )
        file = BufferedInputFile(result.data, filename=result.filename)
        sent = await send_msg.answer_video(file, reply_markup=after_generation_kb())
        await _track_msg(state, sent.message_id)
        await save_generation(tg_user.id, "video", sent.video.file_id, prompt=prompt, model_label=data.get("model_label"))


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
            reply_markup=quick_topup_kb(_has_yookassa()),
        )
    except RateLimitError as e:
        await callback.message.edit_text(f"⏱ {e}", reply_markup=error_kb())
    except ProviderContentPolicyError:
        await callback.message.edit_text(
            "❌ Запрос не прошёл проверку безопасности — попробуй изменить описание или использовать другое изображение.",
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
            "⚠️ Произошла непредвиденная ошибка при отправке результата. Если рубли были списаны — обратись в поддержку.",
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
        await waiting.edit_text("❌ Запрос не прошёл проверку безопасности — попробуй изменить описание или использовать другое изображение.", reply_markup=error_kb())
    except ProviderError:
        await waiting.edit_text("⚠️ Сервис временно недоступен. Кредиты не списаны — попробуй ещё раз.", reply_markup=error_kb())
    except Exception:
        await waiting.edit_text("⚠️ Произошла непредвиденная ошибка при отправке результата. Если рубли были списаны — обратись в поддержку.", reply_markup=error_kb())
