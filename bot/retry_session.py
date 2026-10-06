"""
Сессия бота с повторными попытками при обрыве соединения с Telegram.

Сервер периодически получает `Connection reset by peer` от api.telegram.org (в журнале несколько
раз в сутки). aiogram сам повторяет только опрос обновлений, а обычные запросы (get_file,
отправка готового видео, правка сообщений) падали сразу: пользователь видел «непредвиденную
ошибку», хотя оплаченный результат уже был готов.

Повторяем только методы, где повтор безопасен или важнее возможного дубля (доставка результата),
и только при сетевых обрывах — не при таймаутах (они могут означать долгую загрузку большого
видео).

Watchdog: если GetUpdates не возвращает ответ дольше WATCHDOG_SECONDS — TCP-соединение
«тихо» зависло. os._exit(1) + Restart=always в systemd поднимают бот заново за ~5 сек.
"""
import asyncio
import logging
import os
import time

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

logger = logging.getLogger(__name__)

_RETRY_METHODS = {
    "GetFile",
    "AnswerCallbackQuery", "SendChatAction", "DeleteMessage",
    "EditMessageText", "EditMessageReplyMarkup", "EditMessageMedia",
    "SendMessage", "SendPhoto", "SendVideo", "SendDocument", "SendAudio", "SendVoice", "SendMediaGroup",
}
_ATTEMPTS = 3
WATCHDOG_SECONDS = 300  # 5 минут без ответа от getUpdates → перезапуск

_last_get_updates: float = time.monotonic()


async def polling_watchdog() -> None:
    """Перезапускает процесс если polling завис дольше WATCHDOG_SECONDS."""
    while True:
        await asyncio.sleep(60)
        elapsed = time.monotonic() - _last_get_updates
        if elapsed > WATCHDOG_SECONDS:
            logger.error("Watchdog: getUpdates не отвечал %.0f сек — принудительный перезапуск", elapsed)
            os._exit(1)


class RetryingSession(AiohttpSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Неактивное соединение с Telegram закрываем через 5 сек (по умолчанию 15): иначе запрос
        # уходит в уже «мёртвое» соединение, висит десятки секунд и заканчивается Connection reset.
        self._connector_init["keepalive_timeout"] = 5
        # Закрываем соединения, которые удалённая сторона уже закрыла (CLOSE_WAIT).
        self._connector_init["enable_cleanup_closed"] = True

    async def make_request(self, bot, method, timeout=None):
        global _last_get_updates
        name = type(method).__name__

        if name == "GetUpdates":
            # Обновляем метку времени ДО запроса; если запрос завис, watchdog поймает это
            # через WATCHDOG_SECONDS и перезапустит процесс.
            _last_get_updates = time.monotonic()

        if name == "AnswerCallbackQuery":
            # Ответ на нажатие кнопки живёт у Telegram считанные секунды. Если он «протух» (бот
            # успел повисеть на сетевом сбое), это не ошибка обработки: раньше исключение
            # обрывало весь обработчик — например, генерация не запускалась вовсе.
            try:
                return await self._request_with_retry(name, bot, method, timeout)
            except TelegramBadRequest as e:
                if "query is too old" in str(e) or "query ID is invalid" in str(e):
                    logger.info("Нажатие кнопки устарело — ответ на него пропускаем")
                    return True
                raise

        result = await self._request_with_retry(name, bot, method, timeout)

        if name == "GetUpdates":
            # Успешный ответ сбрасывает watchdog-таймер.
            _last_get_updates = time.monotonic()

        return result

    async def _request_with_retry(self, name, bot, method, timeout):
        if name not in _RETRY_METHODS:
            return await super().make_request(bot, method, timeout)
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                return await super().make_request(bot, method, timeout)
            except TelegramNetworkError as e:
                if attempt == _ATTEMPTS or "timeout" in str(e).lower():
                    raise
                logger.warning("Telegram %s: сетевой сбой (%s), повтор %d/%d", name, str(e)[:80], attempt, _ATTEMPTS - 1)
                await asyncio.sleep(1.5 * attempt)
