from typing import Any, Callable, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from db.queries import get_or_create_user


class UserMiddleware(BaseMiddleware):
    """Создаёт/обновляет пользователя в БД и прокидывает db_user в данные хэндлера."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = data.get("event_from_user")
        if tg_user:
            db_user = await get_or_create_user(tg_user.id, tg_user.username)
            data["db_user"] = db_user
        return await handler(event, data)
