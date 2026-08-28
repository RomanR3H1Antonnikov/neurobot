"""
Адаптер для KIE (kie.ai).
Использует job-based API: POST /api/v1/jobs/createTask → ждёт callback.
Callback приходит на наш webhook-сервер (services/kie_webhook.py).
"""
import asyncio
import uuid
import aiohttp
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult, ProviderUnavailableError, ProviderContentPolicyError
from services.kie_webhook import register_pending, unregister_pending

_KIE_API_BASE = "https://api.kie.ai/api/v1"
_JOB_TIMEOUT = 600  # секунд ожидания callback'а (10 минут)

# Формат соотношения сторон для KIE: "1:1" → "1:1" (совпадает), "auto" для произвольного
_RATIO_MAP: dict[str, str] = {}  # пустой = передаём as-is


def _kie_ratio(aspect_ratio: str) -> str:
    return _RATIO_MAP.get(aspect_ratio, aspect_ratio)


def _extract_url(body: dict) -> str | None:
    """Извлекает URL результата из callback-тела KIE (несколько возможных форматов)."""
    # Формат 1: data.resultList[0].url
    data = body.get("data") or body
    for key in ("resultList", "result_list", "results", "images", "videos"):
        lst = data.get(key)
        if isinstance(lst, list) and lst:
            url = lst[0].get("url") or lst[0].get("image_url") or lst[0].get("video_url")
            if url:
                return url
    # Формат 2: data.url / data.image_url / data.video_url
    for key in ("url", "image_url", "video_url", "output_url"):
        url = data.get(key)
        if url:
            return url
    return None


def _is_failed(body: dict) -> bool:
    data = body.get("data") or body
    status = data.get("status") or body.get("status")
    if isinstance(status, str):
        return status.lower() in ("failed", "error", "fail")
    if isinstance(status, int):
        return status in (3, -1)
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
            raise ProviderUnavailableError(f"KIE: {data.get('msg', 'неизвестная ошибка')}")
        return data["data"]["taskId"]

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

    async def _download(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status >= 400:
                    raise ProviderUnavailableError("Не удалось скачать результат от KIE")
                return await resp.read()

    # ─── Генерация изображений ────────────────────────────────────────────────

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None,
    ) -> GenerationResult:
        actual_model = model or "nano-banana-2"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)  # регистрируем ДО создания job (без гонки)

        try:
            await self._create_job(actual_model, {
                "prompt": prompt,
                "image_input": [],
                "aspect_ratio": _kie_ratio(aspect_ratio),
                "resolution": resolution,
                "output_format": "png",
            }, corr_id)

            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания изображения")
        finally:
            unregister_pending(corr_id)

        if _is_failed(callback_body):
            raise ProviderUnavailableError("KIE: генерация завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            raise ProviderUnavailableError("KIE: не получен URL изображения")

        image_bytes = await self._download(url)
        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")

    # ─── Генерация видео ──────────────────────────────────────────────────────

    async def generate_video(
        self, prompt: str, duration: int = 5, model: str | None = None,
    ) -> GenerationResult:
        actual_model = model or "kling-v3"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        try:
            await self._create_job(actual_model, {
                "prompt": prompt,
                "duration": duration,
                "aspect_ratio": "16:9",
            }, corr_id)

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
