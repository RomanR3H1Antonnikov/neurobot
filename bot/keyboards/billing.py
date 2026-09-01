from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Пакеты пополнения (кредиты → Stars)
TOPUP_PACKAGES = [
    (100, 99),
    (300, 249),
    (1000, 699),
]


def balance_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for credits, stars in TOPUP_PACKAGES:
        builder.add(InlineKeyboardButton(
            text=f"{credits} кредитов — {stars} ⭐",
            callback_data=f"billing:topup:{credits}:{stars}",
        ))
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="billing:menu"))
    return builder.as_markup()


def quick_topup_kb() -> InlineKeyboardMarkup:
    """Пополнение прямо из потока (без выхода в меню)."""
    builder = InlineKeyboardBuilder()
    for credits, stars in TOPUP_PACKAGES:
        builder.add(InlineKeyboardButton(
            text=f"{credits} кредитов — {stars} ⭐",
            callback_data=f"billing:topup:{credits}:{stars}",
        ))
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="billing:cancel_topup"))
    return builder.as_markup()
