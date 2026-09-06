import logging
from aiogram import Bot
from aiogram.fsm.context import FSMContext

logger = logging.getLogger(__name__)


async def cleanup_tracked_messages(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Удаляет все трекнутые сообщения бота перед сбросом состояния."""
    data = await state.get_data()
    tracked = data.get("_tracked_msg_ids") or []
    logger.info("[CLEANUP] chat=%s tracked_ids=%s", chat_id, tracked)
    for mid in tracked:
        try:
            await bot.delete_message(chat_id, mid)
            logger.info("[CLEANUP] deleted bot msg %s", mid)
        except Exception as e:
            logger.warning("[CLEANUP] failed to delete bot msg %s: %s", mid, e)


async def safe_delete(message, label: str = "") -> None:
    """Удаляет пользовательское сообщение; при ошибке логирует и продолжает."""
    try:
        await message.delete()
        logger.info("[DELETE_USER_MSG] %s msg_id=%s ok", label, message.message_id)
    except Exception as e:
        logger.warning("[DELETE_USER_MSG] %s msg_id=%s failed: %s", label, message.message_id, e)
