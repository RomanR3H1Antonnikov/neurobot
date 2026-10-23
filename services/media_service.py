"""
Оркестрация медиагенерации: лимит → баланс → провайдер → списание → результат.
"""
import asyncio
import logging
import os
import tempfile

from providers.base import TaskType, GenerationResult, ProviderUnavailableError
from providers.router import get_provider_by_model_id, get_provider_chain_for_model, get_task_rate_limit
from db.queries import get_or_create_user, deduct_credits, check_and_increment_rate_limit

logger = logging.getLogger(__name__)

_PROVIDER_RETRY = 2        # попыток на каждого провайдера (оригинал + ретрай)
_PROVIDER_RETRY_DELAY = 3  # секунд между попытками


async def _try_provider(fn, *args, **kwargs):
    """Вызывает fn(*args, **kwargs) до _PROVIDER_RETRY раз при ProviderUnavailableError."""
    last_err = None
    for attempt in range(_PROVIDER_RETRY):
        try:
            return await fn(*args, **kwargs)
        except ProviderUnavailableError as e:
            last_err = e
            if attempt < _PROVIDER_RETRY - 1:
                logger.warning("[RETRY] %s attempt %d: %s", fn.__qualname__, attempt + 1, e)
                await asyncio.sleep(_PROVIDER_RETRY_DELAY)
    raise last_err


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


