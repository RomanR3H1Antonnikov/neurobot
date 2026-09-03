"""
Middleware: при нажатии на кнопку удаляет все трекнутые сообщения бота,
которые появились НИЖЕ (т.е. после) сообщения с этой кнопкой.

Если ниже есть сообщения — сначала показывает предупреждение (show_alert).
Повторное нажатие той же кнопки подтверждает удаление.
"""
from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

# Эти callback'и не вызывают навигацию — просто показывают alert или отменяют генерацию
_SKIP = frozenset({
    "media:gen_why_long",
    "media:show_prompt",
    "media:cancel_generation",
})

_WARN_TEXT = (
    "⚠️ Вся переписка ниже этого сообщения будет удалена без возможности восстановления!\n\n"
    "Нажмите повторно для подтверждения."
)


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

            state_data = await state.get_data()
            anchor_id = event.message.message_id
            ids = list(state_data.get("_tracked_msg_ids") or [])
            has_below = any(mid > anchor_id for mid in ids)

            if has_below:
                warned_id = state_data.get("_cleanup_warned_msg_id")
                if warned_id == anchor_id:
                    # Повторное нажатие — удаляем и продолжаем
                    await state.update_data(_cleanup_warned_msg_id=None)
                    await _delete_msgs_below(event.bot, event.message.chat.id, state, anchor_id)
                else:
                    # Первое нажатие — предупреждаем и прерываем
                    await state.update_data(_cleanup_warned_msg_id=anchor_id)
                    await event.answer(_WARN_TEXT, show_alert=True)
                    return
            else:
                # Нет сообщений ниже — сбрасываем флаг на всякий случай и продолжаем
                if state_data.get("_cleanup_warned_msg_id"):
                    await state.update_data(_cleanup_warned_msg_id=None)

        return await handler(event, data)
