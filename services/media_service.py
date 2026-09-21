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


def get_cost(
    model_cfg: dict,
    resolution: str | None = None,
    duration: int | None = None,
    has_video_ref: bool = False,
) -> int:
    """Возвращает стоимость генерации.

    Приоритет:
      1. cost_per_second_by_resolution_no_video × duration  (если нет видео-референса)
      2. cost_per_second_by_resolution × duration
      3. cost_per_second × duration
      4. cost_by_resolution[resolution]
      5. cost_credits (фиксированная)
    """
    if duration:
        if not has_video_ref:
            no_video = model_cfg.get("cost_per_second_by_resolution_no_video")
            if no_video and resolution and resolution in no_video:
                return max(1, round(no_video[resolution] * duration))
        by_dur_res = model_cfg.get("cost_per_second_by_resolution")
        if by_dur_res and resolution and resolution in by_dur_res:
            return max(1, round(by_dur_res[resolution] * duration))
        per_sec = model_cfg.get("cost_per_second")
        if per_sec:
            return max(1, round(per_sec * duration))
    by_res = model_cfg.get("cost_by_resolution")
    if by_res and resolution and resolution in by_res:
        return by_res[resolution]
    return model_cfg["cost_credits"]


async def _check_preconditions(
    telegram_id: int, username: str, task_type: TaskType, model_cfg: dict,
    resolution: str | None = None, duration: int | None = None,
    has_video_ref: bool = False,
) -> int:
    """Проверяет лимиты и баланс. Возвращает user_id из БД."""
    user = await get_or_create_user(telegram_id, username)
    user_id = user["id"]

    rate_limit = get_task_rate_limit(task_type)
    ok = await check_and_increment_rate_limit(user_id, task_type.value, rate_limit)
    if not ok:
        raise RateLimitError(f"Превышен лимит: не более {rate_limit} запросов в час")

    cost = get_cost(model_cfg, resolution, duration, has_video_ref=has_video_ref)
    if user["balance"] < cost:
        raise InsufficientCreditsError(
            f"Недостаточно средств. Нужно: {cost} ₽, у вас: {user['balance']} ₽"
        )
    return user_id


async def generate_image(
    telegram_id: int, username: str, prompt: str, aspect_ratio: str, resolution: str, model_slug: str,
    style_reference_urls: list[str] | None = None,
) -> GenerationResult:
    task = TaskType.IMAGE_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg, resolution=resolution)

    result = await provider.generate_image(
        prompt, aspect_ratio=aspect_ratio, resolution=resolution,
        model=model_cfg["model_id"], style_reference_urls=style_reference_urls,
    )
    await deduct_credits(user_id, get_cost(model_cfg, resolution), task.value)
    return result


async def generate_video(
    telegram_id: int, username: str, prompt: str, duration: int, model_slug: str,
    first_frame_url: str | None = None,
    last_frame_url: str | None = None,
    style_reference_urls: list[str] | None = None,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    audio_reference_urls: list[str] | None = None,
    video_reference_urls: list[str] | None = None,
    output_format: str | None = None,
    audio: bool = True,
) -> GenerationResult:
    task = TaskType.VIDEO_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    has_video_ref = bool(video_reference_urls)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg,
                                         resolution=resolution, duration=duration,
                                         has_video_ref=has_video_ref)

    result = await provider.generate_video(
        prompt, duration=duration, model=model_cfg["model_id"],
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        style_reference_urls=style_reference_urls,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        audio_reference_urls=audio_reference_urls,
        video_reference_urls=video_reference_urls,
        output_format=output_format,
        audio=audio,
    )
    await deduct_credits(user_id, get_cost(model_cfg, resolution, duration, has_video_ref=has_video_ref), task.value)
    return result


async def generate_audio(
    telegram_id: int, username: str, prompt: str, audio_type: str, model_slug: str,
    music_params: dict | None = None,
) -> GenerationResult:
    task = TaskType.AUDIO_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result = await provider.generate_audio(
        prompt, audio_type=audio_type, model=model_cfg["model_id"], music_params=music_params,
    )
    await deduct_credits(user_id, get_cost(model_cfg), task.value)
    return result


async def edit_image(
    telegram_id: int, username: str, image_bytes: bytes, prompt: str, model_slug: str,
    image_url: str | None = None, style_reference_urls: list[str] | None = None,
    provider_task_id: str | None = None, resolution: str | None = None,
) -> GenerationResult:
    task = TaskType.IMAGE_EDIT
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg, resolution=resolution)

    result = await provider.edit_image(
        image_bytes, prompt, model=model_cfg["model_id"],
        image_url=image_url, style_reference_urls=style_reference_urls,
        provider_task_id=provider_task_id,  # для KIE Grok: CDN URL из генерации
        resolution=resolution,
    )
    await deduct_credits(user_id, get_cost(model_cfg, resolution), task.value)
    return result


async def edit_video(
    telegram_id: int, username: str, video_bytes: bytes, prompt: str, model_slug: str,
    video_url: str | None = None,
    duration: int = 5,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    audio: bool = True,
    audio_url: str | None = None,
) -> GenerationResult:
    task = TaskType.VIDEO_EDIT
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result = await provider.edit_video(
        video_bytes, prompt, model=model_cfg["model_id"],
        video_url=video_url,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        audio=audio,
        audio_url=audio_url,
    )
    await deduct_credits(user_id, get_cost(model_cfg), task.value)
    return result
