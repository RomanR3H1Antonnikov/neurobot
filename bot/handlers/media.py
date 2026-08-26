import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_MEDIA, MENU_BUTTONS, main_menu_kb
from bot.keyboards.media import (
    media_type_kb, model_top_kb, model_variant_kb,
    model_select_text, model_variant_text, back_to_model_kb,
    image_confirm_kb, video_confirm_kb, audio_confirm_kb, edit_confirm_kb,
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
    "photo_edit": "Редактирование фото",
    "video_edit": "Редактирование видео",
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
    prompt = data.get("prompt") or "не задан"
    model_label = data.get("model_label", "—")
    model_description = data.get("model_description", "")

    lines = []
    # для сгруппированных моделей описание уже было показано на экране выбора версии
    if model_description and not data.get("model_has_group"):
        lines.append(f"<b>{model_label}</b>\n<blockquote expandable>{model_description}</blockquote>")
    else:
        lines.append(f"<b>Модель:</b> {model_label}")

    if media_type == "image":
        lines.append(f"<b>Формат:</b> {data.get('aspect_ratio', '1:1')}")
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

    lines.append(f"<b>Промпт:</b> {prompt}")
    return "\n".join(lines)


def _confirm_kb(data: dict):
    has_prompt = bool(data.get("prompt"))
    media_type = data.get("media_type")
    if media_type == "image":
        return image_confirm_kb(data.get("aspect_ratio", "1:1"), has_prompt=has_prompt)
    elif media_type == "video":
        return video_confirm_kb(data.get("duration", 5), has_prompt=has_prompt)
    elif media_type == "audio":
        return audio_confirm_kb(has_prompt=has_prompt)
    elif media_type in ("photo_edit", "video_edit"):
        return edit_confirm_kb(has_prompt=has_prompt)
    return edit_confirm_kb(has_prompt=has_prompt)


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

@router.callback_query(MediaStates.select_type, F.data.startswith("media:type:"))
async def select_type(callback: CallbackQuery, state: FSMContext) -> None:
    media_type = callback.data.split(":")[2]
    defaults = {
        "image": {"aspect_ratio": "1:1"},
        "video": {"duration": 5},
        "audio": {"audio_type": "voice"},
        "edit": {},
    }
    await state.update_data(media_type=media_type, **defaults.get(media_type, {}))

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

    update = {
        "model_slug": model_slug,
        "model_label": model_cfg["label"],
        "model_description": model_cfg.get("description", ""),
        "model_has_group": bool(model_cfg.get("group")),
        "model_actual_id": model_cfg["model_id"],
    }
    # для аудио — тип (voice/music) берём из конфига модели
    if media_type == "audio":
        update["audio_type"] = model_cfg.get("audio_type", "voice")

    await state.update_data(**update)
    data = await state.get_data()

    if media_type == "photo_edit":
        await callback.message.edit_text(
            "Пришли фото, которое нужно отредактировать:",
            reply_markup=back_to_model_kb(),
        )
        await state.set_state(MediaStates.enter_reference)
    elif media_type == "video_edit":
        await callback.message.edit_text(
            "Пришли видео, которое нужно отредактировать:",
            reply_markup=back_to_model_kb(),
        )
        await state.set_state(MediaStates.enter_reference)
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


@router.callback_query(F.data == "media:again")
async def generate_again(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(MediaStates.select_type)
    await callback.message.delete()
    await callback.message.answer(MEDIA_MENU_TEXT, parse_mode="HTML", reply_markup=media_type_kb())
    await callback.answer()


@router.callback_query(F.data == "media:back:confirm")
async def back_to_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(MediaStates.confirm)
    await callback.message.edit_reply_markup(reply_markup=None)
    sent = await callback.message.answer(
        _confirm_card_text(data),
        parse_mode="HTML",
        reply_markup=_confirm_kb(data),
    )
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
            reply_markup=back_to_model_kb(),
        )
    elif (message.video or message.video_note) and media_type == "video":
        await message.answer(
            "Этот раздел создаёт видео с нуля по описанию. "
            "Если хочешь изменить готовое видео — используй раздел «✏️ Редактировать видео».\n\n"
            + _PROMPT_HINTS["video"],
            reply_markup=back_to_model_kb(),
        )
    else:
        await message.answer(
            _PROMPT_HINTS.get(media_type, "Введи текстовое описание:"),
            reply_markup=back_to_model_kb(),
        )


# ─── Ввод/изменение описания прямо из confirm карточки ───────────────────────

@router.message(MediaStates.confirm, F.text, ~F.text.in_(MENU_BUTTONS))
async def update_prompt_in_confirm(message: Message, state: FSMContext) -> None:
    await state.update_data(prompt=message.text)
    await _update_confirm_card(message, state)


@router.message(MediaStates.confirm, ~F.text)
async def confirm_unknown_input(message: Message, state: FSMContext) -> None:
    """Нетекстовый ввод в confirm state — поясняем и повторяем карточку."""
    data = await state.get_data()
    media_type = data.get("media_type", "")

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

@router.callback_query(MediaStates.confirm, F.data.startswith("media:ratio:"))
async def set_ratio(callback: CallbackQuery, state: FSMContext) -> None:
    ratio = callback.data.split(":")[2]
    await state.update_data(aspect_ratio=ratio)
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
        reply_markup=back_to_model_kb(),
    )
    await callback.answer()


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
    model_slug = data.get("model_slug", "")

    await callback.message.edit_text("⏳ Генерирую, подожди немного...")
    await callback.answer()

    try:
        if media_type == "image":
            result = await media_service.generate_image(
                tg_user.id, tg_user.username, prompt, data.get("aspect_ratio", "1:1"), model_slug
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_photo(file, reply_markup=after_generation_kb())

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
