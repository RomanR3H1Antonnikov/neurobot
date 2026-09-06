import logging
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from bot.keyboards.main_menu import inline_main_menu_kb

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query()
async def global_callback_fallback(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    logger.warning(
        "[FALLBACK_CB] unhandled callback data=%r state=%s chat=%s",
        callback.data, current_state, callback.message.chat.id if callback.message else "?",
    )
    await callback.answer()


@router.message()
async def global_fallback(message: Message) -> None:
    await message.answer(
        "Не понял тебя. Выбери действие из меню 👇",
        reply_markup=inline_main_menu_kb(),
    )
