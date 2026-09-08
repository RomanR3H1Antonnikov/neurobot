"""
Адаптер для KIE (kie.ai).
Использует job-based API: POST /api/v1/jobs/createTask → ждёт callback.
Callback приходит на наш webhook-сервер (services/kie_webhook.py).
"""
import asyncio
import logging
import uuid
import aiohttp
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult, ProviderUnavailableError, ProviderContentPolicyError
from services.kie_webhook import register_pending, unregister_pending

logger = logging.getLogger(__name__)

_KIE_API_BASE = "https://api.kie.ai/api/v1"
_JOB_TIMEOUT = 600  # секунд ожидания callback'а (10 минут)

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
        }
        if style_reference_urls:
            payload["image_input"] = {
                "image_list": [{"image_url": u} for u in style_reference_urls],
            }
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


def _extract_url(body: dict) -> str | None:
    """Извлекает URL результата из callback-тела KIE."""
    import json as _json
    data = body.get("data") or body

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

    # ─── Генерация аудио ──────────────────────────────────────────────────────

    async def generate_audio(
        self, prompt: str, audio_type: str = "voice", model: str | None = None,
    ) -> GenerationResult:
        actual_model = model or "elevenlabs/text-to-dialogue-v3"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        # elevenlabs/text-to-dialogue-v3 требует массив dialogue с voice ID
        input_data = {
            "dialogue": [{"text": prompt, "voice": "EkK5I93UQWFDigLMpZcX"}],
            "stability": 0.5,
        }

        try:
            await self._create_job(actual_model, input_data, corr_id)
            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
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
    ) -> GenerationResult:
        actual_model = model or "kling-3.0/video"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        is_kling = actual_model.startswith("kling")
        is_bytedance = actual_model.startswith("bytedance/")
        is_wan = actual_model.startswith("wan/")
        is_pixverse = actual_model.startswith("pixverse")

        # Bytedance (Seedance) требует "adaptive" когда задан первый/последний кадр
        _effective_ratio = (
            "adaptive"
            if is_bytedance and (first_frame_url or last_frame_url)
            else (aspect_ratio or "16:9")
        )
        input_data: dict = {
            "prompt": prompt,
            "duration": str(duration) if is_kling else duration,
            "aspect_ratio": _effective_ratio,
        }
        if is_kling or is_wan:
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
        elif is_wan or is_pixverse:
            if first_frame_url:
                input_data["first_frame_url"] = first_frame_url
            if last_frame_url:
                input_data["last_frame_url"] = last_frame_url

        # ── Фото-референсы (extra style refs) ───────────────────────────────
        if style_reference_urls:
            if is_bytedance:
                input_data["style_reference_urls"] = list(style_reference_urls)
            else:
                # minimax-h3, wan, pixverse, happyhorse и прочие
                input_data["image_urls"] = list(style_reference_urls)

        # ── Аудио-референсы ─────────────────────────────────────────────────
        if audio_reference_urls:
            input_data["audio_urls"] = list(audio_reference_urls)

        # ── Видео-референсы ──────────────────────────────────────────────────
        if video_reference_urls:
            input_data["video_urls"] = list(video_reference_urls)

        try:
            await self._create_job(actual_model, input_data, corr_id)

            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания видео")
        finally:
            unregister_pending(corr_id)

        if _is_failed(callback_body):
            raise ProviderUnavailableError("KIE: генерация видео завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            raise ProviderUnavailableError("KIE: не получен URL видео")

        video_bytes = await self._download(url)
        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    # ─── Редактирование изображений ───────────────────────────────────────────

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_urls: list[str] | None = None,
        provider_task_id: str | None = None,
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
                "aspect_ratio": "1:1",
                "image_urls": [src_url] + _srefs,
            }
        elif actual_model.startswith("flux-2/"):
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "input_urls": [image_url] + _srefs,
                "aspect_ratio": "auto",
                "resolution": "1K",
            }
        elif actual_model.startswith("seedream/") and "image-to-image" in actual_model:
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": "1:1",
                "quality": "basic",
                "output_format": "png",
            }
        elif actual_model == "nano-banana-2-lite":
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": "auto",
            }
        else:
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            # google/nano-banana-edit и прочие — поддерживает несколько ориентиров
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "output_format": "png",
            }

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
