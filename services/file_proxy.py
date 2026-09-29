"""
Отдача файлов пользователя нейросетям по публичной ссылке БЕЗ токена бота.

Раньше провайдерам передавались прямые ссылки вида
https://api.telegram.org/file/bot<ТОКЕН>/photos/file_1.jpg — токен бота попадал во внешние
сервисы, в их логи и на скриншоты. Теперь бот отдаёт файл сам:

    https://<публичный адрес бота>/kie/callback/tgfile/<случайный секрет>/<имя файла>

Секрет — одноразово сгенерированные 192 бита (secrets.token_urlsafe), живут ссылки TTL секунд.
Путь специально лежит под /kie/callback/ — этот префикс уже проксируется nginx на порт 8081,
менять конфиг nginx не нужно. Сам файл бот скачивает у Telegram в момент запроса.
"""
import logging
import mimetypes
import posixpath
import secrets
import time

from aiohttp import web

logger = logging.getLogger(__name__)

ROUTE_PREFIX = "/kie/callback/tgfile"
_TTL_SECONDS = 24 * 3600
_MAX_ENTRIES = 5000

# секрет → (file_path в Telegram, время протухания)
_files: dict[str, tuple[str, float]] = {}


def _purge(now: float) -> None:
    """Убирает протухшие записи; при переполнении — самые старые."""
    for key in [k for k, (_, exp) in _files.items() if exp < now]:
        _files.pop(key, None)
    if len(_files) > _MAX_ENTRIES:
        for key in sorted(_files, key=lambda k: _files[k][1])[: len(_files) - _MAX_ENTRIES]:
            _files.pop(key, None)


def url_for_path(file_path: str) -> str:
    """Публичная ссылка без токена на файл Telegram (file_path из bot.get_file)."""
    from config import config

    now = time.time()
    _purge(now)
    secret = secrets.token_urlsafe(24)
    _files[secret] = (file_path, now + _TTL_SECONDS)
    # Имя с расширением нужно провайдерам, которые определяют тип файла по ссылке
    name = posixpath.basename(file_path) or "file"
    base = config.kie_callback_base_url.rstrip("/")
    return f"{base}{ROUTE_PREFIX}/{secret}/{name}"


async def url_for_file_id(bot, file_id: str | None) -> str | None:
    if not file_id:
        return None
    info = await bot.get_file(file_id)
    return url_for_path(info.file_path)


async def handle_file(request: web.Request) -> web.Response:
    entry = _files.get(request.match_info["secret"])
    if not entry or entry[1] < time.time():
        raise web.HTTPNotFound()

    from services import kie_webhook  # ленивый импорт: модули ссылаются друг на друга

    bot = kie_webhook._bot
    if bot is None:
        raise web.HTTPServiceUnavailable()
    try:
        buf = await bot.download_file(entry[0])
        body = buf.read()
    except Exception as e:  # noqa: BLE001 — токен в текст ошибки попасть не должен
        logger.warning("tgfile: не удалось скачать файл из Telegram: %s", type(e).__name__)
        raise web.HTTPBadGateway()

    content_type = mimetypes.guess_type(entry[0])[0] or "application/octet-stream"
    return web.Response(body=body, content_type=content_type, headers={"Cache-Control": "private, max-age=3600"})
