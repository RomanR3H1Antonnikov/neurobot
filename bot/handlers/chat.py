from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.keyboards.main_menu import BTN_CHAT, main_menu_kb
from providers.base import ProviderError
from services import chat_service
from services.media_service import InsufficientCreditsError, RateLimitError

router = Router()


class ChatStates(StatesGroup):
    active = State()


def chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔄 Новый диалог"), KeyboardButton(text="🏠 Выйти в меню")]],
        resize_keyboard=True,
    )


@router.message(F.text == BTN_CHAT)
async def enter_chat(message: Message, state: FSMContext) -> None:
    await state.set_state(ChatStates.active)
    await message.answer(
        "💬 Режим чата активен. Задай любой вопрос.\n"
        "Я помню контекст разговора в рамках сессии.",
        reply_markup=chat_kb(),
    )


@router.message(ChatStates.active, F.text == "🔄 Новый диалог")
async def new_chat(message: Message) -> None:
    await chat_service.reset_history(message.from_user.id)
    await message.answer("Диалог сброшен. Начнём сначала!")


@router.message(ChatStates.active, F.text == "🏠 Выйти в меню")
async def exit_chat(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(ChatStates.active, F.text)
async def chat_message(message: Message) -> None:
    thinking = await message.answer("💭 Думаю...")
    try:
        response = await chat_service.send_message(
            message.from_user.id, message.from_user.username, message.text
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
