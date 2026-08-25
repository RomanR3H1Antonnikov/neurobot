from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, PreCheckoutQuery, LabeledPrice
from aiogram.fsm.context import FSMContext

from bot.keyboards.main_menu import BTN_BALANCE, main_menu_kb
from bot.keyboards.billing import balance_kb
from config import config, reload_models
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
    parts = callback.data.split(":")
    credits = int(parts[2])
    stars = int(parts[3])

    await callback.answer()
    await callback.message.answer_invoice(
        title="Пополнение баланса",
        description=f"{credits} кредитов для генерации медиа, чата и работы с документами",
        payload=f"topup:{credits}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{credits} кредитов", amount=stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    payload = message.successful_payment.invoice_payload
    credits = int(payload.split(":")[1])

    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    new_balance = await add_credits(user["id"], credits, description="Пополнение через Telegram Stars")

    await message.answer(
        f"✅ <b>Баланс пополнен!</b>\n\n"
        f"Зачислено: <b>{credits} кредитов</b>\n"
        f"Текущий баланс: <b>{new_balance} кредитов</b>",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "billing:menu")
async def billing_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


# ─── Админские команды ────────────────────────────────────────────────────────

@router.message(Command("addcredits"))
async def cmd_addcredits(message: Message) -> None:
    if message.from_user.id not in config.admin_ids:
        return

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


@router.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    """Перечитывает models_config.yaml без перезапуска бота."""
    if message.from_user.id not in config.admin_ids:
        return
    try:
        reload_models()
        await message.answer("✅ Конфигурация моделей перезагружена из models_config.yaml")
    except Exception as e:
        await message.answer(f"❌ Ошибка при загрузке конфига: {e}")
