"""«Несколько сцен» для Kling 3.0: до 5 сцен со своим текстом и длительностью (multi_prompt у KIE),
суммарно не больше model_max_duration секунд. Переключатель живёт в FSM ("shots"), как и
персонажи/фото-ориентиры в characters.py — тот же паттерн (меню остаётся в MediaStates.confirm,
создание сцены — отдельное состояние с текстовым шагом и выбором длительности по кнопкам).
"""
import asyncio
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.main_menu import MENU_BUTTONS

logger = logging.getLogger(__name__)
router = Router()

_PROMPT_MAX = 500
_DURATION_PRESETS = [2, 3, 5, 8, 10]


class ShotStates(StatesGroup):
    prompt = State()
    delete_number = State()
    edit_number = State()


def _max_shots(data: dict) -> int:
    return data.get("model_max_shots", 0)


def _total_duration(shots: list[dict]) -> int:
    return sum(s.get("duration", 0) for s in shots)


def _remaining(data: dict, exclude_idx: int | None = None) -> int:
    max_total = data.get("model_max_duration", 15)
    shots = list(data.get("shots") or [])
    if exclude_idx is not None and 0 <= exclude_idx < len(shots):
        shots.pop(exclude_idx)
    return max(0, max_total - _total_duration(shots))


def _menu_text(data: dict) -> str:
    shots = data.get("shots") or []
    total = _total_duration(shots)
    max_total = data.get("model_max_duration", 15)
    lines = [f"🎬 <b>Несколько сцен</b> ({len(shots)}/{_max_shots(data)}, {total}/{max_total} сек)\n"]
    if shots:
        for i, s in enumerate(shots, 1):
            text = s.get("prompt", "")
            short = text if len(text) <= 60 else text[:57] + "…"
            lines.append(f"{i}. {short} — {s.get('duration')} сек")
        lines.append("")
    else:
        lines.append("Раздели видео на отдельные сцены — у каждой свой текст и длительность.\n")
    lines.append(
        "Видео соберётся именно из этих сцен по порядку. Общее описание в карточке "
        "при этом не используется — у каждой сцены свой текст."
    )
    return "\n".join(lines)


def _menu_kb(data: dict) -> InlineKeyboardMarkup:
    shots = data.get("shots") or []
    b = InlineKeyboardBuilder()
    if len(shots) < _max_shots(data) and _remaining(data) > 0:
        b.row(InlineKeyboardButton(text="➕ Добавить сцену", callback_data="shots:new"))
    if shots:
        b.row(InlineKeyboardButton(text="✏️ Изменить сцену №…", callback_data="shots:edit_pick"))
        b.row(InlineKeyboardButton(text="🗑 Удалить последнюю", callback_data="shots:del_last"))
        if len(shots) > 1:
            b.row(InlineKeyboardButton(text="🗑 Удалить сцену №…", callback_data="shots:del_pick"))
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="shots:back"))
    return b.as_markup()


def _duration_kb(remaining: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    options = sorted({d for d in _DURATION_PRESETS if d <= remaining} | ({remaining} if remaining > 0 else set()))
    for d in options:
        b.add(InlineKeyboardButton(text=f"{d} сек", callback_data=f"shots:dur:{d}"))
    b.adjust(min(len(options), 3) or 1)
    b.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="shots:menu"))
    return b.as_markup()


async def _edit(bot, chat_id: int, state: FSMContext, text: str, kb: InlineKeyboardMarkup) -> None:
    """Редактирует экран «Несколько сцен» (сообщение _sref_msg_id); если его нет — шлёт новое."""
    data = await state.get_data()
    msg_id = data.get("_sref_msg_id")
    if msg_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    sent = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    await state.update_data(_sref_msg_id=sent.message_id)


async def _toast(message: Message, text: str, delay: float = 5.0) -> None:
    from bot.handlers.media import _toast as media_toast
    await media_toast(message, text, delay=delay)


async def _show_menu(bot, chat_id: int, state: FSMContext) -> None:
    from bot.handlers.media import MediaStates
    await state.set_state(MediaStates.confirm)
    data = await state.get_data()
    await _edit(bot, chat_id, state, _menu_text(data), _menu_kb(data))


@router.callback_query(F.data == "shots:menu")
async def open_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not _max_shots(data):
        await callback.answer("Эта модель не поддерживает сцены", show_alert=True)
        return
    await state.update_data(_sref_msg_id=callback.message.message_id, _shot_edit_idx=None)
    await _show_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer()


@router.callback_query(F.data == "shots:back")
async def back_to_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.handlers.media import _confirm_card_text, _confirm_kb
    data = await state.get_data()
    await callback.message.edit_text(_confirm_card_text(data), parse_mode="HTML", reply_markup=_confirm_kb(data))
    await callback.answer()


@router.callback_query(F.data == "shots:new")
async def new_shot(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    shots = data.get("shots") or []
    if len(shots) >= _max_shots(data):
        await callback.answer(f"Максимум {_max_shots(data)} сцен", show_alert=True)
        return
    if _remaining(data) <= 0:
        await callback.answer("Длительность сцен уже достигла лимита", show_alert=True)
        return
    await state.update_data(_sref_msg_id=callback.message.message_id, _shot_edit_idx=None)
    await state.set_state(ShotStates.prompt)
    n = len(shots) + 1
    await callback.message.edit_text(
        f"✏️ <b>Сцена {n}.</b> Опиши, что происходит в этой сцене (до {_PROMPT_MAX} символов).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="shots:menu")],
        ]),
    )
    await callback.answer()


