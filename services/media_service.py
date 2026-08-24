"""
Оркестрация медиагенерации: лимит → баланс → провайдер → списание → результат.
"""
from providers.base import TaskType, GenerationResult
from providers.router import get_provider_by_model_id, get_task_rate_limit
from db.queries import get_or_create_user, deduct_credits, check_and_increment_rate_limit


class InsufficientCreditsError(Exception):
    pass


class RateLimitError(Exception):
    pass


async def _check_preconditions(
    telegram_id: int, username: str, task_type: TaskType, model_cfg: dict
) -> int:
    """Проверяет лимиты и баланс. Возвращает user_id из БД."""
    user = await get_or_create_user(telegram_id, username)
    user_id = user["id"]

    rate_limit = get_task_rate_limit(task_type)
    ok = await check_and_increment_rate_limit(user_id, task_type.value, rate_limit)
    if not ok:
        raise RateLimitError(f"Превышен лимит: не более {rate_limit} запросов в час")

    cost = model_cfg["cost_credits"]
    if user["balance"] < cost:
        raise InsufficientCreditsError(
            f"Недостаточно кредитов. Нужно: {cost}, у вас: {user['balance']}"
        )
    return user_id


async def generate_image(
    telegram_id: int, username: str, prompt: str, aspect_ratio: str, model_slug: str
) -> GenerationResult:
    task = TaskType.IMAGE_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result = await provider.generate_image(prompt, aspect_ratio=aspect_ratio, model=model_cfg["model_id"])
    await deduct_credits(user_id, model_cfg["cost_credits"], task.value)
    return result


async def generate_video(
    telegram_id: int, username: str, prompt: str, duration: int, model_slug: str
) -> GenerationResult:
    task = TaskType.VIDEO_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result = await provider.generate_video(prompt, duration=duration, model=model_cfg["model_id"])
    await deduct_credits(user_id, model_cfg["cost_credits"], task.value)
    return result


async def generate_audio(
    telegram_id: int, username: str, prompt: str, audio_type: str, model_slug: str
) -> GenerationResult:
    task = TaskType.AUDIO_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result = await provider.generate_audio(prompt, audio_type=audio_type, model=model_cfg["model_id"])
    await deduct_credits(user_id, model_cfg["cost_credits"], task.value)
    return result


async def edit_image(
    telegram_id: int, username: str, image_bytes: bytes, prompt: str, model_slug: str
) -> GenerationResult:
    task = TaskType.IMAGE_EDIT
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result = await provider.edit_image(image_bytes, prompt, model=model_cfg["model_id"])
    await deduct_credits(user_id, model_cfg["cost_credits"], task.value)
    return result
