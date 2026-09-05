"""
Адаптер для bratuha.ru.
Собственный API операций с async-polling (не OpenAI-совместимый).
POST /api/v1/operations → polling GET /api/v1/operations/{id} → результат.
"""
import asyncio
import aiohttp
from providers.base import (
    AbstractProvider, GenerationResult, ChatResult,
    ProviderUnavailableError, ProviderContentPolicyError,
)

# Маппинг model_id → tool slug на bratuha.ru
_MODEL_TOOLS: dict[str, str] = {
    "veo3.1-lite":      "veo-3-1",
    "veo3.1-fast":      "veo-3-1",
    "veo3.1-quality":   "veo-3-1",
    "suno":             "suno",
    "nano-banana-pro":  "nano-banana-pro",
}


class BratuhaProvider(AbstractProvider):
    provider_id = "bratuha"
    _BASE_URL = "https://bratuha.ru/api/v1"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _session(self, timeout: int = 30) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        )

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict:
        if response.status == 401:
            raise ProviderUnavailableError("Неверный API-ключ Bratuha")
        if response.status == 402:
            raise ProviderUnavailableError("Недостаточно средств на балансе Bratuha")
        if response.status == 400:
            body = await response.text()
            if "content_policy" in body.lower() or "safety" in body.lower():
                raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности")
            raise ProviderUnavailableError(f"Ошибка запроса: {body[:200]}")
        if response.status >= 500:
            raise ProviderUnavailableError("Bratuha временно недоступен")
        if response.status >= 400:
            raise ProviderUnavailableError(f"Ошибка Bratuha: HTTP {response.status}")
        return await response.json()

    async def _create_operation(self, tool: str, input_data: dict) -> str:
        async with self._session() as session:
            async with session.post(f"{self._BASE_URL}/operations", json={
                "tool": tool,
                "input": input_data,
            }) as resp:
                data = await self._handle_response(resp)
        op_id = data.get("id") or data.get("operation_id")
        if not op_id:
            raise ProviderUnavailableError("Bratuha не вернул ID операции")
        return str(op_id)

    async def _poll_operation(self, operation_id: str, max_attempts: int = 60) -> dict:
        for _ in range(max_attempts):
            async with self._session() as session:
                async with session.get(f"{self._BASE_URL}/operations/{operation_id}") as resp:
                    data = await self._handle_response(resp)
            status = data.get("status", "")
            if status == "completed":
                return data.get("output") or data.get("result") or data
            if status in ("failed", "error"):
                raise ProviderUnavailableError("Bratuha: генерация завершилась с ошибкой")
            await asyncio.sleep(10)
        raise ProviderUnavailableError("Bratuha: истекло время ожидания генерации")

    async def generate_video(
        self,
        prompt: str,
        duration: int = 5,
        model: str | None = None,
        aspect_ratio: str = "16:9",
        resolution: str = "1080p",
    ) -> GenerationResult:
        actual_model = model or "veo3.1-lite"
        tool = _MODEL_TOOLS.get(actual_model, "veo-3-1")

        op_id = await self._create_operation(tool, {
            "generation_type": "text",
            "model": actual_model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        })
        result = await self._poll_operation(op_id)

        url = (
            result.get("url")
            or result.get("video_url")
            or ((result.get("videos") or [{}])[0]).get("url")
        )
        if not url:
            raise ProviderUnavailableError("Bratuha: не получен URL видео")

        async with aiohttp.ClientSession() as s:
            async with s.get(url) as vresp:
                video_bytes = await vresp.read()

        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    # ─── Генерация изображений ────────────────────────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        resolution: str = "1K",
        model: str | None = None,
        style_reference_urls: list[str] | None = None,
    ) -> GenerationResult:
        actual_model = model or "nano-banana-pro"
        tool = _MODEL_TOOLS.get(actual_model, actual_model)

        op_id = await self._create_operation(tool, {
            "mode": "normal",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "image_size": resolution,
        })
        result = await self._poll_operation(op_id)

        url = (
            result.get("url")
            or result.get("image_url")
            or ((result.get("images") or [{}])[0]).get("url")
        )
        if not url:
            raise ProviderUnavailableError("Bratuha: не получен URL изображения")

        async with aiohttp.ClientSession() as s:
            async with s.get(url) as iresp:
                image_bytes = await iresp.read()

        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")

    # ─── Методы, не поддерживаемые Bratuha в текущей версии ─────────────────

    async def generate_audio(self, prompt: str, audio_type: str = "voice", model: str | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Генерация аудио через Bratuha не поддерживается")

    async def edit_image(self, image_bytes: bytes, prompt: str, model: str | None = None,
                         image_url: str | None = None, style_reference_urls: list[str] | None = None,
                         provider_task_id: str | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Редактирование изображений через Bratuha не поддерживается")

    async def edit_video(self, video_bytes: bytes, prompt: str, model: str | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Редактирование видео через Bratuha не поддерживается")

    async def chat(self, messages: list[dict], system: str = "") -> ChatResult:
        raise ProviderUnavailableError("Чат через Bratuha не поддерживается")
