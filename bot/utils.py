from aiogram import Bot
from aiogram.fsm.context import FSMContext


async def cleanup_tracked_messages(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Удаляет все трекнутые сообщения бота перед сбросом состояния."""
    data = await state.get_data()
    for mid in data.get("_tracked_msg_ids") or []:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
