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
    builder.row(
        InlineKeyboardButton(text="ℹ️ Инфо", callback_data="media:info"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:menu"),
    )
    return builder.as_markup()


def media_info_kb(expanded: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if expanded:
        builder.row(InlineKeyboardButton(text="Свернуть ▲", callback_data="media:info"))
    else:
        builder.row(InlineKeyboardButton(text="Развернуть ▼", callback_data="media:info:expand"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"))
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


def _cost_label(model: dict) -> str:
    by_res = model.get("cost_by_resolution")
    if by_res:
        lo, hi = min(by_res.values()), max(by_res.values())
        return f"{lo}–{hi} ₽" if lo != hi else f"{lo} ₽"
    if model.get("cost_per_second"):
        return f"от {model['cost_credits']} ₽"
    return f"{model['cost_credits']} ₽"


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
                text=item['label'],
                callback_data=f"media:model:{item['id']}",
            ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:type"))
    return builder.as_markup()


def model_variant_kb(variants: list[dict]) -> InlineKeyboardMarkup:
    """Второй уровень: конкретные версии модели."""
    builder = InlineKeyboardBuilder()
    for m in variants:
        builder.row(InlineKeyboardButton(
            text=m['label'],
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
ALL_RESOLUTIONS = ["1K", "2K", "3K", "4K"]


def style_ref_delete_kb(count: int, max_refs: int = 14) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Удалить все", callback_data="media:style_ref_clear"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:style_ref_back_to_collect"))
    return builder.as_markup()


def audio_ref_delete_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Удалить все", callback_data="media:delete_audio_refs_all"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:audio_ref_back_to_list"))
    return builder.as_markup()


def style_ref_collecting_kb(count: int, max_refs: int = 14) -> InlineKeyboardMarkup:
    """Клавиатура при сборе фото-ориентиров."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"✅ Готово ({count}/{max_refs})", callback_data="media:style_ref_done"))
    if count > 0:
        builder.row(
            InlineKeyboardButton(text="❌ Удалить фото", callback_data="media:style_ref_delete"),
            InlineKeyboardButton(text="✏️ Заменить", callback_data="media:style_ref_replace"),
        )
        builder.row(InlineKeyboardButton(text="🗑 Очистить всё", callback_data="media:style_ref_clear"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def image_confirm_kb(
    aspect_ratio: str, resolution: str = "1K", has_prompt: bool = False,
    style_ref_count: int = 0, max_style_refs: int = 14,
    cost_credits: int | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"📐 Масштаб: {aspect_ratio}", callback_data="media:pick_ratio"),
        InlineKeyboardButton(text=f"🖼 Качество: {resolution}", callback_data="media:pick_resolution"),
    )
    if max_style_refs > 0:
        ref_text = f"🖼 Ориентиры: {style_ref_count} фото ✅" if style_ref_count else "📎 Добавить ориентир"
        builder.row(InlineKeyboardButton(text=ref_text, callback_data="media:add_style_ref"))
    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    start_text = f"🚀 Начать генерацию — {cost_credits} ₽" if cost_credits else "🚀 Начать генерацию"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
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


def image_resolution_kb(current: str, allowed_resolutions: list[str] | None = None) -> InlineKeyboardMarkup:
    resolutions = allowed_resolutions if allowed_resolutions else ALL_RESOLUTIONS
    builder = InlineKeyboardBuilder()
    for res in resolutions:
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
    has_extra_refs: bool = False,
    aspect_ratio: str | None = None,
    has_aspect_ratios: bool = False,
    resolution: str | None = None,
    has_resolutions: bool = False,
    audio_ref_count: int = 0,
    max_audio_refs: int = 0,
    video_ref_count: int = 0,
    max_video_refs: int = 0,
    show_frames_button: bool = True,
    show_first_frame_btn: bool = True,
    show_constructor_btn: bool = True,
    frames_mode: str | None = None,
    show_first_frame_slot: bool = False,
    show_last_frame_slot: bool = False,
    output_format: str | None = None,
    output_formats: list[str] | None = None,
    audio_enabled: bool = True,
    show_audio_toggle: bool = True,
    motion_orientation: str | None = None,
    cost_credits: int | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=f"⏱ Длительность: {duration} сек",
        callback_data="media:pick_duration",
    ))
    if has_aspect_ratios and has_resolutions and aspect_ratio and resolution:
        builder.row(
            InlineKeyboardButton(text=f"📐 Масштаб: {aspect_ratio}", callback_data="media:pick_ratio"),
            InlineKeyboardButton(text=f"🖼 Качество: {resolution}", callback_data="media:pick_resolution"),
        )
    elif has_aspect_ratios and aspect_ratio:
        builder.row(InlineKeyboardButton(text=f"📐 Масштаб: {aspect_ratio}", callback_data="media:pick_ratio"))
    elif has_resolutions and resolution:
        builder.row(InlineKeyboardButton(text=f"🖼 Качество: {resolution}", callback_data="media:pick_resolution"))
    if output_formats and len(output_formats) > 1 and output_format:
        builder.row(InlineKeyboardButton(
            text=f"📁 Формат: {output_format.upper()}",
            callback_data="media:pick_format",
        ))
    if show_frames_button:
        # frames_mode — авторитет: игнорируем файлы "чужого" режима,
        # чтобы оба кнопки никогда не показывали ✅ одновременно
        if frames_mode == "animate":
            has_animate_files = has_first_frame or has_last_frame
            has_constructor_files = False
        elif frames_mode == "constructor":
            has_animate_files = False
            has_constructor_files = has_extra_refs or video_ref_count > 0
        else:
            has_animate_files = has_first_frame or has_last_frame
            has_constructor_files = has_extra_refs or video_ref_count > 0
        row_btns = []
        if show_first_frame_btn:
            if has_animate_files:
                animate_text = "🖼 Оживить фото ✅"
            elif has_constructor_files:
                animate_text = "🖼 Оживить фото ❌"
            else:
                animate_text = "🖼 Оживить фото"
            row_btns.append(InlineKeyboardButton(text=animate_text, callback_data="media:animate_photo"))
        if show_constructor_btn:
            if has_constructor_files:
                constr_text = "🎬 Конструктор видео ✅"
            elif has_animate_files:
                constr_text = "🎬 Конструктор видео ❌"
            else:
                constr_text = "🎬 Конструктор видео"
            row_btns.append(InlineKeyboardButton(text=constr_text, callback_data="media:toggle_frames"))
        if row_btns:
            builder.row(*row_btns)
    if show_first_frame_slot:
        if has_first_frame:
            builder.row(
                InlineKeyboardButton(text="📎 Начало видео ✅", callback_data="media:add_first_frame"),
                InlineKeyboardButton(text="🗑", callback_data="media:delete_first_frame"),
            )
        else:
            builder.row(InlineKeyboardButton(text="📎 Начало видео", callback_data="media:add_first_frame"))
    if show_last_frame_slot:
        if has_last_frame:
            builder.row(
                InlineKeyboardButton(text="📎 Конец видео ✅", callback_data="media:add_last_frame"),
                InlineKeyboardButton(text="🗑", callback_data="media:delete_last_frame"),
            )
        else:
            builder.row(InlineKeyboardButton(text="📎 Конец видео", callback_data="media:add_last_frame"))
    if max_audio_refs > 0:
        audio_text = f"🎵 Аудио: {audio_ref_count}/{max_audio_refs} ✅" if audio_ref_count else "🎵 Аудио"
        builder.row(InlineKeyboardButton(text=audio_text, callback_data="media:add_audio_ref"))
    if motion_orientation is not None:
        orient_label = "Фото" if motion_orientation == "image" else "Видео"
        builder.row(InlineKeyboardButton(
            text=f"🎯 Ориентация: {orient_label}",
            callback_data="media:toggle_orientation",
        ))
    if show_audio_toggle:
        audio_toggle_text = "🔊 Звуковое сопровождение: вкл" if audio_enabled else "🔇 Звуковое сопровождение: выкл"
        builder.row(InlineKeyboardButton(text=audio_toggle_text, callback_data="media:toggle_audio"))
    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    start_text = f"🚀 Начать генерацию — {cost_credits} ₽" if cost_credits else "🚀 Начать генерацию"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
    return builder.as_markup()


def video_format_kb(current: str, formats: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fmt in formats:
        prefix = "✅ " if fmt == current else ""
        builder.add(InlineKeyboardButton(text=f"{prefix}{fmt.upper()}", callback_data=f"media:format:{fmt}"))
    builder.adjust(len(formats))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def video_frames_menu_kb(
    has_first_frame: bool = False,
    has_last_frame: bool = False,
    motion_control: bool = False,
    extra_ref_count: int = 0,
    max_extra_refs: int = 0,
    show_first_frame: bool = True,
    show_last_frame: bool = True,
    show_video_refs: bool = False,
    max_video_refs: int = 0,
    video_ref_count: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if motion_control:
        first_text = "📎 Фото ✅" if has_first_frame else "📎 Фото"
        last_text = "📎 Видео ✅" if has_last_frame else "📎 Видео"
    else:
        first_text = "📎 Начало видео ✅" if has_first_frame else "📎 Начало видео"
        last_text = "📎 Конец видео ✅" if has_last_frame else "📎 Конец видео"
    if show_first_frame:
        if has_first_frame:
            builder.row(
                InlineKeyboardButton(text=first_text, callback_data="media:add_first_frame"),
                InlineKeyboardButton(text="🗑", callback_data="media:delete_first_frame"),
            )
            builder.row(InlineKeyboardButton(text="🔄 Заменить начало", callback_data="media:add_first_frame"))
        else:
            builder.row(InlineKeyboardButton(text=first_text, callback_data="media:add_first_frame"))
    if show_last_frame:
        if has_last_frame:
            builder.row(
                InlineKeyboardButton(text=last_text, callback_data="media:add_last_frame"),
                InlineKeyboardButton(text="🗑", callback_data="media:delete_last_frame"),
            )
            builder.row(InlineKeyboardButton(text="🔄 Заменить конец", callback_data="media:add_last_frame"))
        else:
            builder.row(InlineKeyboardButton(text=last_text, callback_data="media:add_last_frame"))
    if not motion_control and max_extra_refs > 0:
        extra_text = f"📷 Фото: {extra_ref_count}/{max_extra_refs} ✅" if extra_ref_count else "📷 Фото"
        if extra_ref_count > 0:
            builder.row(
                InlineKeyboardButton(text=extra_text, callback_data="media:add_extra_frames"),
                InlineKeyboardButton(text="🗑", callback_data="media:delete_extra_frames"),
            )
            builder.row(InlineKeyboardButton(text="🔄 Заменить фото", callback_data="media:replace_extra_frame"))
        else:
            builder.row(InlineKeyboardButton(text=extra_text, callback_data="media:add_extra_frames"))
    if show_video_refs and max_video_refs > 0:
        video_text = f"🎬 Видео: {video_ref_count}/{max_video_refs} ✅" if video_ref_count else "🎬 Видео"
        if video_ref_count > 0:
            builder.row(
                InlineKeyboardButton(text=video_text, callback_data="media:add_video_ref"),
                InlineKeyboardButton(text="🗑", callback_data="media:delete_video_refs"),
            )
            builder.row(InlineKeyboardButton(text="🔄 Заменить видео", callback_data="media:replace_video_ref"))
        else:
            builder.row(InlineKeyboardButton(text=video_text, callback_data="media:add_video_ref"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def video_duration_picker_kb(
    current: int,
    options: list[int],
    min_d: int,
    max_d: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # если текущее значение не в списке пресетов — добавляем его первым
    display_options = options if current in options else [current] + [d for d in options if d != current]
    for d in display_options:
        prefix = "✅ " if d == current else ""
        builder.add(InlineKeyboardButton(
            text=f"{prefix}{d} сек",
            callback_data=f"media:duration:{d}",
        ))
    builder.adjust(min(len(display_options), 3))
    builder.row(InlineKeyboardButton(
        text=f"✏️ Своя ({min_d}–{max_d} сек)",
        callback_data="media:duration_custom",
    ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


# ─── Карточка подтверждения: аудио ───────────────────────────────────────────

def audio_confirm_kb(
    has_prompt: bool = False,
    cost_credits: int | None = None,
    voice_label: str | None = None,
    style_ref_count: int = 0,
    max_style_refs: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if voice_label is not None:
        builder.row(InlineKeyboardButton(text=f"🗣 Голос: {voice_label}", callback_data="media:pick_voice"))
    if max_style_refs > 0:
        ref_text = f"🖼 Фото-ориентир: {style_ref_count} ✅" if style_ref_count else "📎 Добавить фото-ориентир"
        builder.row(InlineKeyboardButton(text=ref_text, callback_data="media:add_style_ref"))
    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    start_text = f"🚀 Начать генерацию — {cost_credits} ₽" if cost_credits else "🚀 Начать генерацию"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
    return builder.as_markup()


def voice_picker_kb(voices: list[dict], current_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for v in voices:
        prefix = "✅ " if v["id"] == current_id else ""
        builder.row(InlineKeyboardButton(
            text=f"{prefix}{v['label']}",
            callback_data=f"media:set_voice:{v['id']}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


_VOICE_STABILITY_PRESETS = [0.3, 0.5, 0.8]
_STABILITY_LABELS = {0.3: "Экспрессивный", 0.5: "Стандарт", 0.8: "Стабильный"}

_VOICE_LANGUAGES = [
    ("auto", "🌐 Авто"),
    ("ru", "🇷🇺 Русский"),
    ("en", "🇬🇧 English"),
    ("de", "🇩🇪 Deutsch"),
    ("fr", "🇫🇷 Français"),
    ("es", "🇪🇸 Español"),
    ("zh", "🇨🇳 中文"),
    ("ja", "🇯🇵 日本語"),
]


def voice_confirm_kb(data: dict, cost_credits: int | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    dialogue_mode = bool(data.get("voice_dialogue_mode"))
    stability = data.get("voice_stability", 0.5)
    language = data.get("voice_language", "auto")
    voice_label_1 = data.get("selected_voice_label") or "Стандартный"
    voice_label_2 = data.get("selected_voice_label_2") or "Стандартный"
    has_prompt = bool(data.get("prompt"))

    mode_text = "💬 Диалог: ВКЛ" if dialogue_mode else "💬 Диалог: ВЫКЛ"
    builder.row(InlineKeyboardButton(text=mode_text, callback_data="media:toggle_voice_dialogue"))

    if dialogue_mode:
        builder.row(
            InlineKeyboardButton(text=f"🗣 Голос 1: {voice_label_1}", callback_data="media:pick_voice"),
            InlineKeyboardButton(text=f"🗣 Голос 2: {voice_label_2}", callback_data="media:pick_voice_2"),
        )
    else:
        builder.row(InlineKeyboardButton(text=f"🗣 Голос: {voice_label_1}", callback_data="media:pick_voice"))

    stab_label = _STABILITY_LABELS.get(stability, f"{stability:.1f}")
    lang_label = dict(_VOICE_LANGUAGES).get(language, language)
    builder.row(
        InlineKeyboardButton(text=f"⚙️ {stab_label}", callback_data="media:cycle_voice_stability"),
        InlineKeyboardButton(text=lang_label, callback_data="media:pick_voice_language"),
    )

    edit_text = "✏️ Изменить текст" if has_prompt else "✏️ Ввести текст"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    start_text = f"🚀 Начать генерацию — {cost_credits} ₽" if cost_credits else "🚀 Начать генерацию"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
    return builder.as_markup()


def voice_language_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, label in _VOICE_LANGUAGES:
        prefix = "✅ " if code == current else ""
        builder.row(InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"media:set_voice_language:{code}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


_MUSIC_FORMATS = ["mp3_44100_128", "mp3_44100_192", "pcm_44100"]


def music_confirm_kb(data: dict, cost_credits: int | None = None) -> InlineKeyboardMarkup:
    """Карточка подтверждения для ElevenLabs Music с дополнительными настройками."""
    builder = InlineKeyboardBuilder()
    has_prompt = bool(data.get("prompt"))
    show_advanced = bool(data.get("music_show_advanced"))

    dur = data.get("music_duration", 30)
    builder.row(InlineKeyboardButton(
        text=f"⏱ Длительность: {dur} сек",
        callback_data="media:music_duration",
    ))

    if show_advanced:
        pos_styles = data.get("music_positive_styles") or []
        neg_styles = data.get("music_negative_styles") or []
        sections = data.get("music_sections") or []

        pos_text = f"🎼 Стили (+): {len(pos_styles)}" if pos_styles else "➕ Позит. стили"
        neg_text = f"🚫 Стили (-): {len(neg_styles)}" if neg_styles else "➕ Негат. стили"
        builder.row(
            InlineKeyboardButton(text=pos_text, callback_data="media:music_pos_styles"),
            InlineKeyboardButton(text=neg_text, callback_data="media:music_neg_styles"),
        )

        sec_text = f"📋 Секции: {len(sections)}" if sections else "📋 Добавить секции"
        builder.row(InlineKeyboardButton(text=sec_text, callback_data="media:music_sections"))

        instr = bool(data.get("music_instrumental"))
        respect = bool(data.get("music_respect_durations"))
        builder.row(
            InlineKeyboardButton(
                text=f"🎹 Инструментал: {'✅' if instr else '❌'}",
                callback_data="media:music_instrumental",
            ),
            InlineKeyboardButton(
                text=f"⏰ Длит. секций: {'✅' if respect else '❌'}",
                callback_data="media:music_respect_dur",
            ),
        )

        fmt = data.get("music_format", "mp3_44100_128")
        builder.row(InlineKeyboardButton(text=f"📁 Формат: {fmt}", callback_data="media:pick_music_format"))

    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )

    adv_text = "⚙️ Свернуть настройки ▲" if show_advanced else "⚙️ Дополнительные настройки ▼"
    builder.row(InlineKeyboardButton(text=adv_text, callback_data="media:music_adv"))

    start_text = f"🚀 Начать генерацию — {cost_credits} ₽" if cost_credits else "🚀 Начать генерацию"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
    return builder.as_markup()


def music_format_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fmt in _MUSIC_FORMATS:
        prefix = "✅ " if fmt == current else ""
        builder.add(InlineKeyboardButton(text=f"{prefix}{fmt}", callback_data=f"media:music_format:{fmt}"))
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


# ─── Карточка подтверждения: Udio ────────────────────────────────────────────

_UDIO_LYRICS_TYPES = [("generate", "Авто"), ("custom", "Свои тексты"), ("instrumental", "Инструментал")]
_UDIO_MODEL_TYPES = ["udio130-v1.5"]

_UDIO_FLOAT_FIELDS = {
    "prompt_strength": ("💪 Сила промпта", 0.5),
    "lyrics_strength": ("🎵 Сила лирики", 0.5),
    "generation_quality": ("✨ Качество", 0.75),
    "clarity_strength": ("🎯 Чёткость", 0.25),
    "lyrics_placement_start": ("📍 Нач. лирики", 0.2),
    "lyrics_placement_end": ("📍 Кон. лирики", 0.9),
}


def _udio_float_label(field: str, data: dict) -> str:
    label, default = _UDIO_FLOAT_FIELDS[field]
    val = data.get(f"udio_{field}", default)
    return f"{label}: {val:.2f}"


def udio_confirm_kb(data: dict, cost_credits: int | None = None) -> InlineKeyboardMarkup:
    """Карточка подтверждения для Udio с дополнительными настройками."""
    builder = InlineKeyboardBuilder()
    has_prompt = bool(data.get("prompt"))
    show_advanced = bool(data.get("udio_show_advanced"))

    if show_advanced:
        translate = "✅" if data.get("udio_translate_input") else "❌"
        builder.row(InlineKeyboardButton(
            text=f"🌐 Перевод ввода: {translate}",
            callback_data="media:udio_translate",
        ))

        lt_label = {"generate": "Авто", "custom": "Свои тексты", "instrumental": "Инструментал"}.get(
            data.get("udio_lyrics_type", "generate"), "Авто"
        )
        builder.row(InlineKeyboardButton(
            text=f"🎤 Тип лирики: {lt_label}",
            callback_data="media:udio_lyrics_type",
        ))

        if data.get("udio_lyrics_type") == "custom":
            has_lyrics = bool(data.get("udio_lyrics"))
            lyrics_text = "📝 Текст песни: задан ✅" if has_lyrics else "📝 Текст песни: не задан"
            builder.row(InlineKeyboardButton(text=lyrics_text, callback_data="media:udio_lyrics"))

        builder.row(
            InlineKeyboardButton(text=_udio_float_label("prompt_strength", data), callback_data="media:udio_float:prompt_strength"),
            InlineKeyboardButton(text=_udio_float_label("lyrics_strength", data), callback_data="media:udio_float:lyrics_strength"),
        )
        builder.row(
            InlineKeyboardButton(text=_udio_float_label("generation_quality", data), callback_data="media:udio_float:generation_quality"),
            InlineKeyboardButton(text=_udio_float_label("clarity_strength", data), callback_data="media:udio_float:clarity_strength"),
        )
        builder.row(
            InlineKeyboardButton(text=_udio_float_label("lyrics_placement_start", data), callback_data="media:udio_float:lyrics_placement_start"),
            InlineKeyboardButton(text=_udio_float_label("lyrics_placement_end", data), callback_data="media:udio_float:lyrics_placement_end"),
        )

        mt = data.get("udio_model_type", "udio130-v1.5")
        builder.row(InlineKeyboardButton(text=f"🎛 Модель: {mt}", callback_data="media:udio_model_type"))

    edit_text = "✏️ Изменить описание" if has_prompt else "✏️ Ввести описание"
    builder.row(
        InlineKeyboardButton(text=edit_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )

    adv_text = "⚙️ Свернуть настройки ▲" if show_advanced else "⚙️ Дополнительные настройки ▼"
    builder.row(InlineKeyboardButton(text=adv_text, callback_data="media:udio_adv"))

    start_text = f"🚀 Начать генерацию — {cost_credits} ₽" if cost_credits else "🚀 Начать генерацию"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
    return builder.as_markup()


def udio_lyrics_type_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in _UDIO_LYRICS_TYPES:
        prefix = "✅ " if value == current else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"media:udio_set_lyrics_type:{value}"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def udio_model_type_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for mt in _UDIO_MODEL_TYPES:
        prefix = "✅ " if mt == current else ""
        builder.row(InlineKeyboardButton(text=f"{prefix}{mt}", callback_data=f"media:udio_set_model:{mt}"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


# ─── Карточка подтверждения: редактирование ──────────────────────────────────

def edit_confirm_kb(
    has_prompt: bool = False,
    has_reference: bool = False,
    media_type: str = "photo_edit",
    style_ref_count: int = 0,
    max_style_refs: int = 0,
    cost_credits: int | None = None,
    video_edit_audio_mode: str | None = None,
    duration: int | None = None,
    duration_options: list[int] | None = None,
    aspect_ratio: str | None = None,
    has_aspect_ratios: bool = False,
    resolution: str | None = None,
    has_resolutions: bool = False,
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
    if media_type == "video_edit" and max_style_refs > 0:
        style_text = f"🖼 Ориентиры: {style_ref_count} фото ✅" if style_ref_count else "📎 Фото-ориентиры"
        builder.row(InlineKeyboardButton(text=style_text, callback_data="media:add_style_ref"))
    if media_type == "video_edit":
        if duration is not None and duration_options:
            builder.row(InlineKeyboardButton(
                text=f"⏱ Длительность: {duration} сек",
                callback_data="media:pick_duration",
            ))
        if has_aspect_ratios and has_resolutions and aspect_ratio and resolution:
            builder.row(
                InlineKeyboardButton(text=f"📐 Масштаб: {aspect_ratio}", callback_data="media:pick_ratio"),
                InlineKeyboardButton(text=f"🖼 Качество: {resolution}", callback_data="media:pick_resolution"),
            )
        elif has_aspect_ratios and aspect_ratio:
            builder.row(InlineKeyboardButton(text=f"📐 Масштаб: {aspect_ratio}", callback_data="media:pick_ratio"))
        elif has_resolutions and resolution:
            builder.row(InlineKeyboardButton(text=f"🖼 Качество: {resolution}", callback_data="media:pick_resolution"))
        if video_edit_audio_mode == "remove":
            sound_text = "🔕 Звук: убрать ✅"
        elif video_edit_audio_mode == "replace":
            sound_text = "🔊 Звук: заменить ✅"
        else:
            sound_text = "🔊 Звук"
        builder.row(InlineKeyboardButton(text=sound_text, callback_data="media:edit_sound"))
    prompt_text = "✏️ Изменить инструкцию" if has_prompt else "✏️ Ввести инструкцию"
    builder.row(
        InlineKeyboardButton(text=prompt_text, callback_data="media:edit_prompt"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:model"),
    )
    start_text = f"🚀 Начать обработку — {cost_credits} ₽" if cost_credits else "🚀 Начать обработку"
    builder.row(InlineKeyboardButton(text=start_text, callback_data="media:start"))
    return builder.as_markup()


def edit_sound_kb(audio_mode: str | None, audio_file_name: str | None = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора режима звука при редактировании видео."""
    builder = InlineKeyboardBuilder()
    if audio_mode == "remove":
        remove_text = "✅ Убрать звук"
    else:
        remove_text = "🔇 Убрать звук"
    builder.row(InlineKeyboardButton(text=remove_text, callback_data="media:edit_sound:remove"))
    if audio_mode == "remove":
        builder.row(InlineKeyboardButton(text="🔊 Заменить звук", callback_data="media:edit_sound:replace_blocked"))
    elif audio_mode == "replace" and audio_file_name:
        short_name = audio_file_name[:22] + "…" if len(audio_file_name) > 22 else audio_file_name
        builder.row(InlineKeyboardButton(text=f"✅ Заменить: {short_name}", callback_data="media:edit_sound:replace"))
    else:
        builder.row(InlineKeyboardButton(text="🔊 Заменить звук", callback_data="media:edit_sound:replace"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def back_to_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:confirm"))
    return builder.as_markup()


def back_to_frames_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:frames"))
    return builder.as_markup()


def video_ref_delete_kb() -> InlineKeyboardMarkup:
    """Клавиатура в режиме удаления конкретного видео-ориентира."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Удалить все", callback_data="media:delete_video_refs_all"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="media:back:frames"))
    return builder.as_markup()


# ─── После генерации ─────────────────────────────────────────────────────────

def error_kb() -> InlineKeyboardMarkup:
    """Клавиатура под сообщением об ошибке генерации."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔁 Повторить", callback_data="media:back:confirm"),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="media:back:menu"),
    )
    builder.row(
        InlineKeyboardButton(text="🔄 Начать заново", callback_data="media:again"),
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