@router.message(ShotStates.prompt, F.text, ~F.text.in_(MENU_BUTTONS))
async def enter_shot_prompt(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not text or len(text) > _PROMPT_MAX:
        asyncio.create_task(_toast(message, f"⚠️ Текст сцены должен быть от 1 до {_PROMPT_MAX} символов."))
        return
    data = await state.get_data()
    await state.update_data(_shot_draft={"prompt": text})
    remaining = _remaining(data, exclude_idx=data.get("_shot_edit_idx"))
    await _edit(
        message.bot, message.chat.id, state,
        f"⏱ Сколько секунд длится эта сцена? (осталось {remaining} сек из {data.get('model_max_duration', 15)})",
        _duration_kb(remaining),
    )


@router.message(ShotStates.prompt, ~F.text.in_(MENU_BUTTONS))
async def wrong_shot_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    asyncio.create_task(_toast(message, "⚠️ Здесь нужно прислать текст."))


@router.callback_query(ShotStates.prompt, F.data.startswith("shots:dur:"))
async def pick_duration(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("_shot_draft") or {}
    if not draft.get("prompt"):
        await callback.answer()
        return
    try:
        duration = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback.answer()
        return
    edit_idx = data.get("_shot_edit_idx")
    if duration <= 0 or duration > _remaining(data, exclude_idx=edit_idx):
        await callback.answer("Недопустимая длительность", show_alert=True)
        return
    shots = list(data.get("shots") or [])
    if edit_idx is not None and 0 <= edit_idx < len(shots):
        shots[edit_idx] = {"prompt": draft["prompt"], "duration": duration}
    else:
        shots.append({"prompt": draft["prompt"], "duration": duration})
    await state.update_data(shots=shots, _shot_draft=None, _shot_edit_idx=None, duration=_total_duration(shots))
    await _show_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer()


@router.callback_query(F.data == "shots:del_last")
async def delete_last(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    shots = list(data.get("shots") or [])
    had = bool(shots)
    if shots:
        shots.pop()
    new_duration = _total_duration(shots) if shots else data.get("model_min_duration", 5)
    await state.update_data(shots=shots, duration=new_duration)
    await _show_menu(callback.bot, callback.message.chat.id, state)
    await callback.answer("Сцена удалена" if had else "")


@router.callback_query(F.data == "shots:del_pick")
async def delete_pick_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    shots = data.get("shots") or []
    if len(shots) < 2:
        await callback.answer()
        return
    await state.update_data(_sref_msg_id=callback.message.message_id)
    await state.set_state(ShotStates.delete_number)
    await callback.message.edit_text(
        f"🗑 <b>Какую сцену удалить?</b> Введи номер от 1 до {len(shots)}.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="shots:menu")],
        ]),
    )
    await callback.answer()


@router.message(ShotStates.delete_number, F.text, ~F.text.in_(MENU_BUTTONS))
async def delete_pick_number(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    shots = list(data.get("shots") or [])
    if not text.isdigit() or not (1 <= int(text) <= len(shots)):
        asyncio.create_task(_toast(message, f"⚠️ Введи число от 1 до {len(shots)}."))
        return
    idx = int(text) - 1
    shots.pop(idx)
    new_duration = _total_duration(shots) if shots else data.get("model_min_duration", 5)
    await state.update_data(shots=shots, duration=new_duration)
    await _show_menu(message.bot, message.chat.id, state)


@router.message(ShotStates.delete_number, ~F.text.in_(MENU_BUTTONS))
async def delete_pick_wrong_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    asyncio.create_task(_toast(message, "⚠️ Здесь нужно прислать число."))


async def _start_edit_shot(bot, chat_id: int, state: FSMContext, idx: int) -> None:
    """Открывает экран ввода нового текста для сцены idx (0-based)."""
    data = await state.get_data()
    shots = data.get("shots") or []
    n = idx + 1
    current = shots[idx].get("prompt", "") if idx < len(shots) else ""
    await state.update_data(_shot_edit_idx=idx)
    await state.set_state(ShotStates.prompt)
    short = current if len(current) <= 200 else current[:197] + "…"
    await _edit(
        bot, chat_id, state,
        f"✏️ <b>Сцена {n}.</b> Текущий текст: «{short}».\n"
        f"Отправь новый текст сцены (до {_PROMPT_MAX} символов).",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="shots:menu")],
        ]),
    )


@router.callback_query(F.data == "shots:edit_pick")
async def edit_pick_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    shots = data.get("shots") or []
    if not shots:
        await callback.answer()
        return
    await state.update_data(_sref_msg_id=callback.message.message_id)
    if len(shots) == 1:
        await _start_edit_shot(callback.bot, callback.message.chat.id, state, 0)
        await callback.answer()
        return
    await state.set_state(ShotStates.edit_number)
    await callback.message.edit_text(
        f"✏️ <b>Какую сцену изменить?</b> Введи номер от 1 до {len(shots)}.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="shots:menu")],
        ]),
    )
    await callback.answer()


@router.message(ShotStates.edit_number, F.text, ~F.text.in_(MENU_BUTTONS))
async def edit_pick_number(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    shots = data.get("shots") or []
    if not text.isdigit() or not (1 <= int(text) <= len(shots)):
        asyncio.create_task(_toast(message, f"⚠️ Введи число от 1 до {len(shots)}."))
        return
    await _start_edit_shot(message.bot, message.chat.id, state, int(text) - 1)


@router.message(ShotStates.edit_number, ~F.text.in_(MENU_BUTTONS))
async def edit_pick_wrong_input(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
    asyncio.create_task(_toast(message, "⚠️ Здесь нужно прислать число."))
