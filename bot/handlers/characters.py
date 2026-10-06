"""Персонажи в «Конструкторе видео»: Kling 3.0 / Kling 3.0 Omni («элементы») и Gemini Omni.

Персонаж живёт в состоянии текущей карточки (FSM, ключ "characters"), как и фото-ориентиры:
{"name", "tag", "description", "file_ids", "character_id"}.
- Kling: у KIE персонаж не хранится, в каждый запрос уходит элемент (name=tag, description, 2–4 фото),
  в описании на него ссылаются как @tag.
- Gemini Omni: при создании персонаж регистрируется у KIE (omni/character/create) → characterId,
  в запрос генерации уходит character_ids.
Тип определяет флаг models_config.yaml → characters: kling3 | kling_omni | gemini.
"""
import asyncio
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.main_menu import MENU_BUTTONS
from providers.base import ProviderError

logger = logging.getLogger(__name__)
router = Router()

MAX_CHARACTERS = 3
# сколько персонажей/ориентиров можно добавить (PixVerse — до 7 именованных ориентиров)
MAX_BY_KIND = {"kling3": 3, "kling_omni": 3, "gemini": 3, "pixverse": 7}
# тип → (мин., макс. фото на персонажа)
PHOTO_RULES = {"kling3": (2, 4), "kling_omni": (2, 4), "gemini": (1, 2), "pixverse": (1, 1)}
_REF_TYPES = {"subject": "Объект", "background": "Фон"}
_NAME_MAX = 30
_DESC_MAX = 300

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class CharacterStates(StatesGroup):
    name = State()
    desc = State()
    photos = State()
    # PixVerse: фото → тип (Subject / Background) → имя
    ref_photo = State()
    ref_type = State()
    ref_name = State()


def _is_kling(kind: str | None) -> bool:
    return str(kind or "").startswith("kling")


def _is_pixverse(kind: str | None) -> bool:
    return kind == "pixverse"


def _max_items(kind: str | None) -> int:
    return MAX_BY_KIND.get(kind or "", MAX_CHARACTERS)


def _make_tag(name: str, existing: list[str]) -> str:
    """ASCII-тег для ссылки @tag в промпте Kling (кириллицу в именах элементов не рискуем)."""
    raw = "".join(_TRANSLIT.get(ch, ch) for ch in name.lower())
    tag = "".join(ch if (ch.isascii() and ch.isalnum()) else "_" for ch in raw).strip("_")
    tag = tag[:20] or f"char{len(existing) + 1}"
    base, n = tag, 2
    while tag in existing:
        tag = f"{base}{n}"
        n += 1
    return tag


def _menu_text(data: dict) -> str:
    chars = data.get("characters") or []
    kind = data.get("model_characters")
    if _is_pixverse(kind):
        lines = ["🖼 <b>Ориентиры</b>\n"]
        if chars:
            for i, c in enumerate(chars, 1):
                lines.append(f"{i}. <code>@{c['tag']}</code> — {_REF_TYPES.get(c.get('type'), 'Объект')}")
            lines.append("")
        else:
            lines.append(f"Добавь до {_max_items(kind)} ориентиров: объект (Subject) или фон (Background).\n")
        lines.append("Чтобы использовать ориентир, укажи его в описании через @имя, например: "
                     "«<code>@dog бежит по @room</code>».")
        return "\n".join(lines)
    lines = ["👤 <b>Персонажи</b>\n"]
    if chars:
        for i, c in enumerate(chars, 1):
            tail = f" — <code>@{c['tag']}</code>" if _is_kling(kind) else ""
            lines.append(f"{i}. {c['name']}{tail}")
        lines.append("")
    else:
        lines.append(f"Создай до {MAX_CHARACTERS} персонажей: внешность сохранится в видео.\n")
    if _is_kling(kind):
        lines.append("Чтобы персонаж попал в видео, укажи его в описании через @тег, например: "
                     "«<code>@anya идёт по улице</code>».")
    else:
        lines.append("Упомяни персонажа в описании по имени — он появится в видео.")
    return "\n".join(lines)


def _menu_kb(data: dict) -> InlineKeyboardMarkup:
    chars = data.get("characters") or []
    kind = data.get("model_characters")
    b = InlineKeyboardBuilder()
    for i, c in enumerate(chars):
        b.row(InlineKeyboardButton(text=f"👤 {c['name']}", callback_data=f"char:manage:{i}"))
    if len(chars) < _max_items(kind):
        add_text = "➕ Добавить ориентир" if _is_pixverse(kind) else "➕ Создать персонажа"
        b.row(InlineKeyboardButton(text=add_text, callback_data="char:new"))
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="char:back"))
    return b.as_markup()