async def _video_duration_seconds(video_bytes: bytes) -> float | None:
    """Длительность видео в секундах (ffprobe) или None при ошибке."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as inp:
        inp.write(video_bytes)
        inp_path = inp.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", inp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return float(stdout.decode().strip())
    except Exception:
        return None
    finally:
        os.unlink(inp_path)


async def _trim_video(video_bytes: bytes, target_seconds: float) -> bytes:
    """Обрезает видео до target_seconds без перекодирования (-c copy)."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as inp:
        inp.write(video_bytes)
        inp_path = inp.name
    out_path = inp_path + "_trimmed.mp4"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", inp_path, "-t", str(target_seconds), "-c", "copy", out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("ffmpeg trim_video failed: %s", stderr.decode()[:300])
            return video_bytes
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(inp_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


_DURATION_TRIM_TOLERANCE = 0.5  # секунд — не трогаем, если разница в пределах обычного округления кодека


async def _ensure_requested_duration(video_bytes: bytes, requested_seconds: int) -> bytes:
    """Страховка на случай, если модель вернула видео заметно длиннее запрошенного
    (не предсказанное отличие в поведении провайдера) — обрезает результат до запрошенной
    длительности, чтобы пользователь получил ровно ту длительность, за которую заплатил.

    Не вызывается для моделей с video_ref_sets_duration (например, Kling 3.0 Omni
    с video-референсом): там бо́льшая длительность — ожидаемое поведение KIE (длительность
    берётся из референса), а не сбой, и duration уже равен длительности референса (см.
    generate_video()). Это НЕ меняет, сколько агрегатор уже списал за фактическую
    длительность на своей стороне — только то, что получает пользователь."""
    actual = await _video_duration_seconds(video_bytes)
    if actual is None or actual <= requested_seconds + _DURATION_TRIM_TOLERANCE:
        return video_bytes
    logger.warning(
        "[DURATION_MISMATCH] запрошено %sс, получено %.2fс — обрезаю результат до запрошенной длительности",
        requested_seconds, actual,
    )
    return await _trim_video(video_bytes, requested_seconds)


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
      1. cost_by_resolution_with_video[resolution|default]        (если есть видео-референс, фикс. цена)
      2. cost_per_second_by_resolution_with_video × duration      (если есть видео-референс)
      3. cost_per_second_by_resolution_no_video × duration        (если нет видео-референса)
      4. cost_by_duration_resolution[resolution|default][dur]     (дискретная таблица)
      5. cost_per_second_by_resolution_no_audio × duration        (если без звука)
      6. cost_per_second_by_resolution × duration
      7. cost_per_second × duration
      8. cost_by_resolution[resolution]
      9. cost_credits (фиксированная)
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

        # 2. Per-second по разрешению при наличии видео-референса
        if duration:
            per_sec_video = model_cfg.get("cost_per_second_by_resolution_with_video")
            if per_sec_video and resolution and resolution in per_sec_video:
                return max(1, round(per_sec_video[resolution] * duration))

    if duration:
        if not has_video_ref:
            # 3. Per-second по разрешению (без видео-референса)
            no_video = model_cfg.get("cost_per_second_by_resolution_no_video")
            if no_video and resolution and resolution in no_video:
                return max(1, round(no_video[resolution] * duration))
            # 4. Дискретная таблица по длительности и разрешению
            dur_res = model_cfg.get("cost_by_duration_resolution")
            if dur_res:
                dur_map = (dur_res.get(resolution) if resolution else None) or dur_res.get("default")
                if dur_map and str(duration) in dur_map:
                    return int(dur_map[str(duration)])

        # 5. Per-second по разрешению без звука
        if not has_audio:
            no_audio = model_cfg.get("cost_per_second_by_resolution_no_audio")
            if no_audio and resolution and resolution in no_audio:
                return max(1, round(no_audio[resolution] * duration))

        # 6. Per-second по разрешению
        by_dur_res = model_cfg.get("cost_per_second_by_resolution")
        if by_dur_res and resolution and resolution in by_dur_res:
            return max(1, round(by_dur_res[resolution] * duration))
        # 7. Flat per-second
        per_sec = model_cfg.get("cost_per_second")
        if per_sec:
            return max(1, round(per_sec * duration))

    # 8. Фиксированная цена по разрешению
    by_res = model_cfg.get("cost_by_resolution")
    if by_res and resolution and resolution in by_res:
        return by_res[resolution]
    # 9. Fallback
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
    character_reference_url: str | None = None,
    mj_params: dict | None = None,
    output_format: str | None = None,
) -> GenerationResult:
    task = TaskType.IMAGE_GENERATION
    chain = get_provider_chain_for_model(task, model_slug)
    _, model_cfg = chain[0]
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg, resolution=resolution)

    result, last_err = None, None
    for provider, eff_cfg in chain:
        try:
            result = await _try_provider(
                provider.generate_image,
                prompt, aspect_ratio=aspect_ratio, resolution=resolution,
                model=eff_cfg["model_id"], style_reference_urls=style_reference_urls,
                quality=quality, character_reference_url=character_reference_url,
                mj_params=mj_params, output_format=output_format,
            )
            break
        except ProviderUnavailableError as e:
            last_err = e
            if len(chain) > 1:
                logger.warning("[FALLBACK] image %s → %s: %s", model_slug, eff_cfg.get("provider"), e)
    if result is None:
        raise last_err
    _cost = get_cost(model_cfg, resolution)
    await deduct_credits(user_id, _cost, task.value)
    result.cost = _cost
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
    characters: list[dict] | None = None,
    shots: list[dict] | None = None,
) -> GenerationResult:
    task = TaskType.VIDEO_GENERATION
    chain = get_provider_chain_for_model(task, model_slug)
    _, model_cfg = chain[0]
    has_video_ref = bool(video_reference_urls)
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg,
                                         resolution=resolution, duration=duration,
                                         has_video_ref=has_video_ref, has_audio=audio)

    result, last_err = None, None
    for provider, eff_cfg in chain:
        try:
            result = await _try_provider(
                provider.generate_video,
                prompt, duration=duration, model=eff_cfg["model_id"],
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
                **({"characters": characters} if characters else {}),
                **({"shots": shots} if shots else {}),
            )
            break
        except ProviderUnavailableError as e:
            last_err = e
            if len(chain) > 1:
                logger.warning("[FALLBACK] video %s → %s: %s", model_slug, eff_cfg.get("provider"), e)
    if result is None:
        raise last_err
    if not (has_video_ref and model_cfg.get("video_ref_sets_duration")):
        # Для моделей, где длительность берётся из видео-референса (video_ref_sets_duration),
        # duration — это и есть ожидаемая длительность референса, обрезать тут нечего.
        result = GenerationResult(
            data=await _ensure_requested_duration(result.data, duration),
            mime_type=result.mime_type,
            filename=result.filename,
        )
    if not audio:
        result = GenerationResult(
            data=await _strip_audio(result.data),
            mime_type=result.mime_type,
            filename=result.filename,
        )
    _cost = get_cost(model_cfg, resolution, duration, has_video_ref=has_video_ref, has_audio=audio)
    await deduct_credits(user_id, _cost, task.value)
    result.cost = _cost
    return result


