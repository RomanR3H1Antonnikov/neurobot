from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Пакеты пополнения (кредиты → цена в рублях)
TOPUP_PACKAGES = [
    (100, 99),
    (300, 249),
    (1000, 699),
]


def balance_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for credits, price in TOPUP_PACKAGES:
        builder.add(InlineKeyboardButton(
            text=f"{credits} кредитов — {price}₽",
            callback_data=f"billing:topup:{credits}:{price}",
        ))
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="billing:menu"))
    return builder.as_markup()