def _manage_kb(idx: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"char:del:{idx}"),
        InlineKeyboardButton(text="🔄 Заменить", callback_data=f"char:replace:{idx}"),
    )
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="char:menu"))
    return b.as_markup()


def _cancel_kb(back_to: str | None = None, forward_to: str | None = None) -> InlineKeyboardMarkup:
    """Навигация по шагам создания персонажа."""
    b = InlineKeyboardBuilder()
    if back_to:
        b.row(InlineKeyboardButton(text="⬅️ Шаг назад", callback_data=f"char:step:{back_to}"))
    if forward_to:
        b.row(InlineKeyboardButton(text="➡️ Шаг вперёд", callback_data=f"char:step:{forward_to}"))
    b.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="char:menu"))
    return b.as_markup()


def _photos_kb(count: int, minimum: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if count >= minimum:
        b.row(InlineKeyboardButton(text="✅ Готово", callback_data="char:done"))
    b.row(InlineKeyboardButton(text="⬅️ Шаг назад", callback_data="char:step:desc"))
    b.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="char:menu"))
    return b.as_markup()


def _name_text(kind: str | None, draft: dict | None = None) -> str:
    text = f"👤 <b>Шаг 1/3.</b> Введи имя персонажа (до {_NAME_MAX} символов)."
    if _is_kling(kind):
        text += (
            "\n\nДля ссылки в описании бот автоматически переведёт имя из кириллицы в латиницу "
            "и сделает тег: например, «Аня» → <code>@anya</code>. Готовый тег покажем после создания."
        )
    if draft and draft.get("name"):
        text += f"\n\nВаш текст: <code>{draft['name']}</code>"
    return text


def _desc_text(draft: dict | None = None) -> str:
    text = (
        "📝 <b>Шаг 2/3.</b> Коротко опиши персонажа: внешность, одежда, характер "
        "(например: «молодая девушка с короткими серебристыми волосами, в чёрной куртке»)."
    )
    if draft and draft.get("description"):
        text += f"\n\nВаш текст: <code>{draft['description']}</code>"
    return text


def _photos_text(kind: str, count: int) -> str:
    lo, hi = PHOTO_RULES[kind]
    if kind == "gemini":
        hint = ("Отправь портрет (чётко видно лицо). Вторым фото можно добавить снимок в полный рост.")
    elif kind == "kling3":
        hint = f"Отправь {lo}–{hi} фото персонажа с разных ракурсов."
    else:
        hint = f"Отправь {lo}–{hi} фото персонажа (лучше с разных ракурсов)."
    status = f"\n\nЗагружено: {count}/{hi}" + (" — нажми «Готово»." if count >= lo else f" (нужно минимум {lo}).")
    return f"📷 <b>Шаг 3/3.</b> {hint}{status}"


async def _edit(bot, chat_id: int, state: FSMContext, text: str, kb: InlineKeyboardMarkup) -> None:
    """Редактирует экран «Персонажи» (сообщение _sref_msg_id); если его нет — шлёт новое."""
    data = await state.get_data()
    msg_id = data.get("_sref_msg_id")
    if msg_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:  # not modified / message gone
            if "not modified" in str(e).lower():
                return
    sent = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    await state.update_data(_sref_msg_id=sent.message_id)


async def _show_menu(bot, chat_id: int, state: FSMContext) -> None:
    from bot.handlers.media import MediaStates
    await state.set_state(MediaStates.confirm)
    await state.update_data(_char_draft=None, _char_menu_active=True, _replacing_idx=None)
    data = await state.get_data()
    await _edit(bot, chat_id, state, _menu_text(data), _menu_kb(data))


async def _toast(message: Message, text: str, delay: float = 5.0) -> None:
    from bot.handlers.media import _toast as media_toast
    await media_toast(message, text, delay=delay)


# ─── Меню ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "char:menu")
async def open_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("model_characters"):
        await callback.answer("Эта модель не поддерживает персонажей", show_alert=True)
        return
    await state.update_data(_sref_msg_id=callback.message.message_id)
    await _show_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer()


