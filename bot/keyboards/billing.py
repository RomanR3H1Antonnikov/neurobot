from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

MIN_RUB = 75  # Telegram enforces min 7500 kopecks (75 RUB) for fiat currencies
MAX_RUB = 100_000
MIN_STARS = 1

# Пакеты пополнения Stars (рубли → Stars)
TOPUP_PACKAGES_STARS = [
    (100,  99),
    (300,  249),
    (1000, 699),
]

# Пакеты пополнения YooKassa (рубли → рубли)
TOPUP_PACKAGES_RUB = [
    (100,  100),
    (300,  280),
    (1000, 900),
]


def payment_method_kb(has_yookassa: bool = True) -> InlineKeyboardMarkup:
    """Выбор способа оплаты."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="billing:method:stars"))
    if has_yookassa:
        builder.row(InlineKeyboardButton(text="💳 Банковская карта", callback_data="billing:method:rub"))
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="billing:menu"))
    return builder.as_markup()


def stars_packages_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for credits, stars in TOPUP_PACKAGES_STARS:
        builder.row(InlineKeyboardButton(
            text=f"{credits} ₽ — {stars} ⭐",
            callback_data=f"billing:topup:{credits}:{stars}:stars",
        ))
    builder.row(InlineKeyboardButton(text="✏️ Своя сумма", callback_data="billing:custom:stars"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="billing:method_back"))
    return builder.as_markup()


def rub_packages_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for credits, rub in TOPUP_PACKAGES_RUB:
        builder.row(InlineKeyboardButton(
            text=f"{rub} ₽",
            callback_data=f"billing:topup:{credits}:{rub}:rub",
        ))
    builder.row(InlineKeyboardButton(text="✏️ Своя сумма", callback_data="billing:custom:rub"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="billing:method_back"))
    return builder.as_markup()


def cancel_custom_kb(method: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Отмена", callback_data=f"billing:custom_cancel:{method}"))
    return builder.as_markup()


# Сохраняем для обратной совместимости (используется в quick_topup)
def balance_kb(has_yookassa: bool = True) -> InlineKeyboardMarkup:
    return payment_method_kb(has_yookassa)


def quick_topup_kb(has_yookassa: bool = True) -> InlineKeyboardMarkup:
    """Пополнение прямо из потока (без выхода в меню)."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="billing:method:stars"))
    if has_yookassa:
        builder.row(InlineKeyboardButton(text="💳 Банковская карта", callback_data="billing:method:rub"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="billing:cancel_topup"))
    return builder.as_markup()
