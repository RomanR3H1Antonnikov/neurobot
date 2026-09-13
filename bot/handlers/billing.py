import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, PreCheckoutQuery, LabeledPrice
from aiogram.fsm.context import FSMContext

from bot.keyboards.main_menu import BTN_BALANCE, main_menu_kb
from bot.keyboards.billing import (
    payment_method_kb, stars_packages_kb, rub_packages_kb, quick_topup_kb,
    TOPUP_PACKAGES_STARS, TOPUP_PACKAGES_RUB,
)
from bot.utils import cleanup_tracked_messages, safe_delete
from config import config, reload_models
from db.queries import get_or_create_user, add_credits

logger = logging.getLogger(__name__)
router = Router()


def _has_yookassa() -> bool:
    return bool(config.yookassa_provider_token)


def _balance_text(balance: int) -> str:
    return f"💳 <b>Твой баланс:</b> {balance} ₽\n\nВыбери способ оплаты:"


# ─── Показ баланса ────────────────────────────────────────────────────────────

@router.message(F.text == BTN_BALANCE)
@router.message(Command("balance"))
async def show_balance(message: Message, state: FSMContext, db_user: dict) -> None:
    logger.info("[NAV] show_balance called, chat=%s user=%s", message.chat.id, message.from_user.id)
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await safe_delete(message, "BTN_BALANCE")
    sent = await message.answer(
        _balance_text(db_user["balance"]),
        parse_mode="HTML",
        reply_markup=payment_method_kb(_has_yookassa()),
    )
    await state.update_data(_tracked_msg_ids=[sent.message_id])


@router.callback_query(F.data == "menu:balance")
async def menu_to_balance(callback: CallbackQuery, state: FSMContext, db_user: dict) -> None:
    await callback.answer()
    await callback.message.delete()
    await state.clear()
    await callback.message.answer(
        _balance_text(db_user["balance"]),
        parse_mode="HTML",
        reply_markup=payment_method_kb(_has_yookassa()),
    )


# ─── Выбор метода оплаты ─────────────────────────────────────────────────────

@router.callback_query(F.data == "billing:method:stars")
async def choose_stars(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "⭐ <b>Оплата Telegram Stars</b>\n\nВыбери пакет:",
        parse_mode="HTML",
        reply_markup=stars_packages_kb(),
    )


@router.callback_query(F.data == "billing:method:rub")
async def choose_rub(callback: CallbackQuery) -> None:
    if not _has_yookassa():
        await callback.answer("Оплата картой временно недоступна", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "💳 <b>Оплата банковской картой</b>\n\nВыбери пакет:",
        parse_mode="HTML",
        reply_markup=rub_packages_kb(),
    )


@router.callback_query(F.data == "billing:method_back")
async def method_back(callback: CallbackQuery, state: FSMContext, db_user: dict) -> None:
    await callback.answer()
    await callback.message.edit_text(
        _balance_text(db_user["balance"]),
        parse_mode="HTML",
        reply_markup=payment_method_kb(_has_yookassa()),
    )


# ─── Выставление счёта ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("billing:topup:"))
async def topup_selected(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    credits = int(parts[2])
    amount = int(parts[3])
    method = parts[4] if len(parts) > 4 else "stars"

    await callback.answer()

    if method == "rub":
        if not _has_yookassa():
            await callback.answer("Оплата картой временно недоступна", show_alert=True)
            return
        await callback.message.answer_invoice(
            title="Пополнение баланса",
            description=f"Зачислим {credits} ₽ на ваш баланс для генерации медиа, чата и работы с документами",
            payload=f"topup:{credits}",
            provider_token=config.yookassa_provider_token,
            currency="RUB",
            prices=[LabeledPrice(label=f"Пополнение баланса на {credits} ₽", amount=amount * 100)],
        )
    else:
        await callback.message.answer_invoice(
            title="Пополнение баланса",
            description=f"{credits} кредитов для генерации медиа, чата и работы с документами",
            payload=f"topup:{credits}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{credits} кредитов", amount=amount)],
        )


# ─── Обработка платежей ───────────────────────────────────────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message, state: FSMContext) -> None:
    payload = message.successful_payment.invoice_payload
    credits = int(payload.split(":")[1])
    currency = message.successful_payment.currency

    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    method_label = "Telegram Stars" if currency == "XTR" else "банковская карта"
    new_balance = await add_credits(user["id"], credits, description=f"Пополнение через {method_label}")

    await message.answer(
        f"✅ <b>Баланс пополнен!</b>\n\n"
        f"Зачислено: <b>{credits} ₽</b>\n"
        f"Текущий баланс: <b>{new_balance} ₽</b>",
        parse_mode="HTML",
    )

    data = await state.get_data()
    pending_type = data.get("pending_retry_type")

    if pending_type == "media" and data.get("media_type"):
        await state.update_data(pending_retry_type=None)
        from bot.handlers.media import resume_generation_after_topup
        await resume_generation_after_topup(message, state)
    elif pending_type == "chat" and data.get("pending_message"):
        await state.update_data(pending_retry_type=None)
        from bot.handlers.chat import resume_chat_after_topup
        await resume_chat_after_topup(message, state)
    else:
        from bot.keyboards.main_menu import main_menu_kb
        await message.answer("Выбери, что хочешь сделать:", reply_markup=main_menu_kb())


# ─── Навигация ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "billing:cancel_topup")
async def cancel_topup(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pending_retry_type=None, pending_message=None)
    await state.clear()
    await callback.message.edit_text("Пополнение отменено.")
    from bot.keyboards.main_menu import main_menu_kb
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "billing:menu")
async def billing_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()
    from bot.keyboards.main_menu import main_menu_kb
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
        f"✅ Начислено {amount} ₽ пользователю {target_id}.\n"
        f"Новый баланс: {new_balance} ₽."
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
