"""
Легковесный aiohttp-сервер для приёма callback'ов от KIE.
KIE вызывает POST /kie/callback/{corr_id} когда job завершён.
Каждый ожидающий вызов generate_image/generate_video хранит
asyncio.Future в словаре _pending — callback его резолвит.

Если Future уже нет (таймаут или перезапуск бота), ищем контекст
в таблице pending_jobs и доставляем результат пользователю напрямую.
"""
import asyncio
import logging
import aiohttp
from aiohttp import web
from aiogram.types import BufferedInputFile

logger = logging.getLogger(__name__)

# corr_id → asyncio.Future, резолвится телом callback'а от KIE
_pending: dict[str, asyncio.Future] = {}

# Устанавливается из main.py после создания бота
_bot = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def register_pending(corr_id: str) -> asyncio.Future:
    """Регистрирует ожидание callback'а, возвращает Future."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[corr_id] = fut
    return fut


def unregister_pending(corr_id: str) -> None:
    _pending.pop(corr_id, None)


def resolve_pending(corr_id: str, body: dict) -> bool:
    """Резолвит Future если существует. Возвращает True если Future найден."""
    fut = _pending.get(corr_id)
    if fut and not fut.done():
        fut.set_result(body)
        return True
    return False


async def _download_url(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.read()


def _media_filename(media_type: str) -> str:
    return {"video": "video.mp4", "audio": "audio.mp3"}.get(media_type, "image.png")


async def _cleanup_job_record(corr_id: str) -> None:
    try:
        from db.queries import delete_pending_job
        await delete_pending_job(corr_id)
    except Exception as e:
        logger.debug("KIE: не удалось удалить pending_job %s: %s", corr_id, e)


async def _deliver_orphaned(corr_id: str, body: dict) -> None:
    """Доставляет результат KIE когда ожидающего Future нет (таймаут или перезапуск)."""
    from providers.kie import _extract_url, _is_failed
    from db.queries import get_pending_job, delete_pending_job, save_generation

    job = await get_pending_job(corr_id)
    if not job:
        logger.warning("KIE orphaned: нет записи в pending_jobs для corr_id=%s", corr_id)
        return

    await delete_pending_job(corr_id)

    if _is_failed(body):
        logger.info("KIE orphaned: job завершился с ошибкой, corr_id=%s — уведомление не отправляем", corr_id)
        return

    url = _extract_url(body)
    if not url:
        logger.warning("KIE orphaned: нет URL в теле callback, corr_id=%s", corr_id)
        return

    try:
        media_data = await _download_url(url)
    except Exception as e:
        logger.error("KIE orphaned: ошибка загрузки медиа, corr_id=%s: %s", corr_id, e)
        return

    telegram_id = job["telegram_id"]
    chat_id = job["chat_id"]
    media_type = job["media_type"]
    model_label = job["model_label"] or ""
    prompt = job["prompt"]

    _type_names = {"video": "видео", "photo": "фото", "audio": "аудио"}
    caption = f"✅ <b>Готово!</b> Ваш {_type_names.get(media_type, 'результат')} сгенерирован."
    if model_label:
        caption += f"\n<i>{model_label}</i>"
    if media_type == "photo":
        caption += "\n\nЧтобы отредактировать — скачайте фото и загрузите его в раздел «Редактировать медиа»."

    try:
        file = BufferedInputFile(media_data, filename=_media_filename(media_type))
        if media_type == "video":
            sent = await _bot.send_video(chat_id, file, caption=caption, parse_mode="HTML")
            file_id = sent.video.file_id
        elif media_type == "audio":
            sent = await _bot.send_audio(chat_id, file, caption=caption, parse_mode="HTML")
            file_id = sent.audio.file_id
        else:
            sent = await _bot.send_photo(chat_id, file, caption=caption, parse_mode="HTML")
            file_id = sent.photo[-1].file_id

        await save_generation(telegram_id, media_type, file_id, prompt=prompt, model_label=model_label)
        logger.info(
            "KIE orphaned: доставлен %s пользователю telegram_id=%s (chat_id=%s)",
            media_type, telegram_id, chat_id,
        )
    except Exception as e:
        logger.error("KIE orphaned: ошибка отправки пользователю telegram_id=%s: %s", telegram_id, e)


async def _handle_callback(request: web.Request) -> web.Response:
    corr_id = request.match_info["corr_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info("KIE callback received: corr_id=%s, keys=%s", corr_id, list(body.keys()))

    if resolve_pending(corr_id, body):
        # Нормальный путь: Future найден — чистим запись в БД фоново
        asyncio.create_task(_cleanup_job_record(corr_id))
    else:
        logger.warning("KIE callback: нет ожидающего Future для corr_id=%s — пробуем orphaned delivery", corr_id)
        if _bot is not None:
            asyncio.create_task(_deliver_orphaned(corr_id, body))

    return web.json_response({"code": 200, "msg": "ok"})


async def _handle_genapi_callback(request: web.Request) -> web.Response:
    corr_id = request.match_info["corr_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info("GenAPI callback received: corr_id=%s, keys=%s", corr_id, list(body.keys()))

    fut = _pending.get(corr_id)
    if fut and not fut.done():
        fut.set_result(body)
    else:
        logger.warning("GenAPI callback for unknown or already resolved corr_id=%s", corr_id)

    return web.json_response({"ok": True})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/kie/callback/{corr_id}", _handle_callback)
    app.router.add_post("/genapi/callback/{corr_id}", _handle_genapi_callback)

    async def _healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/healthz", _healthz)
    return app


async def start_webhook_server(host: str = "0.0.0.0", port: int = 8081) -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("KIE webhook server started on %s:%d", host, port)
    return runner
