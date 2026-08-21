from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from bot.keyboards.main_menu import main_menu_kb

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db_user: dict) -> None:
    await state.clear()
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Я умею генерировать фото, видео и аудио, вести диалог с ИИ "
        "и работать с твоими документами.\n\n"
        f"Твой баланс: <b>{db_user['balance']} кредитов</b>\n\n"
        "Выбери, что хочешь сделать:",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Доступные команды:</b>\n"
        "/start — главное меню\n"
        "/balance — мой баланс\n"
        "/help — эта справка\n\n"
        "<b>Как пользоваться:</b>\n"
        "• <b>Генерация медиа</b> — опиши словами, что хочешь получить\n"
        "• <b>Чат с ИИ</b> — задай любой вопрос\n"
        "• <b>Документы</b> — пришли файл и задание к нему",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message, db_user: dict) -> None:
    await message.answer(
        f"💳 Твой баланс: <b>{db_user['balance']} кредитов</b>",
        parse_mode="HTML",
    )
