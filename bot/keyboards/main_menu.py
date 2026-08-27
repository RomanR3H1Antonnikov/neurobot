from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BTN_MEDIA = "🎨 Генерация медиа"
BTN_CHAT = "💬 Чат с ИИ"
BTN_DOCS = "📄 Работа с документами"
BTN_BALANCE = "💳 Мой баланс"

MENU_BUTTONS = {BTN_MEDIA, BTN_CHAT, BTN_DOCS, BTN_BALANCE}


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MEDIA), KeyboardButton(text=BTN_CHAT)],
            [KeyboardButton(text=BTN_DOCS), KeyboardButton(text=BTN_BALANCE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
