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
    builder.row(
        InlineKeyboardButton(text="✏️ Редактировать медиа", callback_data="media:type:edit"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:menu"))
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
    has_desc = any(m.get("description") and not m.get("group") for m in models)
    if not has_desc:
        return f"<b>Выбери модель ({type_label}):</b>"
    lines = [f"<b>Выбери модель ({type_label}):</b>\n"]
    for m in models:
        if m.get("group"):
            continue  # описания групповых моделей — на втором уровне
        desc = m.get("description", "")
        if desc:
            lines.append(f"• <b>{m['label']}</b> — {desc}")
        else:
            lines.append(f"• <b>{m['label']}</b>")
    return "\n".join(lines)


def model_variant_text(group_label: str) -> str:
    return f"<b>Выбери версию ({group_label}):</b>"


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

def image_confirm_kb(aspect_ratio: str) -> InlineKeyboardMarkup:
    ratios = {"1:1": "Квадрат", "16:9": "Пейзаж", "9:16": "Портрет"}
    builder = InlineKeyboardBuilder()
    for ratio, label in ratios.items():
        prefix = "✅ " if ratio == aspect_ratio else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"media:ratio:{ratio}",
        ))
    builder.adjust(3)
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить описание", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
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
        InlineKeyboardButton(text="✏️ Изменить описание", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── Карточка подтверждения: аудио ───────────────────────────────────────────

def audio_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить описание", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать генерацию", callback_data="media:start"))
    return builder.as_markup()


# ─── Карточка подтверждения: редактирование ──────────────────────────────────

def edit_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить инструкцию", callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    builder.row(InlineKeyboardButton(text="🚀 Начать обработку", callback_data="media:start"))
    return builder.as_markup()


# ─── После генерации ─────────────────────────────────────────────────────────

def after_generation_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 Сгенерировать ещё", callback_data="media:again"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="media:back:menu"),
    )
    return builder.as_markup()