async def generate_audio(
    telegram_id: int, username: str, prompt: str, audio_type: str, model_slug: str,
    music_params: dict | None = None,
) -> GenerationResult:
    task = TaskType.AUDIO_GENERATION
    chain = get_provider_chain_for_model(task, model_slug)
    _, model_cfg = chain[0]
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg)

    result, last_err = None, None
    for provider, eff_cfg in chain:
        try:
            result = await _try_provider(
                provider.generate_audio,
                prompt, audio_type=audio_type, model=eff_cfg["model_id"], music_params=music_params,
            )
            break
        except ProviderUnavailableError as e:
            last_err = e
            if len(chain) > 1:
                logger.warning("[FALLBACK] audio %s → %s: %s", model_slug, eff_cfg.get("provider"), e)
    if result is None:
        raise last_err
    _cost = get_cost(model_cfg)
    await deduct_credits(user_id, _cost, task.value)
    result.cost = _cost
    return result


async def edit_image(
    telegram_id: int, username: str, image_bytes: bytes, prompt: str, model_slug: str,
    image_url: str | None = None, style_reference_urls: list[str] | None = None,
    provider_task_id: str | None = None, resolution: str | None = None,
    quality: str | None = None, aspect_ratio: str | None = None,
    mj_params: dict | None = None, output_format: str | None = None,
) -> GenerationResult:
    task = TaskType.IMAGE_EDIT
    chain = get_provider_chain_for_model(task, model_slug)
    _, model_cfg = chain[0]
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg, resolution=resolution)

    result, last_err = None, None
    for provider, eff_cfg in chain:
        try:
            result = await _try_provider(
                provider.edit_image,
                image_bytes, prompt, model=eff_cfg["model_id"],
                image_url=image_url, style_reference_urls=style_reference_urls,
                provider_task_id=provider_task_id,
                resolution=resolution,
                quality=quality,
                aspect_ratio=aspect_ratio,
                mj_params=mj_params, output_format=output_format,
            )
            break
        except ProviderUnavailableError as e:
            last_err = e
            if len(chain) > 1:
                logger.warning("[FALLBACK] image_edit %s → %s: %s", model_slug, eff_cfg.get("provider"), e)
    if result is None:
        raise last_err
    _cost = get_cost(model_cfg, resolution)
    await deduct_credits(user_id, _cost, task.value)
    result.cost = _cost
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
    characters: list[dict] | None = None,
    video_reference_urls: list[str] | None = None,
) -> GenerationResult:
    # mute — вырезать звук из готового результата. По умолчанию (None) = not audio;
    # Wan-режим «Оригинальный звук» передаёт audio=False, но звук вырезать НЕ должен.
    task = TaskType.VIDEO_EDIT
    chain = get_provider_chain_for_model(task, model_slug)
    _, model_cfg = chain[0]
    user_id = await _check_preconditions(telegram_id, username, task, model_cfg,
                                         resolution=resolution, duration=duration)

    result, last_err = None, None
    for provider, eff_cfg in chain:
        try:
            result = await _try_provider(
                provider.edit_video,
                video_bytes, prompt, model=eff_cfg["model_id"],
                video_url=video_url,
                duration=duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                audio=audio,
                audio_url=audio_url,
                style_reference_urls=style_reference_urls,
                characters=characters,
                video_reference_urls=video_reference_urls,
            )
            break
        except ProviderUnavailableError as e:
            last_err = e
            if len(chain) > 1:
                logger.warning("[FALLBACK] video_edit %s → %s: %s", model_slug, eff_cfg.get("provider"), e)
    if result is None:
        raise last_err
    if (not audio) if mute is None else mute:
        result = GenerationResult(
            data=await _strip_audio(result.data),
            mime_type=result.mime_type,
            filename=result.filename,
        )
    _cost = get_cost(model_cfg, resolution, duration)
    await deduct_credits(user_id, _cost, task.value)
    result.cost = _cost
    return result
