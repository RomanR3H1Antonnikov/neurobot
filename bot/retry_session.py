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
from aiogram.exceptions import TelegramNetworkError

logger = logging.getLogger(__name__)

_RETRY_METHODS = {
    "GetFile",
    "AnswerCallbackQuery", "SendChatAction", "DeleteMessage",
    "EditMessageText", "EditMessageReplyMarkup", "EditMessageMedia",
    "SendMessage", "SendPhoto", "SendVideo", "SendDocument", "SendAudio", "SendVoice", "SendMediaGroup",
}
_ATTEMPTS = 3


class RetryingSession(AiohttpSession):
    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
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
