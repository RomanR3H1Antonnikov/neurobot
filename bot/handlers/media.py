import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_MEDIA, MENU_BUTTONS, main_menu_kb, inline_main_menu_kb
from bot.keyboards.media import (
    media_type_kb, media_edit_kb, model_top_kb, model_variant_kb,
    model_select_text, model_variant_text, back_to_model_kb, back_to_confirm_kb,
    image_confirm_kb, image_ratio_kb, image_resolution_kb,
    video_confirm_kb, audio_confirm_kb, edit_confirm_kb,
    after_generation_kb,
)
from providers.base import ProviderError, ProviderContentPolicyError, TaskType
from providers.router import get_models_for_task
from services import media_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()


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

    lines.append(f"<b>Описание:</b> {prompt}")
    return "\n".join(lines)


def _confirm_kb(data: dict):
    has_prompt = bool(data.get("prompt"))
    media_type = data.get("media_type")
    if media_type == "image":
        return image_confirm_kb(data.get("aspect_ratio", "1:1"), data.get("resolution", "1K"), has_prompt=has_prompt)
    elif media_type == "video":
        return video_confirm_kb(data.get("duration", 5), has_prompt=has_prompt)
    elif media_type == "audio":
        return audio_confirm_kb(has_prompt=has_prompt)
    elif media_type in ("photo_edit", "video_edit"):
        return edit_confirm_kb(
            has_prompt=has_prompt,
            has_reference=bool(data.get("reference_file_id")),
            media_type=media_type,
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
    update = {
        "model_slug": model_slug,
        "model_label": model_cfg["label"],
        "model_description": model_cfg.get("description", ""),
        "model_variant_description": model_cfg.get("variant_description", ""),
        "model_has_group": bool(model_cfg.get("group")),
        "model_actual_id": model_cfg["model_id"],
        "model_aspect_ratios": aspect_ratios,
    }
    # если текущий ratio недоступен у новой модели — сбрасываем на 1:1
    if aspect_ratios and data.get("aspect_ratio", "1:1") not in aspect_ratios:
        update["aspect_ratio"] = "1:1"
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


@router.callback_query(F.data == "media:back:model")
async def back_to_model(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    media_type = data.get("media_type", "")
    task_type = _TYPE_TO_TASK.get(media_type)
    models = get_models_for_task(task_type) if task_type else []
    type_label = _TYPE_LABELS.get(media_type, media_type)

    await state.update_data(
        prompt=None, model_slug=None, model_label=None,
        model_description=None, model_variant_description=None,
        model_has_group=None, model_aspect_ratios=None,
        confirm_msg_id=None, reference_file_id=None, reference_type=None,
    )
    await state.set_state(MediaStates.select_model)
    await callback.message.edit_text(
        model_select_text(type_label, models),
        parse_mode="HTML",
        reply_markup=model_top_kb(models),
    )
    await callback.answer()


@router.callback_query(F.data == "media:back:menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
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
    await state.clear()
    await state.set_state(MediaStates.select_type)
    await callback.message.delete()
    await callback.message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())
    await callback.answer()


def _find_edit_model(gen_model_id: str) -> dict | None:
    """Возвращает модель редактирования: сначала ищет совпадение по model_id, иначе первую."""
    edit_models = get_models_for_task(TaskType.IMAGE_EDIT)
    return (
        next((m for m in edit_models if m.get("model_id") == gen_model_id), None)
        or (edit_models[0] if edit_models else None)
    )


async def _apply_edit_model_to_state(state: FSMContext, edit_prompt: str | None = None) -> dict | None:
    """Переключает стейт в photo_edit для сгенерированного фото. Возвращает edit_model или None."""
    data = await state.get_data()
    edit_model = _find_edit_model(data.get("model_actual_id", ""))
    if not edit_model:
        return None
    await state.update_data(
        media_type="photo_edit",
        reference_file_id=data["generated_file_id"],
        reference_type="photo",
        prompt=edit_prompt,
        model_slug=edit_model["id"],
        model_label=edit_model["label"],
        model_description=edit_model.get("description", ""),
        model_variant_description=edit_model.get("variant_description", ""),
        model_has_group=bool(edit_model.get("group")),
        model_actual_id=edit_model["model_id"],
        model_aspect_ratios=None,
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

    await state.set_state(MediaStates.enter_prompt)
    await callback.message.answer(
        "Опиши, что нужно изменить на фото:",
        reply_markup=back_to_confirm_kb(),
    )
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:add_reference")
async def add_reference_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка 'Добавить фото/видео' на карточке редактирования."""
    data = await state.get_data()
    media_type = data.get("media_type", "")
    hint = "Отправь фото для редактирования:" if media_type == "photo_edit" else "Отправь видео для редактирования:"
    await callback.message.edit_text(hint, reply_markup=back_to_confirm_kb())
    await state.set_state(MediaStates.enter_reference)
    await callback.answer()


@router.callback_query(F.data == "media:back:confirm")
async def back_to_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат к карточке настроек — работает после генерации и из enter_reference."""
    data = await state.get_data()
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
    await state.update_data(confirm_msg_id=sent.message_id)


@router.message(MediaStates.enter_reference, F.photo)
async def receive_reference_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("media_type") == "video_edit":
        await message.answer("Для редактирования видео пришли видеофайл, а не фото.", reply_markup=back_to_model_kb())
        return
    photo = message.photo[-1]
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


@router.message(MediaStates.enter_reference, ~F.text.in_(MENU_BUTTONS))
async def reference_wrong_type(message: Message) -> None:
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
    await state.update_data(confirm_msg_id=sent.message_id)


@router.message(MediaStates.enter_prompt, F.text, ~F.text.in_(MENU_BUTTONS))
async def receive_prompt(message: Message, state: FSMContext) -> None:
    await state.update_data(prompt=message.text)
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
            "Если хочешь изменить готовое фото — используй раздел «✏️ Редактировать фото».\n\n"
            + _PROMPT_HINTS["image"],
            reply_markup=back_to_confirm_kb(),
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
    # После генерации фото — текст сразу воспринимаем как инструкцию редактирования
    if data.get("generated_file_id") and data.get("media_type") == "image":
        edit_model = await _apply_edit_model_to_state(state, edit_prompt=message.text)
        if edit_model:
            await message.delete()
            await state.set_state(MediaStates.confirm)
            await _update_confirm_card(message, state)
            return
    await state.update_data(prompt=message.text)
    await message.delete()
    await _update_confirm_card(message, state)


@router.message(MediaStates.confirm, ~F.text)
async def confirm_unknown_input(message: Message, state: FSMContext) -> None:
    """Нетекстовый ввод в confirm state."""
    data = await state.get_data()
    media_type = data.get("media_type", "")

    # Во всех случаях удаляем сообщение пользователя
    await message.delete()

    # Для edit-режимов: фото/видео принимаем как загрузку референса
    if media_type == "photo_edit" and message.photo:
        photo = message.photo[-1]
        await state.update_data(reference_file_id=photo.file_id, reference_type="photo")
        await _update_confirm_card(message, state)
        return
    if media_type == "video_edit" and message.video:
        await state.update_data(reference_file_id=message.video.file_id, reference_type="video")
        await _update_confirm_card(message, state)
        return
    if media_type == "photo_edit" and (message.video or message.video_note):
        await message.answer("Для редактирования фото пришли изображение, а не видео.")
        return
    if media_type == "video_edit" and message.photo:
        await message.answer("Для редактирования видео пришли видеофайл, а не фото.")
        return

    # Для генерации: информируем о разнице между генерацией и редактированием
    if message.photo and media_type == "image":
        hint = (
            "Этот раздел создаёт фото с нуля по текстовому описанию. "
            "Если хочешь изменить готовое фото — используй раздел «✏️ Редактировать фото»."
        )
    elif (message.video or message.video_note) and media_type == "video":
        hint = (
            "Этот раздел создаёт видео с нуля по описанию. "
            "Если хочешь изменить готовое видео — используй раздел «✏️ Редактировать видео»."
        )
    else:
        hint = "Не понял запроса. Введи текстовое описание или воспользуйся кнопками."

    await message.answer(hint)
    sent = await message.answer(
        _confirm_card_text(data),
        parse_mode="HTML",
        reply_markup=_confirm_kb(data),
    )
    await state.update_data(confirm_msg_id=sent.message_id)


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
        reply_markup=image_resolution_kb(data.get("resolution", "1K")),
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
    await state.update_data(duration=duration)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
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


# ─── Запуск генерации ─────────────────────────────────────────────────────────

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
    model_slug = data.get("model_slug", "")

    await callback.message.edit_text(
        "⏳ Генерирую, подожди немного...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Долго грузит? ⏳", callback_data="media:gen_why_long"),
        ]]),
    )
    await callback.answer()

    try:
        if media_type == "image":
            result = await media_service.generate_image(
                tg_user.id, tg_user.username, prompt,
                data.get("aspect_ratio", "1:1"), data.get("resolution", "1K"), model_slug
            )
            if result.variants:
                # Несколько вариантов (например, Midjourney) — отправляем карусель
                all_images = [result.data] + result.variants
                media_group = [
                    InputMediaPhoto(media=BufferedInputFile(img, filename=f"image_{i+1}.png"))
                    for i, img in enumerate(all_images)
                ]
                import logging as _log
                _log.getLogger(__name__).info("Sending media group: %d images", len(all_images))
                msgs = await callback.message.answer_media_group(media=media_group)
                _log.getLogger(__name__).info("Media group sent: %d messages returned", len(msgs))
                gen_file_id = msgs[0].photo[-1].file_id if msgs and msgs[0].photo else None
                await state.update_data(generated_file_id=gen_file_id)
                await callback.message.answer("Выбери действие:", reply_markup=after_generation_kb(is_image=True))
            else:
                file = BufferedInputFile(result.data, filename=result.filename)
                sent = await callback.message.answer_photo(file, reply_markup=after_generation_kb(is_image=True))
                await state.update_data(generated_file_id=sent.photo[-1].file_id)

        elif media_type == "video":
            result = await media_service.generate_video(
                tg_user.id, tg_user.username, prompt, data.get("duration", 5), model_slug
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_video(file, reply_markup=after_generation_kb())

        elif media_type == "audio":
            result = await media_service.generate_audio(
                tg_user.id, tg_user.username, prompt, data.get("audio_type", "voice"), model_slug
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_audio(file, reply_markup=after_generation_kb())

        elif media_type == "photo_edit":
            file_info = await callback.bot.get_file(data["reference_file_id"])
            file_bytes = await callback.bot.download_file(file_info.file_path)
            media_bytes = file_bytes.read()
            result = await media_service.edit_image(
                tg_user.id, tg_user.username, media_bytes, prompt, model_slug
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_photo(file, reply_markup=after_generation_kb())

        elif media_type == "video_edit":
            file_info = await callback.bot.get_file(data["reference_file_id"])
            file_bytes = await callback.bot.download_file(file_info.file_path)
            media_bytes = file_bytes.read()
            result = await media_service.edit_video(
                tg_user.id, tg_user.username, media_bytes, prompt, model_slug
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_video(file, reply_markup=after_generation_kb())

        await callback.message.delete()
        # state не очищаем — данные нужны для кнопки "Назад"

    except InsufficientCreditsError as e:
        await callback.message.edit_text(
            f"❌ {e}\n\nПополни баланс в разделе «Мой баланс».",
            reply_markup=after_generation_kb(),
        )
    except RateLimitError as e:
        await callback.message.edit_text(f"⏱ {e}", reply_markup=after_generation_kb())
    except ProviderContentPolicyError:
        await callback.message.edit_text(
            "❌ Запрос не прошёл проверку безопасности. Попробуй изменить описание.",
            reply_markup=after_generation_kb(),
        )
    except ProviderError:
        await callback.message.edit_text(
            "⚠️ Сервис временно недоступен. Попробуй позже.",
            reply_markup=after_generation_kb(),
        )
    except Exception:
        await callback.message.edit_text(
            "⚠️ Произошла непредвиденная ошибка. Попробуй позже.",
            reply_markup=after_generation_kb(),
        )
