from providers.base import TaskType, ChatResult
from providers.router import get_provider, get_provider_by_model_id, get_task_rate_limit
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


async def send_message(
    telegram_id: int, username: str, text: str, model_slug: str | None = None,
    image_url: str | None = None,
) -> str:
    user = await get_or_create_user(telegram_id, username)
    user_id = user["id"]

    rate_limit = get_task_rate_limit(TaskType.CHAT)
    ok = await check_and_increment_rate_limit(user_id, TaskType.CHAT.value, rate_limit)
    if not ok:
        raise RateLimitError(f"Превышен лимит сообщений ({rate_limit} в час)")

    if model_slug:
        provider, model_cfg = get_provider_by_model_id(TaskType.CHAT, model_slug)
    else:
        provider, model_cfg = get_provider(TaskType.CHAT)

    cost = model_cfg["cost_credits"]
    if user["balance"] < cost:
        raise InsufficientCreditsError(
            f"Недостаточно средств. Нужно: {cost} ₽, у вас: {user['balance']} ₽"
        )

    user_text = text.strip() if text else ""
    history = await get_chat_history(user_id)

    if image_url:
        # Мультимодальное сообщение: фото + текст (OpenAI-compat формат)
        content: list | str = [
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        if user_text:
            content.insert(0, {"type": "text", "text": user_text})
        else:
            content.append({"type": "text", "text": "Опиши, что изображено на фото."})
        history_text = f"[фото] {user_text}" if user_text else "[фото]"
    else:
        content = user_text
        history_text = user_text

    history.append({"role": "user", "content": content})

    result: ChatResult = await provider.chat(
        history, system=SYSTEM_PROMPT, model=model_cfg["model_id"]
    )

    await add_chat_message(user_id, "user", history_text)
    await add_chat_message(user_id, "assistant", result.text)
    await deduct_credits(user_id, cost, TaskType.CHAT.value)

    return result.text


async def reset_history(telegram_id: int) -> None:
    user = await get_or_create_user(telegram_id)
    await clear_chat_history(user["id"])
