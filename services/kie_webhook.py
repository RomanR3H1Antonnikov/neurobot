"""
Легковесный aiohttp-сервер для приёма callback'ов от KIE.
KIE вызывает POST /kie/callback/{corr_id} когда job завершён.
Каждый ожидающий вызов generate_image/generate_video хранит
asyncio.Future в словаре _pending — callback его резолвит.
"""
import asyncio
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

# corr_id → asyncio.Future, резолвится телом callback'а от KIE
_pending: dict[str, asyncio.Future] = {}


def register_pending(corr_id: str) -> asyncio.Future:
    """Регистрирует ожидание callback'а, возвращает Future."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[corr_id] = fut
    return fut


def unregister_pending(corr_id: str) -> None:
    _pending.pop(corr_id, None)


async def _handle_callback(request: web.Request) -> web.Response:
    corr_id = request.match_info["corr_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info("KIE callback received: corr_id=%s, keys=%s", corr_id, list(body.keys()))

    fut = _pending.get(corr_id)
    if fut and not fut.done():
        fut.set_result(body)
    else:
        logger.warning("KIE callback for unknown or already resolved corr_id=%s", corr_id)

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
