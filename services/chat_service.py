from providers.base import TaskType, ChatResult
from providers.router import get_provider, get_task_cost, get_task_rate_limit
from db.queries import (
    get_or_create_user, deduct_credits,
    get_chat_history, add_chat_message, clear_chat_history,
    check_and_increment_rate_limit,
)
from services.media_service import InsufficientCreditsError, RateLimitError

SYSTEM_PROMPT = (
    "Ты — умный и дружелюбный ИИ-ассистент. "
    "Отвечай чётко, по делу, на том же языке, на котором пишет пользователь."
)


async def send_message(telegram_id: int, username: str, text: str) -> str:
    """Отправляет сообщение в чат, возвращает ответ ИИ."""
    user = await get_or_create_user(telegram_id, username)
    user_id = user["id"]

    rate_limit = get_task_rate_limit(TaskType.CHAT)
    ok = await check_and_increment_rate_limit(user_id, TaskType.CHAT.value, rate_limit)
    if not ok:
        raise RateLimitError(f"Превышен лимит сообщений ({rate_limit} в час)")

    cost = get_task_cost(TaskType.CHAT)
    if user["balance"] < cost:
        raise InsufficientCreditsError(
            f"Недостаточно кредитов. Нужно: {cost}, у вас: {user['balance']}"
        )

    history = await get_chat_history(user_id)
    history.append({"role": "user", "content": text})

    provider, _ = get_provider(TaskType.CHAT)
    result: ChatResult = await provider.chat(history, system=SYSTEM_PROMPT)

    await add_chat_message(user_id, "user", text)
    await add_chat_message(user_id, "assistant", result.text)
    await deduct_credits(user_id, cost, TaskType.CHAT.value)

    return result.text


async def reset_history(telegram_id: int) -> None:
    user = await get_or_create_user(telegram_id)
    await clear_chat_history(user["id"])
