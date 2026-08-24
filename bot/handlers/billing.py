from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.keyboards.main_menu import BTN_BALANCE, main_menu_kb
from bot.keyboards.billing import balance_kb
from config import config
from db.queries import get_or_create_user, add_credits, get_balance

router = Router()


@router.message(F.text == BTN_BALANCE)
@router.message(Command("balance"))
async def show_balance(message: Message, state: FSMContext, db_user: dict) -> None:
    await state.clear()
    await message.answer(
        f"💳 <b>Твой баланс:</b> {db_user['balance']} кредитов\n\n"
        "Выбери пакет для пополнения:",
        parse_mode="HTML",
        reply_markup=balance_kb(),
    )


@router.callback_query(F.data.startswith("billing:topup:"))
async def topup_selected(callback: CallbackQuery) -> None:
    # Формат: billing:topup:{credits}:{price}
    parts = callback.data.split(":")
    credits = int(parts[2])
    price = int(parts[3])

    # TODO: Этап 3 — здесь будет вызов Telegram Payments (send_invoice)
    # Пока показываем заглушку с инструкцией по оплате
    await callback.message.edit_text(
        f"💳 <b>Пополнение на {credits} кредитов</b>\n\n"
        f"Стоимость: <b>{price}₽</b>\n\n"
        "Оплата через Telegram Payments будет подключена на финальном этапе.\n"
        "Для тестирования обратись к администратору.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "billing:menu")
async def billing_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


# ─── Админская команда для тестового начисления кредитов ─────────────────────

@router.message(Command("addcredits"))
async def cmd_addcredits(message: Message) -> None:
    if message.from_user.id not in config.admin_ids:
        return  # молча игнорируем для не-администраторов

    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /addcredits <telegram_id> <количество>")
        return

    target_id = int(parts[1])
    amount = int(parts[2])

    user = await get_or_create_user(target_id)
    new_balance = await add_credits(user["id"], amount, description="Ручное пополнение (тест)")
    await message.answer(
        f"✅ Начислено {amount} кредитов пользователю {target_id}.\n"
        f"Новый баланс: {new_balance} кредитов."
    )
