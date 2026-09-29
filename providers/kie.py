"""
Адаптер для KIE (kie.ai).
Использует job-based API: POST /api/v1/jobs/createTask → ждёт callback.
Callback приходит на наш webhook-сервер (services/kie_webhook.py).
"""
import asyncio
import logging
import uuid
import aiohttp
from contextvars import ContextVar
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult, ProviderUnavailableError, ProviderContentPolicyError
from services.kie_webhook import register_pending, unregister_pending

# Устанавливается в _run_generation перед вызовом generate_*; читается в _create_job
_pending_job_ctx: ContextVar[dict | None] = ContextVar("_pending_job_ctx", default=None)


def set_pending_job_ctx(
    telegram_id: int, chat_id: int,
    model_label: str, media_type: str, prompt: str | None,
) -> None:
    _pending_job_ctx.set({
        "telegram_id": telegram_id, "chat_id": chat_id,
        "model_label": model_label, "media_type": media_type, "prompt": prompt,
    })

logger = logging.getLogger(__name__)

_KIE_API_BASE = "https://api.kie.ai/api/v1"
_JOB_TIMEOUT = 600        # секунд ожидания callback'а (10 минут)
_JOB_TIMEOUT_AUDIO = 1200  # 20 минут для аудио-генерации

# Формат соотношения сторон для KIE: "1:1" → "1:1" (совпадает), "auto" для произвольного
_RATIO_MAP: dict[str, str] = {}  # пустой = передаём as-is

# Маппинг resolution → quality для моделей Seedream (basic=2K, high=3K, ultra=4K)
_SEEDREAM_QUALITY: dict[str, str] = {
    "2K": "basic",
    "3K": "high",
    "4K": "ultra",
}


def _kie_ratio(aspect_ratio: str) -> str:
    return _RATIO_MAP.get(aspect_ratio, aspect_ratio)


def _image_input(
    model: str, prompt: str, aspect_ratio: str, resolution: str,
    style_reference_urls: list[str] | None = None,
) -> dict:
    """Формирует поле input для createTask в зависимости от семейства модели."""
    if model.startswith("seedream/"):
        return {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "quality": _SEEDREAM_QUALITY.get(resolution, "basic"),
            "output_format": "png",
        }
    if model.startswith("flux-2/"):
        # KIE docs: flux-2 text-to-image принимает только prompt/aspect_ratio/resolution.
        # image_input: [] вызывает 501 — Flex воспринимает его как пустой image-to-image.
        return {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "resolution": resolution,
        }
    if model == "nano-banana-2-lite":
        # Lite использует image_urls (не image_input) и не принимает resolution/output_format
        return {
            "prompt": prompt,
            "image_urls": list(style_reference_urls) if style_reference_urls else [],
            "aspect_ratio": _kie_ratio(aspect_ratio),
        }
    if model.startswith("grok-imagine-image-2-0/"):
        payload: dict = {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "resolution": resolution,
        }
        if style_reference_urls:
            payload["image_input"] = {
                "image_list": [{"image_url": u} for u in style_reference_urls],
            }
        return payload
    if model in ("gpt-image-2-text-to-image", "gpt-image-2-5-flare-text-to-image", "gpt-image-2-5-sunburst-text-to-image"):
        payload = {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "resolution": resolution,
        }
        if style_reference_urls:
            payload["image_urls"] = list(style_reference_urls)
        return payload
    # Nano Banana 2 и прочие — стандартный формат; поддерживает до 14 ориентиров
    result: dict = {
        "prompt": prompt,
        "image_input": list(style_reference_urls) if style_reference_urls else [],
        "aspect_ratio": _kie_ratio(aspect_ratio),
        "resolution": resolution,
        "output_format": "png",
    }
    return result


KLING_OMNI_GEN_ALIAS = "kling-3.0-omni/video"


