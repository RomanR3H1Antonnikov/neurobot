import logging
from aiogram import Router, F

logger = logging.getLogger(__name__)
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_CHAT, BTN_EXIT_CHAT, BTN_NEW_DIALOG, BTN_CHAT_PICK_MODEL, MENU_BUTTONS, main_menu_kb, inline_main_menu_kb
from bot.utils import cleanup_tracked_messages, safe_delete
from bot.keyboards.billing import quick_topup_kb
from providers.base import ProviderError, TaskType
from providers.router import get_models_for_task
from services import chat_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()


class ChatStates(StatesGroup):
    select_model = State()
    active = State()


def chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW_DIALOG), KeyboardButton(text=BTN_CHAT_PICK_MODEL)],
            [KeyboardButton(text=BTN_EXIT_CHAT)],
        ],
        resize_keyboard=True,
    )


def _chat_model_kb(models: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for m in models:
        builder.row(InlineKeyboardButton(
            text=f"{m['label']} — {m['cost_credits']} кр./сообщ.",
            callback_data=f"chat:model:{m['id']}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="chat:back:menu"))
    return builder.as_markup()


@router.message(F.text == BTN_CHAT)
async def enter_chat(message: Message, state: FSMContext) -> None:
    logger.info("[NAV] enter_chat called, chat=%s user=%s", message.chat.id, message.from_user.id)
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await safe_delete(message, "BTN_CHAT")
    models = get_models_for_task(TaskType.CHAT)
    logger.info("[NAV] enter_chat models found: %s", [m['id'] for m in models] if models else [])
    if not models:
        await message.answer("⚠️ Чат временно недоступен. Попробуй позже.")
        return
    await state.set_state(ChatStates.select_model)
    logger.info("[NAV] enter_chat state set to select_model")
    sent = await message.answer(
        "💬 <b>Выбери модель для чата:</b>",
        parse_mode="HTML",
        reply_markup=_chat_model_kb(models),
    )
    await state.update_data(_tracked_msg_ids=[sent.message_id])
    logger.info("[NAV] enter_chat sent model kb msg_id=%s", sent.message_id)


@router.callback_query(ChatStates.select_model, F.data == "chat:back:menu")
async def chat_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:chat")
async def menu_to_chat(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.delete()
    await state.clear()
    models = get_models_for_task(TaskType.CHAT)
    if not models:
        await callback.message.answer("⚠️ Чат временно недоступен. Попробуй позже.")
        return
    await state.set_state(ChatStates.select_model)
    await callback.message.answer(
        "💬 <b>Выбери модель для чата:</b>",
        parse_mode="HTML",
        reply_markup=_chat_model_kb(models),
    )


@router.callback_query(ChatStates.select_model, F.data.startswith("chat:model:"))
async def select_chat_model(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[NAV] select_chat_model called, data=%s", callback.data)
    model_slug = callback.data[len("chat:model:"):]
    models = get_models_for_task(TaskType.CHAT)
    model_cfg = next((m for m in models if m["id"] == model_slug), None)

    if not model_cfg:
        await callback.answer("Модель недоступна", show_alert=True)
        return

    await state.update_data(
        chat_model_slug=model_slug,
        chat_model_id=model_cfg["model_id"],
        chat_model_label=model_cfg["label"],
    )
    await state.set_state(ChatStates.active)

    await callback.message.edit_text(
        f"💬 <b>{model_cfg['label']}</b>\n"
        "Режим чата активен. Задай любой вопрос.\n"
        "Я помню контекст разговора в рамках сессии.",
        parse_mode="HTML",
    )
    await callback.message.answer("Введи сообщение:", reply_markup=chat_kb())
    await callback.answer()


@router.message(ChatStates.active, F.text == BTN_NEW_DIALOG)
async def new_chat(message: Message, state: FSMContext) -> None:
    await chat_service.reset_history(message.from_user.id)
    data = await state.get_data()
    label = data.get("chat_model_label", "ИИ")
    await message.answer(f"Диалог сброшен. {label} готов к новому разговору!")


@router.message(ChatStates.active, F.text == BTN_CHAT_PICK_MODEL)
async def back_to_model_select(message: Message, state: FSMContext) -> None:
    await chat_service.reset_history(message.from_user.id)
    models = get_models_for_task(TaskType.CHAT)
    await state.set_state(ChatStates.select_model)
    await message.answer(
        "💬 <b>Выбери модель для чата:</b>",
        parse_mode="HTML",
        reply_markup=_chat_model_kb(models),
    )


@router.message(F.text == BTN_EXIT_CHAT)
async def exit_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(ChatStates.active, F.text, ~F.text.in_(MENU_BUTTONS))
async def chat_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    model_slug = data.get("chat_model_slug")

    thinking = await message.answer("💭 Думаю...")
    try:
        response = await chat_service.send_message(
            message.from_user.id, message.from_user.username, message.text, model_slug
        )
        await thinking.delete()
        await message.answer(response)

    except InsufficientCreditsError as e:
        await thinking.delete()
        await state.update_data(pending_retry_type="chat", pending_message=message.text)
        await message.answer(
            f"❌ {e}\n\nПополни баланс — я отвечу на твой вопрос автоматически:",
            reply_markup=quick_topup_kb(),
        )
    except RateLimitError as e:
        await thinking.delete()
        await message.answer(f"⏱ {e}")
    except ProviderError:
        await thinking.delete()
        await message.answer("⚠️ Сервис временно недоступен. Попробуй позже.")
    except Exception:
        await thinking.delete()
        await message.answer("⚠️ Произошла непредвиденная ошибка. Попробуй позже.")


@router.message(ChatStates.active, ~F.text.in_(MENU_BUTTONS))
async def chat_wrong_input(message: Message) -> None:
    await message.answer("Напиши текстовое сообщение — я отвечу на него.")


async def resume_chat_after_topup(message: Message, state: FSMContext) -> None:
    """Вызывается из billing после успешной оплаты — отвечает на отложенное сообщение."""
    data = await state.get_data()
    pending_text = data.get("pending_message", "")
    await state.update_data(pending_message=None)

    model_slug = data.get("chat_model_slug")
    thinking = await message.answer("💭 Думаю...")
    try:
        response = await chat_service.send_message(
            message.from_user.id, message.from_user.username, pending_text, model_slug
        )
        await thinking.delete()
        await message.answer(response)
    except InsufficientCreditsError as e:
        await thinking.delete()
        await message.answer(f"❌ {e}\n\nПополни баланс в разделе «Мой баланс».")
    except RateLimitError as e:
        await thinking.delete()
        await message.answer(f"⏱ {e}")
    except ProviderError:
        await thinking.delete()
        await message.answer("⚠️ Сервис временно недоступен. Попробуй позже.")
    except Exception:
        await thinking.delete()
        await message.answer("⚠️ Произошла непредвиденная ошибка. Попробуй позже.")