@router.callback_query(F.data == "char:back")
async def back_to_constructor(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.handlers.media import _back_to_frames_menu
    await state.update_data(_char_draft=None, _char_menu_active=None, _replacing_idx=None)
    await _back_to_frames_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer()


@router.callback_query(F.data.startswith("char:manage:"))
async def manage_character(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает меню управления конкретным персонажем: удалить / заменить."""
    data = await state.get_data()
    try:
        idx = int(callback.data.split(":")[2])
        chars = data.get("characters") or []
        char = chars[idx]
    except (ValueError, IndexError):
        await callback.answer()
        return
    label = "ориентир" if _is_pixverse(data.get("model_characters")) else "персонаж"
    await callback.message.edit_text(
        f"👤 <b>{char['name']}</b>\n\nЧто сделать с этим {label}ем?",
        parse_mode="HTML",
        reply_markup=_manage_kb(idx),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("char:del:"))
async def delete_character(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    chars = list(data.get("characters") or [])
    try:
        idx = int(callback.data.split(":")[2])
        chars.pop(idx)
    except (ValueError, IndexError):
        await callback.answer()
        return
    await state.update_data(characters=chars)
    await _show_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer("Персонаж удалён")


@router.callback_query(F.data.startswith("char:replace:"))
async def replace_character(callback: CallbackQuery, state: FSMContext) -> None:
    """Запускает создание нового персонажа на замену; старый удаляется только при нажатии «Готово»."""
    data = await state.get_data()
    chars = list(data.get("characters") or [])
    try:
        idx = int(callback.data.split(":")[2])
        if idx < 0 or idx >= len(chars):
            raise IndexError
    except (ValueError, IndexError):
        await callback.answer()
        return
    kind = data.get("model_characters")
    # Сохраняем индекс замены — старый персонаж пока остаётся в списке
    await state.update_data(_sref_msg_id=callback.message.message_id, _char_draft={"file_ids": []},
                            _replacing_idx=idx)
    if _is_pixverse(kind):
        await state.set_state(CharacterStates.ref_photo)
        await callback.message.edit_text(_REF_PHOTO_TEXT, parse_mode="HTML", reply_markup=_cancel_kb())
    else:
        await state.set_state(CharacterStates.name)
        await callback.message.edit_text(
            _name_text(kind), parse_mode="HTML", reply_markup=_cancel_kb(),
        )
    await callback.answer("Создай нового персонажа на замену")


# ─── Создание: имя → описание → фото ─────────────────────────────────────────

@router.callback_query(F.data == "char:new")
async def new_character(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data.get("model_characters")
    if len(data.get("characters") or []) >= _max_items(kind):
        await callback.answer(f"Максимум {_max_items(kind)}", show_alert=True)
        return
    await state.update_data(_sref_msg_id=callback.message.message_id, _char_draft={"file_ids": []})
    if _is_pixverse(kind):
        await state.set_state(CharacterStates.ref_photo)
        await callback.message.edit_text(_REF_PHOTO_TEXT, parse_mode="HTML", reply_markup=_cancel_kb())
        await callback.answer()
        return
    await state.set_state(CharacterStates.name)
    await callback.message.edit_text(
        _name_text(data.get("model_characters")), parse_mode="HTML", reply_markup=_cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"char:step:name", "char:step:desc", "char:step:photos"}))
async def step_nav(callback: CallbackQuery, state: FSMContext) -> None:
    """Навигация назад/вперёд по шагам создания персонажа."""
    data = await state.get_data()
    draft = data.get("_char_draft") or {}
    kind = data.get("model_characters") or ""
    await state.update_data(_sref_msg_id=callback.message.message_id)

    if callback.data.endswith(":name"):
        await state.set_state(CharacterStates.name)
        await _edit(callback.bot, callback.message.chat.id, state,
                    _name_text(kind, draft), _cancel_kb(forward_to="desc"))
    elif callback.data.endswith(":desc"):
        await state.set_state(CharacterStates.desc)
        await _edit(callback.bot, callback.message.chat.id, state,
                    _desc_text(draft), _cancel_kb("name", "photos"))
    else:  # :photos
        await state.set_state(CharacterStates.photos)
        count = len(draft.get("file_ids") or [])
        lo = PHOTO_RULES[kind][0]
        await _edit(callback.bot, callback.message.chat.id, state,
                    _photos_text(kind, count), _photos_kb(count, lo))
    await callback.answer()


@router.message(CharacterStates.name, F.text, ~F.text.in_(MENU_BUTTONS))
async def enter_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not name or len(name) > _NAME_MAX:
        asyncio.create_task(_toast(message, f"⚠️ Имя должно быть от 1 до {_NAME_MAX} символов."))
        return
    data = await state.get_data()
    existing = [c["tag"] for c in data.get("characters") or []]
    draft = dict(data.get("_char_draft") or {"file_ids": []})
    draft.update(name=name, tag=_make_tag(name, existing))
    await state.update_data(_char_draft=draft)
    await state.set_state(CharacterStates.desc)
    await _edit(message.bot, message.chat.id, state, _desc_text(draft), _cancel_kb("name"))


@router.message(CharacterStates.desc, F.text, ~F.text.in_(MENU_BUTTONS))
async def enter_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not desc or len(desc) > _DESC_MAX:
        asyncio.create_task(_toast(message, f"⚠️ Описание должно быть от 1 до {_DESC_MAX} символов."))
        return
    data = await state.get_data()
    draft = dict(data.get("_char_draft") or {"file_ids": []})
    draft["description"] = desc
    await state.update_data(_char_draft=draft)
    await state.set_state(CharacterStates.photos)
    kind = data["model_characters"]
    await _edit(message.bot, message.chat.id, state, _photos_text(kind, 0), _photos_kb(0, PHOTO_RULES[kind][0]))


@router.message(CharacterStates.photos, F.photo)
async def receive_photos(message: Message, state: FSMContext, album: list | None = None) -> None:
    data = await state.get_data()
    kind = data["model_characters"]
    lo, hi = PHOTO_RULES[kind]
    msgs = album or [message]
    draft = dict(data.get("_char_draft") or {"file_ids": []})
    ids = list(draft.get("file_ids") or [])
    accepted = 0
    for m in msgs:
        if m.photo and len(ids) < hi:
            ids.append(m.photo[-1].file_id)
            accepted += 1
        try:
            await m.delete()
        except Exception:
            pass
    draft["file_ids"] = ids
    await state.update_data(_char_draft=draft)
    if accepted < sum(1 for m in msgs if m.photo):
        asyncio.create_task(_toast(message, f"⚠️ Максимум {hi} фото на персонажа — лишние не добавлены."))
    await _edit(message.bot, message.chat.id, state, _photos_text(kind, len(ids)), _photos_kb(len(ids), lo))


@router.message(CharacterStates.name, ~F.text.in_(MENU_BUTTONS))
@router.message(CharacterStates.desc, ~F.text.in_(MENU_BUTTONS))
async def wrong_text_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    asyncio.create_task(_toast(message, "⚠️ Здесь нужно прислать текст."))


@router.message(CharacterStates.photos, ~F.text.in_(MENU_BUTTONS))
async def wrong_photo_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    asyncio.create_task(_toast(message, "⚠️ Здесь нужно прислать фото 📷."))


@router.callback_query(CharacterStates.photos, F.data == "char:done")
async def finish_character(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.handlers.media import _tg_file_url
    data = await state.get_data()
    kind = data["model_characters"]
    draft = data.get("_char_draft") or {}
    file_ids = draft.get("file_ids") or []
    lo, _ = PHOTO_RULES[kind]
    if not draft.get("name"):
        await callback.answer("⚠️ Сначала заполни имя персонажа (шаг 1)", show_alert=True)
        return
    if not draft.get("description"):
        await callback.answer("⚠️ Сначала заполни описание персонажа (шаг 2)", show_alert=True)
        return
    if len(file_ids) < lo:
        await callback.answer(f"⚠️ Нужно минимум {lo} фото (шаг 3)", show_alert=True)
        return

    character = {
        "name": draft["name"], "tag": draft["tag"], "description": draft["description"],
        "file_ids": file_ids, "character_id": None,
    }
    if kind == "gemini":
        # Gemini Omni: персонаж регистрируется у KIE прямо сейчас (быстро и бесплатно)
        await callback.answer()
        await _edit(callback.bot, callback.message.chat.id, state, "⏳ Создаю персонажа…", InlineKeyboardMarkup(inline_keyboard=[]))
        try:
            from providers.router import _registry
            urls = [u for u in [await _tg_file_url(callback.bot, fid) for fid in file_ids] if u]
            character["character_id"] = await _registry["kie"].create_omni_character(
                character["name"], character["description"], urls,
            )
        except ProviderError as e:
            logger.error("Gemini character create failed: %s", e)
            draft["file_ids"] = []
            await state.update_data(_char_draft=draft)
            await _edit(
                callback.bot, callback.message.chat.id, state,
                "⚠️ Не удалось создать персонажа. Убедись, что на фото чётко видно лицо, и отправь фото заново.\n\n"
                + _photos_text(kind, 0),
                _photos_kb(0, lo),
            )
            return
    else:
        await callback.answer()

    chars = list(data.get("characters") or [])
    replacing_idx = data.get("_replacing_idx")
    if replacing_idx is not None and 0 <= replacing_idx < len(chars):
        chars[replacing_idx] = character
    else:
        chars.append(character)
    await state.update_data(characters=chars, _replacing_idx=None)
    await _show_menu(callback.bot, callback.message.chat.id, state)


# ─── PixVerse: именованные ориентиры (фото → тип → имя) ───────────────────────

_REF_PHOTO_TEXT = "🖼 <b>Шаг 1/3.</b> Отправь фото ориентира (JPG, PNG или WebP, до 20 МБ)."
_REF_TYPE_TEXT = (
    "🎯 <b>Шаг 2/3.</b> Выбери тип ориентира:\n\n"
    "• <b>Объект</b> (Subject) — персонаж, предмет или животное, которые должны быть в видео;\n"
    "• <b>Фон</b> (Background) — место, где происходит действие."
)


def _ref_name_text() -> str:
    return (
        f"✏️ <b>Шаг 3/3.</b> Введи имя ориентира (до {_NAME_MAX} символов): на него можно будет "
        "сослаться в описании как @имя. Кириллица автоматически переводится в латиницу, "
        "например «собака» → <code>@sobaka</code>. Или нажми «Пропустить»."
    )


def _ref_type_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🧍 Объект", callback_data="char:rtype:subject"),
        InlineKeyboardButton(text="🏞 Фон", callback_data="char:rtype:background"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Шаг назад", callback_data="char:step:rphoto"))
    b.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="char:menu"))
    return b.as_markup()


def _ref_name_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⏭ Пропустить", callback_data="char:rname:skip"))
    b.row(InlineKeyboardButton(text="⬅️ Шаг назад", callback_data="char:step:rtype"))
    b.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="char:menu"))
    return b.as_markup()


@router.message(CharacterStates.ref_photo, F.photo)
async def ref_receive_photo(message: Message, state: FSMContext, album: list | None = None) -> None:
    msgs = album or [message]
    photos = [m for m in msgs if m.photo]
    for m in msgs:
        try:
            await m.delete()
        except Exception:
            pass
    data = await state.get_data()
    draft = dict(data.get("_char_draft") or {})
    draft["file_ids"] = [photos[0].photo[-1].file_id]
    await state.update_data(_char_draft=draft)
    if len(photos) > 1:
        asyncio.create_task(_toast(message, "⚠️ Один ориентир — одно фото. Взято первое, остальные добавляй по очереди."))
    await state.set_state(CharacterStates.ref_type)
    await _edit(message.bot, message.chat.id, state, _REF_TYPE_TEXT, _ref_type_kb())


@router.callback_query(CharacterStates.ref_type, F.data.in_({"char:rtype:subject", "char:rtype:background"}))
async def ref_choose_type(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = dict(data.get("_char_draft") or {})
    draft["type"] = callback.data.split(":")[2]
    await state.update_data(_char_draft=draft, _sref_msg_id=callback.message.message_id)
    await state.set_state(CharacterStates.ref_name)
    await _edit(callback.bot, callback.message.chat.id, state, _ref_name_text(), _ref_name_kb())
    await callback.answer()


async def _finish_ref(bot, chat_id: int, state: FSMContext, name: str | None) -> None:
    data = await state.get_data()
    draft = data.get("_char_draft") or {}
    chars = list(data.get("characters") or [])
    if not draft.get("file_ids"):
        await _show_menu(bot, chat_id, state)
        return
    existing = [c["tag"] for c in chars]
    name = name or f"image{len(chars) + 1}"
    tag = _make_tag(name, existing)
    chars.append({
        "name": name, "tag": tag, "type": draft.get("type", "subject"), "description": "",
        "file_ids": draft["file_ids"], "character_id": None,
    })
    await state.update_data(characters=chars)
    await _show_menu(bot, chat_id, state)


@router.callback_query(CharacterStates.ref_name, F.data == "char:rname:skip")
async def ref_skip_name(callback: CallbackQuery, state: FSMContext) -> None:
    await _finish_ref(callback.bot, callback.message.chat.id, state, None)
    await callback.answer()


@router.message(CharacterStates.ref_name, F.text, ~F.text.in_(MENU_BUTTONS))
async def ref_enter_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not name or len(name) > _NAME_MAX:
        asyncio.create_task(_toast(message, f"⚠️ Имя должно быть от 1 до {_NAME_MAX} символов."))
        return
    await _finish_ref(message.bot, message.chat.id, state, name)


@router.callback_query(F.data.in_({"char:step:rphoto", "char:step:rtype"}))
async def ref_step_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(_sref_msg_id=callback.message.message_id)
    if callback.data.endswith(":rphoto"):
        await state.set_state(CharacterStates.ref_photo)
        await _edit(callback.bot, callback.message.chat.id, state, _REF_PHOTO_TEXT, _cancel_kb())
    else:
        await state.set_state(CharacterStates.ref_type)
        await _edit(callback.bot, callback.message.chat.id, state, _REF_TYPE_TEXT, _ref_type_kb())
    await callback.answer()


@router.message(CharacterStates.ref_photo, ~F.text.in_(MENU_BUTTONS))
@router.message(CharacterStates.ref_type, ~F.text.in_(MENU_BUTTONS))
@router.message(CharacterStates.ref_name, ~F.text.in_(MENU_BUTTONS))
async def ref_wrong_input(message: Message, state: FSMContext) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    cur = await state.get_state()
    hint = {
        CharacterStates.ref_photo.state: "⚠️ Здесь нужно прислать фото 📷.",
        CharacterStates.ref_type.state: "⚠️ Выбери тип кнопкой: Объект или Фон.",
    }.get(cur, "⚠️ Здесь нужно прислать текст.")
    asyncio.create_task(_toast(message, hint))


# ─── Для генерации ───────────────────────────────────────────────────────────

def characters_start_error(data: dict, prompt: str) -> str | None:
    """Проверки перед запуском: ссылка @тег в описании и лимиты KIE. None — всё в порядке."""
    chars = data.get("characters") or []
    kind = data.get("model_characters")
    if not chars or not kind:
        return None
    low = (prompt or "").lower()
    refs = len(data.get("style_reference_file_ids") or [])
    vids = len(data.get("video_style_reference_file_ids") or [])
    if _is_pixverse(kind):
        if data.get("video_first_frame_file_id") or data.get("video_last_frame_file_id"):
            return "Ориентиры нельзя сочетать с началом/концом видео — убери кадры или ориентиры."
        return None
    if _is_kling(kind):
        missing = [c["tag"] for c in chars if f"@{c['tag']}".lower() not in low]
        if missing:
            return "Укажи персонажа в описании: " + ", ".join(f"@{t}" for t in missing)
        if kind == "kling_omni":
            limit = 4 if vids else 7
            if refs + len(chars) > limit:
                return f"Слишком много материалов: фото-ориентиры + персонажи не больше {limit}."
    else:
        if data.get("video_first_frame_file_id") or data.get("video_last_frame_file_id"):
            return "Персонажи нельзя сочетать с началом/концом видео — убери кадры или персонажей."
        units = refs + 2 * vids + sum(2 if len(c.get("file_ids") or []) >= 2 else 1 for c in chars)
        if units > 7:
            return "Слишком много материалов для Gemini Omni: уменьши число фото-ориентиров или персонажей."
    return None


async def build_character_payload(bot, data: dict) -> list[dict] | None:
    """Персонажи в формате для KieProvider.generate_video."""
    from bot.handlers.media import _tg_file_url
    chars = data.get("characters") or []
    kind = data.get("model_characters")
    if not chars or not kind:
        return None
    if _is_pixverse(kind):
        payload = []
        for c in chars:
            url = await _tg_file_url(bot, c["file_ids"][0])
            if url:
                payload.append({"image_url": url, "type": c.get("type", "subject"), "ref_name": c["tag"]})
        return payload or None
    if kind == "gemini":
        return [{"character_id": c["character_id"]} for c in chars if c.get("character_id")] or None
    payload = []
    for c in chars:
        urls = [u for u in [await _tg_file_url(bot, fid) for fid in c["file_ids"]] if u]
        payload.append({"name": c["tag"], "description": c["description"], "element_input_urls": urls})
    return payload
