"""
Адаптер для GenAPI (api.gen-api.ru).
Использует кастомный API: POST /api/v1/networks/{model} → поллинг GET /api/v1/requests/{id}.
"""
import asyncio
import logging
import aiohttp
from providers.base import AbstractProvider, GenerationResult, ChatResult, ProviderUnavailableError, ProviderContentPolicyError

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.gen-api.ru/api/v1"
_POLL_INTERVAL = 10   # секунд между запросами статуса
_POLL_TIMEOUT = 600   # максимальное ожидание (10 минут)

_DONE_STATUSES = {"success", "completed", "done", "finished"}
_FAIL_STATUSES = {"failed", "error", "cancelled", "rejected"}


def _extract_url(data: dict) -> str | None:
    """Извлекает URL результата из ответа GenAPI (несколько возможных форматов)."""
    result = data.get("result") or data.get("data") or data
    if isinstance(result, dict):
        for key in ("url", "image_url", "video_url", "audio_url", "output_url"):
            if result.get(key):
                return result[key]
        # result.urls = [...]
        urls = result.get("urls") or result.get("images") or result.get("videos")
        if isinstance(urls, list) and urls:
            return urls[0] if isinstance(urls[0], str) else urls[0].get("url")
    if isinstance(result, list) and result:
        item = result[0]
        return item if isinstance(item, str) else item.get("url")
    return None


class GenApiProvider(AbstractProvider):
    provider_id = "genapi"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post_network(self, model: str, prompt: str, extra: dict | None = None) -> dict:
        """POST /networks/{model} → возвращает тело ответа."""
        payload = {"prompt": prompt, "callback_url": None}
        if extra:
            payload.update(extra)

        async with aiohttp.ClientSession(
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            async with session.post(f"{_BASE_URL}/networks/{model}", json=payload) as resp:
                if resp.status == 402:
                    raise ProviderUnavailableError("Недостаточно средств на балансе GenAPI")
                if resp.status == 400:
                    body = await resp.text()
                    if "content" in body.lower() or "policy" in body.lower():
                        raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности GenAPI")
                    raise ProviderUnavailableError(f"GenAPI ошибка запроса: {body[:200]}")
                if resp.status >= 400:
                    raise ProviderUnavailableError(f"GenAPI ответил HTTP {resp.status}")
                return await resp.json()

    async def _poll_request(self, request_id: str) -> dict:
        """Поллинг GET /requests/{id} до завершения задачи."""
        elapsed = 0
        async with aiohttp.ClientSession(
            headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            while elapsed < _POLL_TIMEOUT:
                await asyncio.sleep(_POLL_INTERVAL)
                elapsed += _POLL_INTERVAL

                async with session.get(f"{_BASE_URL}/requests/{request_id}") as resp:
                    if resp.status >= 400:
                        logger.warning("GenAPI poll error: HTTP %d for request %s", resp.status, request_id)
                        continue
                    data = await resp.json()

                status = (data.get("status") or "").lower()
                logger.info("GenAPI request %s: status=%s (elapsed=%ds)", request_id, status, elapsed)

                if status in _DONE_STATUSES:
                    return data
                if status in _FAIL_STATUSES:
                    raise ProviderUnavailableError(f"GenAPI: задача завершилась с ошибкой ({status})")

        raise ProviderUnavailableError("GenAPI: истекло время ожидания результата")

    async def _run(self, model: str, prompt: str, extra: dict | None = None) -> dict:
        """Запускает задачу и при необходимости дожидается результата через поллинг."""
        data = await self._post_network(model, prompt, extra)

        # Если результат уже есть в ответе — возвращаем сразу
        if _extract_url(data):
            return data

        # Иначе ищем ID для поллинга
        request_id = data.get("id") or data.get("request_id") or data.get("task_id")
        if not request_id:
            raise ProviderUnavailableError("GenAPI: не получен id задачи и URL результата")

        logger.info("GenAPI task created: id=%s, model=%s", request_id, model)
        return await self._poll_request(request_id)

    # ─── Генерация изображений ────────────────────────────────────────────────

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K", model: str | None = None
    ) -> GenerationResult:
        actual_model = model or "midjourney"
        data = await self._run(actual_model, prompt, {"aspect_ratio": aspect_ratio})

        url = _extract_url(data)
        if not url:
            raise ProviderUnavailableError("GenAPI: не получен URL изображения")

        image_bytes = await self._download(url)
        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")

    # ─── Генерация аудио ──────────────────────────────────────────────────────

    async def generate_audio(
        self, prompt: str, audio_type: str = "music", model: str | None = None
    ) -> GenerationResult:
        actual_model = model or "udio"
        data = await self._run(actual_model, prompt)

        url = _extract_url(data)
        if not url:
            raise ProviderUnavailableError("GenAPI: не получен URL аудио")

        audio_bytes = await self._download(url)
        return GenerationResult(data=audio_bytes, mime_type="audio/mpeg", filename="audio.mp3")

    # ─── Загрузка результата ──────────────────────────────────────────────────

    async def _download(self, url: str) -> bytes:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.get(url) as resp:
                if resp.status >= 400:
                    raise ProviderUnavailableError("GenAPI: не удалось скачать результат")
                return await resp.read()

    # ─── Чат (GenAPI вряд ли используется для чата — заглушка) ──────────────

    async def chat(self, messages: list[dict], system: str = "", model: str | None = None) -> ChatResult:
        raise ProviderUnavailableError("Чат через GenAPI не настроен")
