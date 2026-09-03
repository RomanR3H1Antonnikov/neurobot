"""
Middleware: при нажатии на кнопку удаляет все трекнутые сообщения бота,
которые появились НИЖЕ (т.е. после) сообщения с этой кнопкой.
"""
from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

# Эти callback'и не вызывают навигацию — просто показывают alert, чистить не нужно
_SKIP = frozenset({
    "media:gen_why_long",
    "media:show_prompt",
})


class CallbackCleanupMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        if state and event.message and event.data not in _SKIP:
            from bot.handlers.media import _delete_msgs_below
            await _delete_msgs_below(event.bot, event.message.chat.id, state, event.message.message_id)
        return await handler(event, data)
