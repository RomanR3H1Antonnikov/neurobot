import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_MEDIA, main_menu_kb
from bot.keyboards.media import (
    media_type_kb, image_confirm_kb, video_confirm_kb,
    audio_confirm_kb, edit_confirm_kb, after_generation_kb,
)
from providers.base import ProviderError, ProviderContentPolicyError
from services import media_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()


class MediaStates(StatesGroup):
    select_type = State()
    enter_prompt = State()
    enter_reference = State()  # загрузка фото для редактирования
    confirm = State()


def _media_type_label(media_type: str) -> str:
    return {"image": "Фото", "video": "Видео", "audio": "Аудио", "edit": "Редактирование фото"}.get(media_type, media_type)


def _confirm_card_text(data: dict) -> str:
    media_type = data.get("media_type", "")
    prompt = data.get("prompt") or "не задан"
    lines = [
        f"<b>Тип:</b> {_media_type_label(media_type)}",
        f"<b>Промпт:</b> {prompt}",
    ]
    if media_type == "image":
        lines.append(f"<b>Формат:</b> {data.get('aspect_ratio', '1:1')}")
    elif media_type == "video":
        lines.append(f"<b>Длительность:</b> {data.get('duration', 5)} сек")
    elif media_type == "audio":
        t = "Озвучка" if data.get("audio_type", "voice") == "voice" else "Музыка"
        lines.append(f"<b>Тип:</b> {t}")
    return "\n".join(lines)


def _confirm_kb(data: dict):
    media_type = data.get("media_type")
    if media_type == "image":
        return image_confirm_kb(data.get("aspect_ratio", "1:1"))
    elif media_type == "video":
        return video_confirm_kb(data.get("duration", 5))
    elif media_type == "audio":
        return audio_confirm_kb(data.get("audio_type", "voice"))
    else:
        return edit_confirm_kb()


# ─── Вход в раздел ───────────────────────────────────────────────────────────

@router.message(F.text == BTN_MEDIA)
async def media_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(MediaStates.select_type)
    await message.answer("Выбери тип контента:", reply_markup=media_type_kb())


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

    prompt_hint = {
        "image": "Опиши изображение, которое хочешь получить:",
        "video": "Опиши видео (действие, сцена, стиль):",
        "audio": "Введи текст для озвучки или описание музыки:",
        "edit": "Пришли фото, которое нужно отредактировать:",
    }[media_type]

    await callback.message.edit_text(prompt_hint)
    next_state = MediaStates.enter_reference if media_type == "edit" else MediaStates.enter_prompt
    await state.set_state(next_state)
    await callback.answer()


# ─── Загрузка референса (только для edit) ────────────────────────────────────

@router.message(MediaStates.enter_reference, F.photo)
async def receive_reference_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]  # максимальное разрешение
    file_id = photo.file_id
    await state.update_data(reference_file_id=file_id)
    await state.set_state(MediaStates.enter_prompt)
    await message.answer("Теперь опиши, что нужно изменить на фото:")


@router.message(MediaStates.enter_reference)
async def reference_not_photo(message: Message) -> None:
    await message.answer("Пожалуйста, пришли фотографию (не файлом).")


# ─── Ввод промпта ────────────────────────────────────────────────────────────

@router.message(MediaStates.enter_prompt, F.text)
async def receive_prompt(message: Message, state: FSMContext) -> None:
    await state.update_data(prompt=message.text)
    data = await state.get_data()
    await state.set_state(MediaStates.confirm)
    await message.answer(
        _confirm_card_text(data),
        parse_mode="HTML",
        reply_markup=_confirm_kb(data),
    )


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


@router.callback_query(MediaStates.confirm, F.data.startswith("media:audio_type:"))
async def set_audio_type(callback: CallbackQuery, state: FSMContext) -> None:
    audio_type = callback.data.split(":")[2]
    await state.update_data(audio_type=audio_type)
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(MediaStates.confirm, F.data == "media:edit_prompt")
async def edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MediaStates.enter_prompt)
    await callback.message.edit_text("Введи новый промпт:")
    await callback.answer()


# ─── Навигация назад ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "media:back:type")
async def back_to_type(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MediaStates.select_type)
    await callback.message.edit_text("Выбери тип контента:", reply_markup=media_type_kb())
    await callback.answer()


@router.callback_query(F.data == "media:back:menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "media:again")
async def generate_again(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MediaStates.select_type)
    await callback.message.edit_text("Выбери тип контента:", reply_markup=media_type_kb())
    await callback.answer()


# ─── Запуск генерации ─────────────────────────────────────────────────────────

@router.callback_query(MediaStates.confirm, F.data == "media:start")
async def start_generation(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    tg_user = callback.from_user
    media_type = data["media_type"]
    prompt = data.get("prompt", "")

    await callback.message.edit_text("⏳ Генерирую, подожди немного...")
    await callback.answer()

    try:
        if media_type == "image":
            result = await media_service.generate_image(
                tg_user.id, tg_user.username, prompt, data.get("aspect_ratio", "1:1")
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_photo(file, reply_markup=after_generation_kb())

        elif media_type == "video":
            result = await media_service.generate_video(
                tg_user.id, tg_user.username, prompt, data.get("duration", 5)
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_video(file, reply_markup=after_generation_kb())

        elif media_type == "audio":
            result = await media_service.generate_audio(
                tg_user.id, tg_user.username, prompt, data.get("audio_type", "voice")
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_audio(file, reply_markup=after_generation_kb())

        elif media_type == "edit":
            # скачиваем референс-фото
            file_info = await callback.bot.get_file(data["reference_file_id"])
            file_bytes = await callback.bot.download_file(file_info.file_path)
            image_bytes = file_bytes.read()
            result = await media_service.edit_image(
                tg_user.id, tg_user.username, image_bytes, prompt
            )
            file = BufferedInputFile(result.data, filename=result.filename)
            await callback.message.answer_photo(file, reply_markup=after_generation_kb())

        await callback.message.delete()
        await state.clear()

    except InsufficientCreditsError as e:
        await callback.message.edit_text(
            f"❌ {e}\n\nПополни баланс в разделе «Мой баланс».",
            reply_markup=after_generation_kb(),
        )
    except RateLimitError as e:
        await callback.message.edit_text(
            f"⏱ {e}",
            reply_markup=after_generation_kb(),
        )
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
