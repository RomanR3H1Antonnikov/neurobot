"""
Адаптер для RouteAI (routerai.ru).
Чат — OpenAI-compat (/chat/completions).
Видео — асинхронный API: POST /videos → поллинг GET /videos/{id} → GET /videos/{id}/content.
Grok Image — генерация через /images/generations, редактирование через /images/edits.
"""
import asyncio
import base64
import logging
import aiohttp
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult, ProviderUnavailableError

logger = logging.getLogger(__name__)

_BASE_URL = "https://routerai.ru/api/v1"
_POLL_INTERVAL = 10   # секунд между запросами статуса
_POLL_TIMEOUT = 600   # максимальное ожидание (10 минут)

_DONE_STATUSES = {"completed", "succeeded", "done", "finished"}
_FAIL_STATUSES = {"failed", "error", "cancelled", "rejected"}


class RouteraiProvider(OpenAICompatProvider):
    provider_id = "routerai"
    base_url = _BASE_URL
    chat_model = "gpt-4o-mini"

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None, style_reference_urls: list[str] | None = None,
    ) -> GenerationResult:
        actual_model = model or self.image_model

        if actual_model.startswith("x-ai/"):
            extra_body: dict = {"aspect_ratio": aspect_ratio}
            if style_reference_urls:
                extra_body["input_references"] = [
                    {"type": "image_url", "image_url": {"url": u}}
                    for u in style_reference_urls
                ]
            body = {
                "model": actual_model,
                "prompt": prompt,
                "n": 1,
                "size": self._compute_size(aspect_ratio, resolution),
                "extra_body": extra_body,
            }
            async with self._session() as session:
                async with session.post(f"{self.base_url}/images/generations", json=body) as resp:
                    data = await self._handle_response(resp)
            image_bytes = base64.b64decode(data["data"][0]["b64_json"])
            return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")

        return await super().generate_image(prompt, aspect_ratio, resolution, model, style_reference_urls)

    async def generate_video(
        self, prompt: str, duration: int = 5, model: str | None = None,
        aspect_ratio: str | None = None, resolution: str | None = None,
        style_reference_urls: list[str] | None = None,
        audio_reference_urls: list[str] | None = None,
        first_frame_url: str | None = None,
        last_frame_url: str | None = None,
        video_reference_urls: list[str] | None = None,
        output_format: str | None = None,
    ) -> GenerationResult:
        actual_model = model or "alibaba/happyhorse-1.1"

        payload: dict = {
            "model": actual_model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio or "16:9",
            "duration": duration,
            "resolution": resolution or "720p",
        }
        if style_reference_urls:
            payload["image_urls"] = list(style_reference_urls)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            async with session.post(f"{_BASE_URL}/videos", json=payload) as resp:
                if resp.status == 402:
                    raise ProviderUnavailableError("Недостаточно средств на балансе RouteAI")
                if resp.status >= 400:
                    body = await resp.text()
                    raise ProviderUnavailableError(f"RouteAI ошибка создания задачи: {body[:200]}")
                task = await resp.json()

        task_id = task.get("id")
        if not task_id:
            raise ProviderUnavailableError("RouteAI: не получен id задачи")

        logger.info("RouteAI video task created: id=%s, model=%s", task_id, actual_model)

        # Поллинг статуса
        elapsed = 0
        async with aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            while elapsed < _POLL_TIMEOUT:
                await asyncio.sleep(_POLL_INTERVAL)
                elapsed += _POLL_INTERVAL

                async with session.get(f"{_BASE_URL}/videos/{task_id}") as resp:
                    if resp.status >= 400:
                        logger.warning("RouteAI poll error: HTTP %d for task %s", resp.status, task_id)
                        continue
                    data = await resp.json()

                status = (data.get("status") or "").lower()
                logger.info("RouteAI video %s: status=%s (elapsed=%ds)", task_id, status, elapsed)

                if status in _DONE_STATUSES:
                    break
                if status in _FAIL_STATUSES:
                    raise ProviderUnavailableError(f"RouteAI: генерация видео завершилась с ошибкой ({status})")
            else:
                raise ProviderUnavailableError("RouteAI: истекло время ожидания видео")

        # Скачиваем готовое видео
        async with aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as session:
            async with session.get(f"{_BASE_URL}/videos/{task_id}/content") as resp:
                if resp.status >= 400:
                    raise ProviderUnavailableError("RouteAI: не удалось скачать видео")
                video_bytes = await resp.read()

        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_urls: list[str] | None = None,
        provider_task_id: str | None = None, resolution: str | None = None,
    ) -> GenerationResult:
        actual_model = model or self.image_edit_model

        if actual_model.startswith("x-ai/"):
            if not image_url:
                raise ProviderUnavailableError("Grok Image edit: не передан URL исходного изображения")
            # x-ai /images/edits: основное фото в image, ориентиры в images (массив)
            body: dict = {
                "model": actual_model,
                "prompt": prompt,
                "image": {"url": image_url, "type": "image_url"},
            }
            if style_reference_urls:
                body["images"] = [{"url": u, "type": "image_url"} for u in style_reference_urls]
            headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(f"{_BASE_URL}/images/edits", json=body) as resp:
                    data = await self._handle_response(resp)
            image_out = base64.b64decode(data["data"][0]["b64_json"])
            return GenerationResult(data=image_out, mime_type="image/png", filename="edited.png")

        return await super().edit_image(image_bytes, prompt, model, image_url, style_reference_urls)

    async def generate_audio(self, prompt: str, audio_type: str = "voice", model: str | None = None, music_params: dict | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Генерация аудио через RouteAI не настроена")