def _kling_omni_request(
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    audio: bool,
    first_frame_url: str | None,
    last_frame_url: str | None,
    style_reference_urls: list[str] | None,
    video_reference_urls: list[str] | None,
) -> tuple[str, dict]:
    """Kling 3.0 Omni у KIE — три отдельные модели, а не одна: выбираем по входным данным.

    - видео-референс           → reference-to-video (видео + до 4 фото)
    - фото-референсы           → reference-to-video (до 7 фото)
    - первый/последний кадр    → image-to-video (image_urls: [первый, последний])
    - только текст             → text-to-video
    Поле resolution у Omni — в нижнем регистре ("4k"); поля mode у него нет.
    """
    frames = [u for u in (first_frame_url, last_frame_url) if u]
    refs = list(style_reference_urls or [])
    res = (resolution or "720p").lower()
    ratio = aspect_ratio or "16:9"
    input_data: dict = {"prompt": prompt, "resolution": res}

    if video_reference_urls:
        model = "kling-3.0-omni/reference-to-video"
        input_data["video_urls"] = list(video_reference_urls)[:1]
        images = (frames + refs)[:4]
        if images:
            input_data.update(image_urls=images, duration=duration, aspect_ratio=ratio)
        else:
            # Только видео: KIE требует aspect_ratio="auto", длительность берётся из видео
            input_data["aspect_ratio"] = "auto"
        # С видео-входом звук у Omni всегда выключен
        input_data["audio"] = False
        return model, input_data

    input_data.update(duration=duration, aspect_ratio=ratio, audio=bool(audio))
    if refs:
        model = "kling-3.0-omni/reference-to-video"
        input_data["image_urls"] = (frames + refs)[:7]
    elif frames:
        model = "kling-3.0-omni/image-to-video"
        input_data["image_urls"] = frames
    else:
        model = "kling-3.0-omni/text-to-video"
    return model, input_data


WAN27_EDIT_ALIAS = "wan/2-7-video-to-video"


def _wan27_videoedit_request(
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    audio: bool,
    video_url: str,
    style_reference_urls: list[str] | None,
) -> tuple[str, dict]:
    """Wan 2.7 Video Edit у KIE: модель wan/2-7-videoedit, видео — строкой video_url,
    референс — один reference_image (не списком). audio_setting: auto | origin."""
    input_data: dict = {
        "prompt": prompt,
        "video_url": video_url,
        "resolution": (resolution or "720p").lower(),
        "duration": int(duration),
        "audio_setting": "auto" if audio else "origin",
    }
    if aspect_ratio:
        input_data["aspect_ratio"] = aspect_ratio
    if style_reference_urls:
        input_data["reference_image"] = style_reference_urls[0]
    return "wan/2-7-videoedit", input_data


MINIMAX_H3_GEN_ALIAS = "minimax-h3/text-to-video"


def _minimax_h3_request(
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    first_frame_url: str | None,
    last_frame_url: str | None,
    style_reference_urls: list[str] | None,
    video_reference_urls: list[str] | None,
    audio_reference_urls: list[str] | None,
) -> tuple[str, dict]:
    """MiniMax H3 у KIE — три отдельные модели: выбираем по входным данным.

    - видео/фото-референсы    → reference-to-video (reference_*_urls; кадры идут как фото)
    - первый/последний кадр   → image-to-video (без aspect_ratio)
    - только текст            → text-to-video (aspect_ratio обязателен)
    Разрешение у KIE — "768P" / "2K" (заглавная P). Поля audio у MiniMax нет.
    """
    res = (resolution or "2K").upper()
    frames = [u for u in (first_frame_url, last_frame_url) if u]
    images = (frames + list(style_reference_urls or []))[:9]
    videos = list(video_reference_urls or [])[:3]
    input_data: dict = {"prompt": prompt, "duration": int(duration), "resolution": res}

    if videos or (style_reference_urls and images):
        if images:
            input_data["reference_image_urls"] = images
        if videos:
            input_data["reference_video_urls"] = videos
        if audio_reference_urls:
            input_data["reference_audio_urls"] = list(audio_reference_urls)[:3]
        input_data["aspect_ratio"] = aspect_ratio or "adaptive"
        return "minimax-h3/reference-to-video", input_data

    if frames:
        if first_frame_url:
            input_data["first_frame_url"] = first_frame_url
        if last_frame_url:
            input_data["last_frame_url"] = last_frame_url
        return "minimax-h3/image-to-video", input_data

    input_data["aspect_ratio"] = aspect_ratio or "16:9"
    return MINIMAX_H3_GEN_ALIAS, input_data


