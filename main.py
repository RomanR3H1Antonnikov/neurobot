import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from db.database import get_db, close_db
from bot.middlewares.user_middleware import UserMiddleware
from bot.handlers import start, media, chat, documents, billing, fallback
from services.kie_webhook import start_webhook_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.update.middleware(UserMiddleware())

    dp.include_router(start.router)
    dp.include_router(billing.router)
    dp.include_router(media.router)
    dp.include_router(chat.router)
    dp.include_router(documents.router)
    dp.include_router(fallback.router)  # должен быть последним

    await get_db()  # инициализация БД при старте

    webhook_runner = await start_webhook_server(host="0.0.0.0", port=8081)
    logger.info("Бот запущен")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await webhook_runner.cleanup()
        await close_db()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
