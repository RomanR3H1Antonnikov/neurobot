"""
Адаптер для RouteAI (routerai.ru).
Чат — OpenAI-compat (/chat/completions).
Видео — асинхронный API: POST /videos → поллинг GET /videos/{id} → GET /videos/{id}/content.
"""
import asyncio
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

    async def generate_video(
        self, prompt: str, duration: int = 5, model: str | None = None,
        aspect_ratio: str = "16:9", resolution: str = "720p",
    ) -> GenerationResult:
        actual_model = model or "alibaba/happyhorse-1.1"

        payload = {
            "model": actual_model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
        }

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

    async def generate_audio(self, prompt: str, audio_type: str = "voice", model: str | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Генерация аудио через RouteAI не настроена")