def _extract_url(body: dict) -> str | None:
    """Извлекает URL результата из callback-тела KIE."""
    import json as _json
    data = body.get("data") or body

    # Veo 3.1 формат: data.info.resultUrls (объект, не строка)
    info = data.get("info")
    if isinstance(info, dict):
        for key in ("resultUrls", "originUrls"):
            urls = info.get(key)
            if isinstance(urls, list) and urls:
                return urls[0]

    # Основной формат KIE: data.resultJson — JSON-строка {"resultUrls": ["url", ...]}
    result_json_str = data.get("resultJson")
    if result_json_str:
        try:
            result_obj = _json.loads(result_json_str)
            urls = result_obj.get("resultUrls") or result_obj.get("resultUrl")
            if isinstance(urls, list) and urls:
                return urls[0]
            if isinstance(urls, str) and urls:
                return urls
        except Exception:
            pass

    # Запасные форматы (legacy / другие модели)
    for key in ("resultList", "result_list", "results", "images", "videos"):
        lst = data.get(key)
        if isinstance(lst, list) and lst:
            url = lst[0].get("url") or lst[0].get("image_url") or lst[0].get("video_url")
            if url:
                return url
    for key in ("url", "image_url", "video_url", "output_url"):
        url = data.get(key)
        if url:
            return url
    return None


def _is_failed(body: dict) -> bool:
    data = body.get("data") or body
    # Современный формат KIE: data.state
    state = (data.get("state") or "").lower()
    if state in ("fail", "failed", "error"):
        return True
    # Легаси: data.status
    status = data.get("status") or body.get("status")
    if isinstance(status, str):
        return status.lower() in ("failed", "error", "fail")
    if isinstance(status, int):
        return status in (3, -1)
    # code 4xx/5xx от KIE = ошибка генерации
    code = body.get("code")
    if isinstance(code, int) and (400 <= code < 600):
        return True
    return False


