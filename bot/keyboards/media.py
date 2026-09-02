from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ─── Выбор типа медиа ────────────────────────────────────────────────────────

def media_type_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🖼 Фото", callback_data="media:type:image"),
        InlineKeyboardButton(text="🎬 Видео", callback_data="media:type:video"),
        InlineKeyboardButton(text="🎵 Аудио", callback_data="media:type:audio"),
    )
    builder.row(InlineKeyboardButton(text="✏️ Редактировать медиа", callback_data="media:edit_menu"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:menu"))
    return builder.as_markup()


def media_edit_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🖼 Изменить фото", callback_data="media:type:photo_edit"),
        InlineKeyboardButton(text="🎬 Изменить видео", callback_data="media:type:video_edit"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"))
    return builder.as_markup()


# ─── Выбор модели (двухуровневый) ────────────────────────────────────────────

def _top_level_items(models: list[dict]) -> list[dict]:
    """
    Строит список первого уровня: группы (→ выбор версии) + одиночные модели.
    Группы дедуплицируются и идут в порядке первого появления в yaml.
    """
    seen_groups: set[str] = set()
    result = []
    for m in models:
        group = m.get("group")
        if group:
            if group not in seen_groups:
                seen_groups.add(group)
                result.append({
                    "_type": "group",
                    "group_id": group,
                    "label": m.get("group_label", group),
                })
        else:
            result.append({"_type": "model", **m})
    return result


def model_top_kb(models: list[dict]) -> InlineKeyboardMarkup:
    """Первый уровень выбора: группы со стрелкой + одиночные модели с ценой."""
    builder = InlineKeyboardBuilder()
    for item in _top_level_items(models):
        if item["_type"] == "group":
            builder.row(InlineKeyboardButton(
                text=f"{item['label']} ›",
                callback_data=f"media:group:{item['group_id']}",
            ))
        else:
            builder.row(InlineKeyboardButton(
                text=f"{item['label']} — {item['cost_credits']} кр.",
                callback_data=f"media:model:{item['id']}",
            ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"))
    return builder.as_markup()


def model_variant_kb(variants: list[dict]) -> InlineKeyboardMarkup:
    """Второй уровень: конкретные версии модели."""
    builder = InlineKeyboardBuilder()
    for m in variants:
        builder.row(InlineKeyboardButton(
            text=f"{m['label']} — {m['cost_credits']} кр.",
            callback_data=f"media:model:{m['id']}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"))
    return builder.as_markup()


def model_select_text(type_label: str, models: list[dict]) -> str:
    """Текст сообщения над клавиатурой первого уровня."""
    return f"<b>Выбери модель ({type_label}):</b>"


def model_variant_text(group_label: str, description: str = "") -> str:
    header = f"<b>Выбери версию ({group_label}):</b>"
    if description:
        return f"{header}\n<blockquote expandable>{description}</blockquote>"
    return header


# ─── Навигация: назад к выбору модели ────────────────────────────────────────

def back_to_model_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"))
    return builder.as_markup()


def back_to_type_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"))
    return builder.as_markup()


# ─── Карточка подтверждения: фото ────────────────────────────────────────────

ALL_RATIOS = ["1:1", "2:3", "3:2", "1:4", "4:1", "3:4", "4:3", "4:5", "5:4", "1:8", "8:1", "9:16", "16:9"]
ALL_RESOLUTIONS = ["1K", "2K", "4K"]


def style_ref_collecting_kb(count: int, max_refs: int = 14) -> InlineKeyboardMarkup:
    """Клавиатура при сборе фото-ориентиров."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"✅ Готово ({count}/{max_refs})", callback_data="media:style_ref_done"))
    if count > 0:
        builder.row(InlineKeyboardButton(text="🗑 Очистить всё", callback_data="media:style_ref_clear"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def image_confirm_kb(
    aspect_ratio: str, resolution: str = "1K", has_prompt: bool = False, style_ref_count: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"📐 Масштаб: {aspect_ratio}", callback_data="media:pick_ratio"),
        InlineKeyboardButton(text=f"🖼 Качество: {resolution}", callback_data="media:pick_resolution"),
    )
    ref_text = f"🖼 Ориентиры: {style_ref_count} фото ✅" if style_ref_count else "📎 Добавить ориентир"
    builder.row(InlineKeyboardButton(text=ref_text, callback_data="media:add_style_ref"))
    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


def image_ratio_kb(current: str, allowed_ratios: list[str] | None = None) -> InlineKeyboardMarkup:
    ratios = allowed_ratios if allowed_ratios else ALL_RATIOS
    builder = InlineKeyboardBuilder()
    for ratio in ratios:
        prefix = "✅ " if ratio == current else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{ratio}",
            callback_data=f"media:ratio:{ratio}",
        ))
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def image_resolution_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for res in ALL_RESOLUTIONS:
        prefix = "✅ " if res == current else ""
        builder.add(InlineKeyboardButton(text=f"{prefix}{res}", callback_data=f"media:resolution:{res}"))
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


# ─── Карточка подтверждения: видео ───────────────────────────────────────────

def video_confirm_kb(
    duration: int,
    has_prompt: bool = False,
    duration_options: list[int] | None = None,
    has_first_frame: bool = False,
    has_last_frame: bool = False,
    frames_expanded: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=f"⏱ Длительность: {duration} сек",
        callback_data="media:pick_duration",
    ))
    if frames_expanded or has_first_frame or has_last_frame:
        first_text = "📎 Первый кадр: фото ✅" if has_first_frame else "📎 Первый кадр"
        last_text = "📎 Последний кадр: фото ✅" if has_last_frame else "📎 Последний кадр"
        builder.row(InlineKeyboardButton(text=first_text, callback_data="media:add_first_frame"))
        builder.row(InlineKeyboardButton(text=last_text, callback_data="media:add_last_frame"))
    else:
        builder.row(InlineKeyboardButton(text="📎 Добавить кадры", callback_data="media:toggle_frames"))
    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


def video_duration_picker_kb(
    current: int,
    options: list[int],
    min_d: int,
    max_d: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for d in options:
        prefix = "✅ " if d == current else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{d} сек",
            callback_data=f"media:duration:{d}",
        ))
    builder.adjust(min(len(options), 3))
    builder.row(InlineKeyboardButton(
        text=f"✏️ Своя ({min_d}–{max_d} сек)",
        callback_data="media:duration_custom",
    ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


# ─── Карточка подтверждения: аудио ───────────────────────────────────────────

def audio_confirm_kb(has_prompt: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── Карточка подтверждения: редактирование ──────────────────────────────────

def edit_confirm_kb(
    has_prompt: bool = False,
    has_reference: bool = False,
    media_type: str = "photo_edit",
    style_ref_count: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if media_type == "photo_edit":
        ref_text = "📎 Изменить фото" if has_reference else "📎 Добавить фото"
    else:
        ref_text = "📎 Изменить видео" if has_reference else "📎 Добавить видео"
    builder.row(InlineKeyboardButton(text=ref_text, callback_data="media:add_reference"))
    if media_type == "photo_edit":
        style_text = f"🖼 Ориентиры: {style_ref_count} фото ✅" if style_ref_count else "📎 Добавить ориентир"
        builder.row(InlineKeyboardButton(text=style_text, callback_data="media:add_style_ref"))
    prompt_text = "✏️ Изменить инструкцию" if has_prompt else "✏️ Ввести инструкцию"
    builder.row(
        InlineKeyboardButton(text=prompt_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать обработку", callback_data="media:start"))
    return builder.as_markup()


def back_to_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


# ─── После генерации ─────────────────────────────────────────────────────────

def error_kb() -> InlineKeyboardMarkup:
    """Клавиатура под сообщением об ошибке генерации."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="media:again"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="media:back:menu"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"),
        InlineKeyboardButton(text="📋 К моделям", callback_data="media:back:model"),
    )
    return builder.as_markup()


def gen_waiting_kb() -> InlineKeyboardMarkup:
    """Клавиатура под сообщением «Генерирую…» — кнопка помощи и просмотр промпта."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Долго грузит? ⏳", callback_data="media:gen_why_long"),
            InlineKeyboardButton(text="📝 Описание", callback_data="media:show_prompt"),
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="media:cancel_generation"),
        ],
    ])


def after_generation_kb(is_image: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_image:
        builder.row(InlineKeyboardButton(text="✏️ Редактировать", callback_data="media:edit_generated"))
    builder.row(
        InlineKeyboardButton(text="🔄 Сгенерировать ещё", callback_data="media:again"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="media:back:menu"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"))
    return builder.as_markup()
