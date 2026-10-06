"""
Сессия бота с повторными попытками при обрыве соединения с Telegram.

Сервер периодически получает `Connection reset by peer` от api.telegram.org (в журнале несколько
раз в сутки). aiogram сам повторяет только опрос обновлений, а обычные запросы (get_file,
отправка готового видео, правка сообщений) падали сразу: пользователь видел «непредвиденную
ошибку», хотя оплаченный результат уже был готов.

Повторяем только методы, где повтор безопасен или важнее возможного дубля (доставка результата),
и только при сетевых обрывах — не при таймаутах (они могут означать долгую загрузку большого
видео). getUpdates не трогаем: у диспетчера своя логика переподключения.
"""
import asyncio
import logging

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


class RetryingSession(AiohttpSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Неактивное соединение с Telegram закрываем через 5 сек (по умолчанию 15): иначе запрос
        # уходит в уже «мёртвое» соединение, висит десятки секунд и заканчивается Connection reset.
        self._connector_init["keepalive_timeout"] = 5
        # Закрываем соединения, которые удалённая сторона уже закрыла (CLOSE_WAIT),
        # чтобы polling не вис молча при «тихом» разрыве TCP-канала к Telegram.
        self._connector_init["enable_cleanup_closed"] = True

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
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
        return await self._request_with_retry(name, bot, method, timeout)

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
