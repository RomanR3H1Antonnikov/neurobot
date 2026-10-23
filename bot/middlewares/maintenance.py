from typing import Any, Callable, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Update

from config import config
from db.queries import get_setting

MAINTENANCE_TEXT = "🔧 Бот временно на технических работах.\n\nПожалуйста, попробуй немного позже."


class MaintenanceMiddleware(BaseMiddleware):
    """Если включён режим техработ (команда /maintenance on), блокирует действия всех,
    кроме админов — показывает уведомление вместо обычной обработки.

    Уже начатые платежи (pre_checkout_query, successful_payment) не блокируются —
    иначе можно потерять уже списанные у пользователя деньги."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user and tg_user.id not in config.admin_ids:
            is_payment = (
                event.pre_checkout_query is not None
                or (event.message is not None and event.message.successful_payment is not None)
                or (
                    event.callback_query is not None
                    and (event.callback_query.data or "").startswith("billing:yk_check:")
                )
            )
            if not is_payment and await get_setting("maintenance_mode") == "1":
                if event.message:
                    await event.message.answer(MAINTENANCE_TEXT)
                elif event.callback_query:
                    await event.callback_query.answer(MAINTENANCE_TEXT, show_alert=True)
                return
        return await handler(event, data)