class KieProvider(OpenAICompatProvider):
    provider_id = "kie"
    base_url = "https://api.kie.ai/v1"  # для chat через OpenAI-compat
    chat_model = "gpt-4o-mini"

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key)
        # callback base URL берётся из конфига при первом вызове
        self._callback_base: str | None = None

    def _get_callback_base(self) -> str:
        if self._callback_base is None:
            from config import config
            self._callback_base = getattr(config, "kie_callback_base_url", "").rstrip("/")
        return self._callback_base

    async def _create_job(self, model: str, input_data: dict, corr_id: str) -> str:
        """Создаёт job на KIE, возвращает taskId."""
        callback_url = f"{self._get_callback_base()}/kie/callback/{corr_id}"
        payload = {
            "model": model,
            "callBackUrl": callback_url,
            "input": input_data,
        }
        async with self._session(timeout=30) as session:
            async with session.post(f"{_KIE_API_BASE}/jobs/createTask", json=payload) as resp:
                if resp.status == 402:
                    raise ProviderUnavailableError("Недостаточно средств на балансе KIE")
                if resp.status == 400:
                    body = await resp.text()
                    if "content" in body.lower() or "policy" in body.lower():
                        raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности KIE")
                    raise ProviderUnavailableError(f"Ошибка KIE: {body[:200]}")
                if resp.status >= 400:
                    raise ProviderUnavailableError(f"KIE ответил HTTP {resp.status}")
                data = await resp.json()

        if data.get("code") != 200:
            logger.error(
                "KIE createTask failed: code=%s msg=%s model=%s input=%s",
                data.get("code"), data.get("msg"), model, input_data,
            )
            raise ProviderUnavailableError(f"KIE: {data.get('msg', 'неизвестная ошибка')}")
        task_id = data["data"]["taskId"]
        logger.info("KIE task created: model=%s taskId=%s", model, task_id)
        ctx = _pending_job_ctx.get()
        if ctx:
            try:
                from db.queries import save_pending_job
                await save_pending_job(corr_id, **ctx)
            except Exception as _e:
                logger.warning("KIE: не удалось сохранить контекст job %s: %s", corr_id, _e)
        return task_id

    async def _await_job(self, corr_id: str) -> dict:
        """Ждёт callback от KIE с таймаутом."""
        fut = register_pending(corr_id)
        try:
            result = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания результата")
        finally:
            unregister_pending(corr_id)
        return result

    async def _create_veo_job(self, model: str, input_data: dict, corr_id: str) -> str:
        """Создаёт Veo-задачу через /veo/generate (отдельный endpoint KIE).
        Этот endpoint принимает поля на верхнем уровне, не вложенными в 'input'."""
        callback_url = f"{self._get_callback_base()}/kie/callback/{corr_id}"
        # /veo/generate — поля на корневом уровне, не внутри "input"
        payload = {"model": model, "callBackUrl": callback_url, **input_data}
        async with self._session(timeout=30) as session:
            async with session.post(f"{_KIE_API_BASE}/veo/generate", json=payload) as resp:
                if resp.status == 402:
                    raise ProviderUnavailableError("Недостаточно средств на балансе KIE")
                if resp.status == 400:
                    body = await resp.text()
                    raise ProviderUnavailableError(f"KIE Veo ошибка запроса: {body[:200]}")
                if resp.status >= 400:
                    raise ProviderUnavailableError(f"KIE Veo ответил HTTP {resp.status}")
                data = await resp.json()
        if data.get("code") != 200:
            logger.error("KIE Veo create failed: code=%s msg=%s model=%s", data.get("code"), data.get("msg"), model)
            raise ProviderUnavailableError(f"KIE Veo: {data.get('msg', 'неизвестная ошибка')}")
        task_id = data["data"]["taskId"]
        logger.info("KIE Veo task created: model=%s taskId=%s", model, task_id)
        ctx = _pending_job_ctx.get()
        if ctx:
            try:
                from db.queries import save_pending_job
                await save_pending_job(corr_id, **ctx)
            except Exception as _e:
                logger.warning("KIE: не удалось сохранить контекст Veo job %s: %s", corr_id, _e)
        return task_id

    # ─── Генерация аудио ──────────────────────────────────────────────────────

    async def generate_audio(
        self, prompt: str, audio_type: str = "voice", model: str | None = None,
        music_params: dict | None = None,
    ) -> GenerationResult:
        actual_model = model or "elevenlabs/text-to-dialogue-v3"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        # elevenlabs/text-to-dialogue-v3 требует массив dialogue с voice ID
        voice_id = "EkK5I93UQWFDigLMpZcX"
        stability = 0.5
        language_code = None
        dialogue_mode = False
        voice_id_2 = voice_id
        if music_params and music_params.get("_provider_model") == "elevenlabs-v3":
            voice_id = music_params.get("voice_id") or voice_id
            stability = music_params.get("voice_stability", 0.5)
            raw_lang = music_params.get("voice_language", "auto")
            language_code = None if raw_lang == "auto" else raw_lang
            dialogue_mode = bool(music_params.get("voice_dialogue_mode"))
            voice_id_2 = music_params.get("voice_id_2") or voice_id

        if dialogue_mode:
            # Каждая непустая строка — отдельная реплика, голоса чередуются
            raw_lines = [l.strip() for l in prompt.splitlines() if l.strip()]
            dialogue = [
                {"text": line, "voice": voice_id if i % 2 == 0 else voice_id_2}
                for i, line in enumerate(raw_lines)
            ] or [{"text": prompt, "voice": voice_id}]
        else:
            dialogue = [{"text": prompt, "voice": voice_id}]

        input_data: dict = {"dialogue": dialogue, "stability": stability}
        if language_code:
            input_data["language_code"] = language_code

        try:
            await self._create_job(actual_model, input_data, corr_id)
            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT_AUDIO)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания аудио")
        finally:
            unregister_pending(corr_id)

        if _is_failed(callback_body):
            raise ProviderUnavailableError("KIE: генерация аудио завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            raise ProviderUnavailableError("KIE: не получен URL аудио")

        audio_bytes = await self._download(url)
        return GenerationResult(data=audio_bytes, mime_type="audio/mpeg", filename="audio.mp3")

    async def _download(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status >= 400:
                    raise ProviderUnavailableError("Не удалось скачать результат от KIE")
                return await resp.read()

    # ─── Генерация изображений ────────────────────────────────────────────────

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None, style_reference_urls: list[str] | None = None,
        quality: str | None = None,
    ) -> GenerationResult:
        actual_model = model or "nano-banana-2"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)  # регистрируем ДО создания job (без гонки)

        try:
            task_id = await self._create_job(
                actual_model,
                _image_input(actual_model, prompt, aspect_ratio, resolution, style_reference_urls),
                corr_id,
            )

            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания изображения")
        finally:
            unregister_pending(corr_id)

        data = callback_body.get("data", {})
        logger.info("KIE image callback: model=%s state=%s code=%s resultJson=%s",
                    actual_model, data.get("state"), callback_body.get("code"),
                    str(data.get("resultJson", ""))[:200])

        if _is_failed(callback_body):
            logger.error("KIE image failed: model=%s failMsg=%s", actual_model, data.get("failMsg"))
            raise ProviderUnavailableError("KIE: генерация завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            logger.error("KIE image: URL not found in callback. data keys=%s", list(data.keys()))
            raise ProviderUnavailableError("KIE: не получен URL изображения")

        image_bytes = await self._download(url)
        # Grok не принимает TG-URL при редактировании — сохраняем оригинальный CDN URL
        gen_image_url = url if actual_model.startswith("grok-imagine") else None
        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png",
                                provider_image_url=gen_image_url)

    # ─── Генерация видео ──────────────────────────────────────────────────────

    async def generate_video(
        self, prompt: str, duration: int = 5, model: str | None = None,
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
        actual_model = model or "kling-3.0/video"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        is_veo = actual_model.startswith("veo")
        is_kling = actual_model.startswith("kling")
        is_bytedance = actual_model.startswith("bytedance/")
        is_wan = actual_model.startswith("wan/")
        is_wan_prime = actual_model == "wan/3-0-video-prime"
        is_pixverse = actual_model.startswith("pixverse")
        is_google = actual_model.startswith("google/")
        is_grok_video = actual_model.startswith("grok-imagine-video")
        is_minimax = actual_model.startswith("minimax")

        # Bytedance (Seedance) требует "adaptive" когда задан первый/последний кадр;
        # Wan в image/reference-режиме не принимает aspect_ratio и resolution,
        # но video-to-video режим (video_reference_urls) принимает — не подавляем.
        # Minimax не принимает aspect_ratio в image-to-video режиме (первый/последний кадр).
        _has_frame = bool(first_frame_url or last_frame_url)
        _wan_video_edit = is_wan and bool(video_reference_urls)
        _wan_with_images = is_wan and not _wan_video_edit and (_has_frame or bool(style_reference_urls))
        _pixverse_video_edit = is_pixverse and bool(video_reference_urls)
        _minimax_with_frames = is_minimax and _has_frame
        _effective_ratio = (
            "adaptive"
            if is_bytedance and _has_frame
            else (aspect_ratio or "16:9")
        )
        input_data: dict = {
            "prompt": prompt,
            "duration": str(duration) if (is_kling or is_google) else duration,
        }
        if not _wan_with_images and not _pixverse_video_edit and not _minimax_with_frames:
            input_data["aspect_ratio"] = _effective_ratio
        # wan/3-0-video-prime не принимает поле resolution совсем (ни text, ни video-edit)
        # pixverse всегда использует поле quality (не resolution), во всех режимах
        if (is_kling or is_wan or is_bytedance or is_google or is_grok_video or is_pixverse or is_minimax) and not _wan_with_images and not is_wan_prime:
            if is_pixverse:
                input_data["quality"] = resolution or "720p"
            else:
                input_data["resolution"] = resolution or "720p"

        # ── Первый / последний кадр ──────────────────────────────────────────
        if is_bytedance:
            if first_frame_url:
                input_data["first_frame_url"] = first_frame_url
            if last_frame_url:
                input_data["last_frame_url"] = last_frame_url
        elif is_kling:
            if first_frame_url or last_frame_url:
                input_data["image_urls"] = [u for u in [first_frame_url, last_frame_url] if u]
        elif is_wan or is_pixverse or is_google or is_minimax:
            if first_frame_url:
                input_data["first_frame_url"] = first_frame_url
            if last_frame_url:
                input_data["last_frame_url"] = last_frame_url
        elif is_grok_video:
            # Grok uses image_urls for all images; first_frame occupies slot 0
            if first_frame_url:
                input_data["image_urls"] = [first_frame_url]

        # ── Фото-референсы (extra style refs) ───────────────────────────────
        if style_reference_urls:
            if is_bytedance:
                input_data["style_reference_urls"] = list(style_reference_urls)
            elif is_grok_video:
                # Append after first_frame (if any) — total cap 7 images
                existing = input_data.get("image_urls", [])
                input_data["image_urls"] = existing + list(style_reference_urls)
            elif is_wan_prime:
                # wan/3-0-video-prime не поддерживает image_urls — используем frame urls
                refs = list(style_reference_urls)
                if not input_data.get("first_frame_url"):
                    input_data["first_frame_url"] = refs[0]
                if len(refs) > 1 and not input_data.get("last_frame_url"):
                    input_data["last_frame_url"] = refs[1]
            else:
                # minimax-h3, wan, pixverse, google и прочие
                input_data["image_urls"] = list(style_reference_urls)

        # ── Формат вывода ────────────────────────────────────────────────────
        if output_format and is_bytedance:
            input_data["output_format"] = output_format

        # ── Ориентация персонажа (Kling Motion Control) ──────────────────────
        if character_orientation and "motion-control" in actual_model:
            input_data["character_orientation"] = character_orientation

        # ── Режим Kling (только для новых моделей вида kling-X.X/...) ────────
        # Старые модели kling/v*-{mode}-* кодируют режим в названии.
        # Новые (kling-3.0/video, kling-3.0-omni/video и др.) требуют явного mode.
        if is_kling and "-" in actual_model.split("/")[0] and "." in actual_model:
            input_data.setdefault("mode", "std")

        # ── Флаг аудио ───────────────────────────────────────────────────────
        # WAN video-to-video использует audio_setting: "auto"/"origin", а не audio: bool
        # Pixverse video edit использует generate_audio_switch
        if _wan_video_edit:
            input_data["audio_setting"] = "origin" if not audio else "auto"
        elif _pixverse_video_edit:
            input_data["generate_audio_switch"] = audio
        else:
            input_data["audio"] = audio

        # ── Аудио-референсы ─────────────────────────────────────────────────
        if audio_reference_urls:
            if is_google:
                # Gemini ожидает поле audio_ids
                input_data["audio_ids"] = list(audio_reference_urls)
            else:
                input_data["audio_urls"] = list(audio_reference_urls)

        # ── Видео-референсы ──────────────────────────────────────────────────
        if video_reference_urls:
            if is_google:
                # Gemini ожидает video_list с объектами {url, start, ends}
                input_data["video_list"] = [
                    {"url": url, "start": 0, "ends": duration}
                    for url in video_reference_urls
                ]
            else:
                input_data["video_urls"] = list(video_reference_urls)

        if actual_model == KLING_OMNI_GEN_ALIAS:
            # Omni собирается отдельно: три модели KIE с разными наборами полей
            actual_model, input_data = _kling_omni_request(
                prompt, duration, aspect_ratio, resolution, audio,
                first_frame_url, last_frame_url, style_reference_urls, video_reference_urls,
            )

        if actual_model == WAN27_EDIT_ALIAS and video_reference_urls:
            # Wan 2.7 Video Edit — отдельная модель KIE с другими именами полей
            actual_model, input_data = _wan27_videoedit_request(
                prompt, duration, aspect_ratio, resolution, audio,
                video_reference_urls[0], style_reference_urls,
            )

        if actual_model == MINIMAX_H3_GEN_ALIAS:
            # MiniMax H3 — тоже три модели KIE с разными полями
            actual_model, input_data = _minimax_h3_request(
                prompt, duration, aspect_ratio, resolution, first_frame_url, last_frame_url,
                style_reference_urls, video_reference_urls, audio_reference_urls,
            )

        try:
            if is_veo:
                # KIE Veo: resolution передаётся строчной (4k, не 4K)
                _veo_res = (resolution or "1080p").replace("K", "k")
                veo_input: dict = {
                    "prompt": prompt,
                    "duration": duration,
                    "resolution": _veo_res,
                    "aspect_ratio": aspect_ratio or "16:9",
                }
                if video_reference_urls:
                    veo_input["generationType"] = "REFERENCE_2_VIDEO"
                    veo_input["videoUrls"] = list(video_reference_urls)
                elif first_frame_url:
                    veo_input["generationType"] = "FIRST_AND_LAST_FRAMES_2_VIDEO"
                    veo_input["imageUrls"] = [first_frame_url]
                elif style_reference_urls:
                    if actual_model == "veo3":
                        # Quality не поддерживает REFERENCE_2_VIDEO
                        veo_input["generationType"] = "FIRST_AND_LAST_FRAMES_2_VIDEO"
                    else:
                        veo_input["generationType"] = "REFERENCE_2_VIDEO"
                    veo_input["imageUrls"] = list(style_reference_urls)
                else:
                    veo_input["generationType"] = "TEXT_2_VIDEO"
                veo_input["audio"] = audio
                await self._create_veo_job(actual_model, veo_input, corr_id)
            else:
                await self._create_job(actual_model, input_data, corr_id)

            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания видео")
        finally:
            unregister_pending(corr_id)

        if _is_failed(callback_body):
            _data = callback_body.get("data", {})
            _fail_msg = (_data.get("failMsg") or _data.get("fail_msg") or "").lower()
            logger.error("KIE video failed: model=%s failMsg=%s", actual_model, _fail_msg)
            if any(k in _fail_msg for k in ("validation", "policy", "safety", "content")):
                raise ProviderContentPolicyError("Изображение не прошло проверку безопасности — попробуйте другое фото")
            raise ProviderUnavailableError("KIE: генерация видео завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            logger.error("KIE video: URL not found. data keys=%s", list(callback_body.get("data", {}).keys()))
            raise ProviderUnavailableError("KIE: не получен URL видео")

        video_bytes = await self._download(url)
        if output_format == "mov":
            return GenerationResult(data=video_bytes, mime_type="video/quicktime", filename="video.mov")
        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    # ─── Редактирование изображений ───────────────────────────────────────────

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_urls: list[str] | None = None,
        provider_task_id: str | None = None, resolution: str | None = None,
        quality: str | None = None, aspect_ratio: str | None = None,
    ) -> GenerationResult:
        """Job-based редактирование через KIE createTask."""
        actual_model = model or "google/nano-banana-edit"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)
        _srefs = list(style_reference_urls) if style_reference_urls else []

        if actual_model.startswith("grok-imagine"):
            # Grok не принимает TG-URL — нужен оригинальный CDN URL от KIE.
            # provider_task_id здесь — это сохранённый CDN URL (provider_image_url из генерации).
            src_url = provider_task_id or image_url
            if not src_url:
                raise ProviderUnavailableError(
                    "Grok Image: для редактирования нужно сначала сгенерировать фото этой же моделью"
                )
            input_data: dict = {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio or "1:1",
                "image_urls": [src_url] + _srefs,
            }
        elif actual_model.startswith("flux-2/"):
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "input_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "auto",
                "resolution": resolution or "1K",
            }
        elif actual_model.startswith("seedream/") and "image-to-image" in actual_model:
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "1:1",
                "quality": _SEEDREAM_QUALITY.get(resolution or "2K", "basic"),
                "output_format": "png",
            }
        elif actual_model == "nano-banana-2-lite":
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "auto",
            }
        elif actual_model == "nano-banana-pro":
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            # nano-banana-pro использует image_input (массив URL), а не image_urls
            input_data = {
                "prompt": prompt,
                "image_input": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "1:1",
                "resolution": resolution or "1K",
                "output_format": "png",
            }
        elif actual_model in ("gpt-image-2-image-to-image", "gpt-image-2-5-flare-image-to-image", "gpt-image-2-5-sunburst-image-to-image"):
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "input_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "auto",
                "resolution": resolution or "1K",
            }
        else:
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            # google/nano-banana-edit и прочие — поддерживает несколько ориентиров
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio,
                "output_format": "png",
            }
            if not aspect_ratio:
                input_data.pop("aspect_ratio")

        try:
            await self._create_job(actual_model, input_data, corr_id)
            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания редактирования")
        finally:
            unregister_pending(corr_id)

        data = callback_body.get("data", {})
        logger.info("KIE edit callback: model=%s state=%s code=%s resultJson=%s",
                    actual_model, data.get("state"), callback_body.get("code"),
                    str(data.get("resultJson", ""))[:200])

        if _is_failed(callback_body):
            logger.error("KIE edit failed: model=%s failMsg=%s", actual_model, data.get("failMsg"))
            raise ProviderUnavailableError("KIE: редактирование завершилось с ошибкой")

        result_url = _extract_url(callback_body)
        if not result_url:
            logger.error("KIE edit: URL not found in callback. data keys=%s", list(data.keys()))
            raise ProviderUnavailableError("KIE: не получен URL результата редактирования")

        image_out = await self._download(result_url)
        return GenerationResult(data=image_out, mime_type="image/png", filename="edited.png")

    # ─── Редактирование видео ─────────────────────────────────────────────────

    async def edit_video(
        self, video_bytes: bytes, prompt: str, model: str | None = None,
        video_url: str | None = None,
        duration: int = 5,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        audio: bool = True,
        audio_url: str | None = None,
        style_reference_urls: list[str] | None = None,
    ) -> GenerationResult:
        if not video_url:
            raise ProviderUnavailableError("KIE edit_video: не передан URL видео")
        return await self.generate_video(
            prompt=prompt,
            duration=duration,
            model=model,
            video_reference_urls=[video_url],
            aspect_ratio=aspect_ratio or "16:9",
            resolution=resolution,
            audio=audio,
            audio_reference_urls=[audio_url] if audio_url else None,
            style_reference_urls=style_reference_urls,
        )
