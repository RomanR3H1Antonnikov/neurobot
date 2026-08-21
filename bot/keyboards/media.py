from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ─── Выбор типа медиа ────────────────────────────────────────────────────────

def media_type_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🖼 Фото", callback_data="media:type:image"),
        InlineKeyboardButton(text="🎬 Видео", callback_data="media:type:video"),
    )
    builder.row(
        InlineKeyboardButton(text="🎵 Аудио", callback_data="media:type:audio"),
        InlineKeyboardButton(text="✏️ Редактировать фото", callback_data="media:type:edit"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:menu"))
    return builder.as_markup()


# ─── Карточка подтверждения: фото ────────────────────────────────────────────

def image_confirm_kb(aspect_ratio: str) -> InlineKeyboardMarkup:
    ratios = {"1:1": "Квадрат", "16:9": "Пейзаж", "9:16": "Портрет"}
    builder = InlineKeyboardBuilder()
    # кнопки выбора формата
    for ratio, label in ratios.items():
        prefix = "✅ " if ratio == aspect_ratio else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"media:ratio:{ratio}",
        ))
    builder.adjust(3)
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить промпт", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── Карточка подтверждения: видео ───────────────────────────────────────────

def video_confirm_kb(duration: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d, label in [(5, "5 сек"), (10, "10 сек")]:
        prefix = "✅ " if d == duration else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"media:duration:{d}",
        ))
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить промпт", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── Карточка подтверждения: аудио ───────────────────────────────────────────

def audio_confirm_kb(audio_type: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for t, label in [("voice", "🗣 Озвучка"), ("music", "🎸 Музыка")]:
        prefix = "✅ " if t == audio_type else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"media:audio_type:{t}",
        ))
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить текст", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── Карточка подтверждения: редактирование ──────────────────────────────────

def edit_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить инструкцию", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── После генерации ─────────────────────────────────────────────────────────

def after_generation_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 Сгенерировать ещё", callback_data="media:again"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="media:back:menu"),
    )
    return builder.as_markup()
