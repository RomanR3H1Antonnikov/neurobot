"""
Оркестрация медиагенерации: лимит → баланс → провайдер → списание → результат.
"""
import asyncio
import logging
import os
import tempfile

from providers.base import TaskType, GenerationResult
from providers.router import get_provider_by_model_id, get_task_rate_limit
from db.queries import get_or_create_user, deduct_credits, check_and_increment_rate_limit

logger = logging.getLogger(__name__)


async def _strip_audio(video_bytes: bytes) -> bytes:
    """Removes the audio track from a video using ffmpeg (-an -c:v copy)."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as inp:
        inp.write(video_bytes)
        inp_path = inp.name
    out_path = inp_path + "_muted.mp4"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", inp_path, "-an", "-c:v", "copy", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("ffmpeg strip_audio failed: %s", stderr.decode()[:300])
            return video_bytes
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(inp_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


class InsufficientCreditsError(Exception):
    pass


class RateLimitError(Exception):
    pass


def get_cost(
    model_cfg: dict,
    resolution: str | None = None,
    duration: int | None = None,
    has_video_ref: bool = False,
    has_audio: bool = True,
) -> int:
    """Возвращает стоимость генерации.

    Приоритет:
      1. cost_by_resolution_with_video[resolution|default]       (если есть видео-референс)
      2. cost_per_second_by_resolution_no_video × duration       (если нет видео-референса)
      3. cost_by_duration_resolution[resolution|default][dur]    (дискретная таблица)
      4. cost_per_second_by_resolution_no_audio × duration       (если без звука)
      5. cost_per_second_by_resolution × duration
      6. cost_per_second × duration
      7. cost_by_resolution[resolution]
      8. cost_credits (фиксированная)
    """
    # 1. Фиксированная цена за генерацию при наличии видео-референса
    if has_video_ref:
        with_video = model_cfg.get("cost_by_resolution_with_video")
        if with_video:
            cost = (with_video.get(resolution) if resolution else None)
            if cost is None:
                cost = with_video.get("default")
            if cost is not None:
                return int(cost)

    if duration:
        if not has_video_ref:
            # 2. Per-second по разрешению (без видео-референса)
            no_video = model_cfg.get("cost_per_second_by_resolution_no_video")
            if no_video and resolution and resolution in no_video:
                return max(1, round(no_video[resolution] * duration))
            # 3. Дискретная таблица по длительности и разрешению
            dur_res = model_cfg.get("cost_by_duration_resolution")
            if dur_res:
                dur_map = (dur_res.get(resolution) if resolution else None) or dur_res.get("default")
                if dur_map and str(duration) in dur_map:
                    return int(dur_map[str(duration)])

        # 4. Per-second по разрешению без звука
        if not has_audio:
            no_audio = model_cfg.get("cost_per_second_by_resolution_no_audio")
            if no_audio and resolution and resolution in no_audio:
                return max(1, round(no_audio[resolution] * duration))

        # 5. Per-second по разрешению
        by_dur_res = model_cfg.get("cost_per_second_by_resolution")
        if by_dur_res and resolution and resolution in by_dur_res:
            return max(1, round(by_dur_res[resolution] * duration))
        # 6. Flat per-second
        per_sec = model_cfg.get("cost_per_second")
        if per_sec:
            return max(1, round(per_sec * duration))

    # 7. Фиксированная цена по разрешению
    by_res = model_cfg.get("cost_by_resolution")
    if by_res and resolution and resolution in by_res:
        return by_res[resolution]
    # 8. Fallback
    return model_cfg["cost_credits"]


async def _check_preconditions(
    telegram_id: int, username: str, task_type: TaskType, model_cfg: dict,
    resolution: str | None = None, duration: int | None = None,
    has_video_ref: bool = False, has_audio: bool = True,
) -> int:
    """Проверяет лимиты и баланс. Возвращает user_id из БД."""
    user = await get_or_create_user(telegram_id, username)
    user_id = user["id"]

    rate_limit = get_task_rate_limit(task_type)
    ok = await check_and_increment_rate_limit(user_id, task_type.value, rate_limit)
    if not ok:
        raise RateLimitError(f"Превышен лимит: не более {rate_limit} запросов в час")

    cost = get_cost(model_cfg, resolution, duration, has_video_ref=has_video_ref, has_audio=has_audio)
    if user["balance"] < cost:
        raise InsufficientCreditsError(
            f"Недостаточно средств. Нужно: {cost} ₽, у вас: {user['balance']} ₽"
        )
    return user_id


async def generate_image(
    telegram_id: int, username: str, prompt: str, aspect_ratio: str, resolution: str, model_slug: str,
    style_reference_urls: list[str] | None = None,
    quality: str | None = None,
) -> GenerationResult:
    task = TaskType.IMAGE_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg, resolution=resolution)

    result = await provider.generate_image(
        prompt, aspect_ratio=aspect_ratio, resolution=resolution,
        model=model_cfg["model_id"], style_reference_urls=style_reference_urls,
        quality=quality,
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
    character_orientation: str | None = None,
) -> GenerationResult:
    task = TaskType.VIDEO_GENERATION
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    has_video_ref = bool(video_reference_urls)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg,
                                         resolution=resolution, duration=duration,
                                         has_video_ref=has_video_ref, has_audio=audio)

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
        character_orientation=character_orientation,
    )
    if not audio:
        result = GenerationResult(
            data=await _strip_audio(result.data),
            mime_type=result.mime_type,
            filename=result.filename,
        )
    await deduct_credits(user_id, get_cost(model_cfg, resolution, duration, has_video_ref=has_video_ref, has_audio=audio), task.value)
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
    quality: str | None = None, aspect_ratio: str | None = None,
) -> GenerationResult:
    task = TaskType.IMAGE_EDIT
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg, resolution=resolution)

    result = await provider.edit_image(
        image_bytes, prompt, model=model_cfg["model_id"],
        image_url=image_url, style_reference_urls=style_reference_urls,
        provider_task_id=provider_task_id,  # для KIE Grok: CDN URL из генерации
        resolution=resolution,
        quality=quality,
        aspect_ratio=aspect_ratio,
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
    style_reference_urls: list[str] | None = None,
    mute: bool | None = None,
) -> GenerationResult:
    # mute — вырезать звук из готового результата. По умолчанию (None) = not audio;
    # Wan-режим «Оригинальный звук» передаёт audio=False, но звук вырезать НЕ должен.
    task = TaskType.VIDEO_EDIT
    provider, model_cfg = get_provider_by_model_id(task, model_slug)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg,
                                         resolution=resolution, duration=duration)

    result = await provider.edit_video(
        video_bytes, prompt, model=model_cfg["model_id"],
        video_url=video_url,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        audio=audio,
        audio_url=audio_url,
        style_reference_urls=style_reference_urls,
    )
    if (not audio) if mute is None else mute:
        result = GenerationResult(
            data=await _strip_audio(result.data),
            mime_type=result.mime_type,
            filename=result.filename,
        )
    await deduct_credits(user_id, get_cost(model_cfg, resolution, duration), task.value)
    return result
