import asyncio
from typing import Any, Callable, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

_COLLECT_DELAY = 0.5  # секунды на сбор всех фото альбома


class AlbumMiddleware(BaseMiddleware):
    """Собирает фото из одного Telegram-альбома (media_group) и передаёт их вместе.

    Для первого сообщения группы ждёт _COLLECT_DELAY, затем вызывает хэндлер
    один раз с data["album"] = list[Message] (все фото группы).
    Остальные сообщения альбома отбрасываются (хэндлер не вызывается).
    Одиночные фото (без media_group_id) проходят без изменений.
    """

    def __init__(self) -> None:
        self._groups: dict[str, list[Message]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.media_group_id:
            return await handler(event, data)

        group_id = event.media_group_id
        is_first = group_id not in self._groups

        if is_first:
            self._groups[group_id] = []
        self._groups[group_id].append(event)

        if not is_first:
            return None  # остальные сообщения группы: просто буферизуем

        await asyncio.sleep(_COLLECT_DELAY)
        album = self._groups.pop(group_id, [])
        data["album"] = album
        return await handler(album[0], data)
