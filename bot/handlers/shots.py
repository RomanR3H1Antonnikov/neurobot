"""«Кадры» для Kling 3.0: до 5 кадров со своим текстом и длительностью (multi_prompt у KIE),
суммарно не больше model_max_duration секунд. Переключатель живёт в FSM ("shots"), как и
персонажи/фото-ориентиры в characters.py — тот же паттерн (меню остаётся в MediaStates.confirm,
создание кадра — отдельное состояние с текстовым шагом и выбором длительности по кнопкам).
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


def _max_shots(data: dict) -> int:
    return data.get("model_max_shots", 0)


def _total_duration(shots: list[dict]) -> int:
    return sum(s.get("duration", 0) for s in shots)


def _remaining(data: dict) -> int:
    max_total = data.get("model_max_duration", 15)
    return max(0, max_total - _total_duration(data.get("shots") or []))


def _menu_text(data: dict) -> str:
    shots = data.get("shots") or []
    total = _total_duration(shots)
    max_total = data.get("model_max_duration", 15)
    lines = [f"🎬 <b>Кадры</b> ({len(shots)}/{_max_shots(data)}, {total}/{max_total} сек)\n"]
    if shots:
        for i, s in enumerate(shots, 1):
            text = s.get("prompt", "")
            short = text if len(text) <= 60 else text[:57] + "…"
            lines.append(f"{i}. {short} — {s.get('duration')} сек")
        lines.append("")
    else:
        lines.append("Раздели видео на отдельные кадры — у каждого свой текст и длительность.\n")
    lines.append(
        "Видео соберётся именно из этих кадров по порядку. Основной текст в карточке "
        "остаётся как общее описание сцены — он отправляется вместе с кадрами."
    )
    return "\n".join(lines)


def _menu_kb(data: dict) -> InlineKeyboardMarkup:
    shots = data.get("shots") or []
    b = InlineKeyboardBuilder()
    if len(shots) < _max_shots(data) and _remaining(data) > 0:
        b.row(InlineKeyboardButton(text="➕ Добавить кадр", callback_data="shots:new"))
    if shots:
        b.row(InlineKeyboardButton(text="🗑 Удалить последний", callback_data="shots:del_last"))
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
    """Редактирует экран «Кадры» (сообщение _sref_msg_id); если его нет — шлёт новое."""
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
        await callback.answer("Эта модель не поддерживает кадры", show_alert=True)
        return
    await state.update_data(_sref_msg_id=callback.message.message_id)
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
        await callback.answer(f"Максимум {_max_shots(data)} кадров", show_alert=True)
        return
    if _remaining(data) <= 0:
        await callback.answer("Длительность кадров уже достигла лимита", show_alert=True)
        return
    await state.update_data(_sref_msg_id=callback.message.message_id)
    await state.set_state(ShotStates.prompt)
    n = len(shots) + 1
    await callback.message.edit_text(
        f"✏️ <b>Кадр {n}.</b> Опиши, что происходит в этом кадре (до {_PROMPT_MAX} символов).",
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
        asyncio.create_task(_toast(message, f"⚠️ Текст кадра должен быть от 1 до {_PROMPT_MAX} символов."))
        return
    data = await state.get_data()
    await state.update_data(_shot_draft={"prompt": text})
    remaining = _remaining(data)
    await _edit(
        message.bot, message.chat.id, state,
        f"⏱ Сколько секунд длится этот кадр? (осталось {remaining} сек из {data.get('model_max_duration', 15)})",
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
    if duration <= 0 or duration > _remaining(data):
        await callback.answer("Недопустимая длительность", show_alert=True)
        return
    shots = list(data.get("shots") or [])
    shots.append({"prompt": draft["prompt"], "duration": duration})
    await state.update_data(shots=shots, _shot_draft=None, duration=_total_duration(shots))
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
    await callback.answer("Кадр удалён" if had else "")
