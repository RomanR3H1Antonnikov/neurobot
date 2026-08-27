from aiogram import Router
from aiogram.types import Message
from bot.keyboards.main_menu import inline_main_menu_kb

router = Router()


@router.message()
async def global_fallback(message: Message) -> None:
    await message.answer(
        "Не понял тебя. Выбери действие из меню 👇",
        reply_markup=inline_main_menu_kb(),
    )
