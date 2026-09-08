"""
Раздел «Мои генерации»: просмотр результатов за последние 24 часа.
"""
import time
import logging
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.main_menu import BTN_MY_GENS
from db.queries import get_recent_generations, get_generation_counts

router = Router()
logger = logging.getLogger(__name__)

_TYPE_LABELS = {
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "audio": "🎵 Аудио",
}

_HOURS = 24
_ALBUM_SIZE = 10  # максимум фото в одном media group


def _overview_kb(counts: dict[str, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in _TYPE_LABELS.items():
        n = counts.get(key, 0)
        builder.button(
            text=f"{label} ({n})" if n else f"{label} (0)",
            callback_data=f"mygen:show:{key}",
        )
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="mygen:menu"))
    return builder.as_markup()


def _back_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data="mygen:overview"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="mygen:menu"),
    )
    return builder.as_markup()


def _overview_text(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if total == 0:
        return "📁 <b>Мои генерации</b>\n\nЗа последние 24 часа генераций нет."
    return "📁 <b>Мои генерации</b>\n\nВыбери тип, чтобы посмотреть результаты за последние 24 часа:"


def _relative_time(created_at: int) -> str:
    diff = int(time.time()) - created_at
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{diff // 60} мин назад"
    return f"{diff // 3600} ч назад"


@router.callback_query(F.data == "menu:mygenerations")
async def menu_to_mygenerations(callback: CallbackQuery) -> None:
    await callback.answer()
    counts = await get_generation_counts(callback.from_user.id)
    await callback.message.edit_text(
        _overview_text(counts),
        parse_mode="HTML",
        reply_markup=_overview_kb(counts),
    )


@router.message(F.text == BTN_MY_GENS)
async def my_generations_menu(message: Message) -> None:
    counts = await get_generation_counts(message.from_user.id)
    await message.answer(
        _overview_text(counts),
        parse_mode="HTML",
        reply_markup=_overview_kb(counts),
    )


@router.callback_query(F.data == "mygen:overview")
async def mygen_overview(callback: CallbackQuery) -> None:
    counts = await get_generation_counts(callback.from_user.id)
    try:
        await callback.message.edit_text(
            _overview_text(counts),
            parse_mode="HTML",
            reply_markup=_overview_kb(counts),
        )
    except Exception:
        await callback.message.answer(
            _overview_text(counts),
            parse_mode="HTML",
            reply_markup=_overview_kb(counts),
        )
    await callback.answer()


@router.callback_query(F.data == "mygen:menu")
async def mygen_to_menu(callback: CallbackQuery) -> None:
    from bot.keyboards.main_menu import inline_main_menu_kb
    try:
        await callback.message.edit_text("Главное меню:", reply_markup=inline_main_menu_kb())
    except Exception:
        await callback.message.answer("Главное меню:", reply_markup=inline_main_menu_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("mygen:show:"))
async def mygen_show_type(callback: CallbackQuery) -> None:
    media_type = callback.data.split(":")[-1]
    label = _TYPE_LABELS.get(media_type, media_type)

    items = await get_recent_generations(callback.from_user.id, media_type)
    if not items:
        await callback.answer(f"Нет результатов за последние {_HOURS} часов", show_alert=True)
        return

    await callback.answer()

    if media_type == "photo":
        await _send_photos(callback.message, items, label)
    elif media_type == "video":
        await _send_videos(callback.message, items, label)
    elif media_type == "audio":
        await _send_audios(callback.message, items, label)


async def _send_photos(message: Message, items: list[dict], label: str) -> None:
    # Группируем по ALBUM_SIZE, подпись только на последней группе
    chunks = [items[i:i + _ALBUM_SIZE] for i in range(0, len(items), _ALBUM_SIZE)]
    for chunk_idx, chunk in enumerate(chunks):
        is_last_chunk = (chunk_idx == len(chunks) - 1)
        if len(chunk) == 1:
            caption = _item_caption(chunk[0])
            kb = _back_kb() if is_last_chunk else None
            await message.answer_photo(chunk[0]["file_id"], caption=caption, reply_markup=kb)
        else:
            media = []
            for i, item in enumerate(chunk):
                cap = _item_caption(item) if i == 0 else None
                media.append(InputMediaPhoto(media=item["file_id"], caption=cap))
            await message.answer_media_group(media)
            if is_last_chunk:
                await message.answer(
                    f"{label}: показано {len(items)} за последние {_HOURS} ч",
                    reply_markup=_back_kb(),
                )


async def _send_videos(message: Message, items: list[dict], label: str) -> None:
    for i, item in enumerate(items):
        is_last = (i == len(items) - 1)
        caption = _item_caption(item)
        kb = _back_kb() if is_last else None
        try:
            await message.answer_video(item["file_id"], caption=caption, reply_markup=kb)
        except Exception:
            logger.warning("mygen: не удалось отправить видео file_id=%s", item["file_id"])
            if is_last:
                await message.answer(
                    f"{label}: показано {i} из {len(items)} (часть файлов недоступна)",
                    reply_markup=_back_kb(),
                )


async def _send_audios(message: Message, items: list[dict], label: str) -> None:
    for i, item in enumerate(items):
        is_last = (i == len(items) - 1)
        caption = _item_caption(item)
        kb = _back_kb() if is_last else None
        try:
            await message.answer_audio(item["file_id"], caption=caption, reply_markup=kb)
        except Exception:
            logger.warning("mygen: не удалось отправить аудио file_id=%s", item["file_id"])
            if is_last:
                await message.answer(
                    f"{label}: показано {i} из {len(items)} (часть файлов недоступна)",
                    reply_markup=_back_kb(),
                )


def _item_caption(item: dict) -> str:
    parts = []
    if item.get("model_label"):
        parts.append(item["model_label"])
    if item.get("prompt"):
        p = item["prompt"]
        parts.append("Описание: " + (p if len(p) <= 100 else p[:97] + "…"))
    parts.append(_relative_time(item["created_at"]))
    return " · ".join(parts)
