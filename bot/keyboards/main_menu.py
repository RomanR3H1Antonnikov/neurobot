from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_MEDIA = "🎨 Генерация медиа"
BTN_CHAT = "💬 Чат с ИИ"
BTN_DOCS = "📄 Работа с документами"
BTN_BALANCE = "💳 Мой баланс"
BTN_EXIT_CHAT = "🏠 Выйти в меню"
BTN_NEW_DIALOG = "🔄 Новый диалог"

MENU_BUTTONS = {BTN_MEDIA, BTN_CHAT, BTN_DOCS, BTN_BALANCE, BTN_EXIT_CHAT, BTN_NEW_DIALOG}


def inline_main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=BTN_MEDIA, callback_data="menu:media"),
        InlineKeyboardButton(text=BTN_CHAT, callback_data="menu:chat"),
    )
    builder.row(
        InlineKeyboardButton(text=BTN_DOCS, callback_data="menu:docs"),
        InlineKeyboardButton(text=BTN_BALANCE, callback_data="menu:balance"),
    )
    return builder.as_markup()


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MEDIA), KeyboardButton(text=BTN_CHAT)],
            [KeyboardButton(text=BTN_DOCS), KeyboardButton(text=BTN_BALANCE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
