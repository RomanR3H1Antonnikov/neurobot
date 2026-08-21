"""
Адаптер для KIE (Kling AI).
Специализируется на генерации видео через модели Kling.
Документация: уточнить у клиента после получения ключей.
"""
import aiohttp
from providers.base import (
    AbstractProvider, GenerationResult, ChatResult,
    ProviderUnavailableError,
)

BASE_URL = "https://api.kie.ai/v1"  # уточнить

VIDEO_MODEL = "kling-v1"


class KieProvider(AbstractProvider):
    provider_id = "kie"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=self._headers, timeout=aiohttp.ClientTimeout(total=300))

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict:
        if response.status == 402:
            raise ProviderUnavailableError("Недостаточно средств на балансе агрегатора")
        if response.status >= 500:
            raise ProviderUnavailableError("Агрегатор временно недоступен")
        if response.status >= 400:
            raise ProviderUnavailableError(f"Ошибка агрегатора: HTTP {response.status}")
        return await response.json()

    async def generate_video(self, prompt: str, duration: int = 5) -> GenerationResult:
        # Kling AI может использовать polling: создаём задачу, опрашиваем статус
        async with self._session() as session:
            async with session.post(f"{BASE_URL}/video/generations", json={
                "model": VIDEO_MODEL,
                "prompt": prompt,
                "duration": duration,
            }) as resp:
                data = await self._handle_response(resp)

        # Если API возвращает task_id и нужен polling — реализовать здесь
        if "task_id" in data:
            video_bytes = await self._poll_task(data["task_id"])
        elif "url" in data.get("data", [{}])[0]:
            url = data["data"][0]["url"]
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as vresp:
                    video_bytes = await vresp.read()
        else:
            raise ProviderUnavailableError("Неожиданный формат ответа от KIE")

        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    async def _poll_task(self, task_id: str, max_attempts: int = 30) -> bytes:
        import asyncio
        async with self._session() as session:
            for _ in range(max_attempts):
                async with session.get(f"{BASE_URL}/video/tasks/{task_id}") as resp:
                    data = await self._handle_response(resp)
                status = data.get("status")
                if status == "completed":
                    url = data["result"]["url"]
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url) as vresp:
                            return await vresp.read()
                if status == "failed":
                    raise ProviderUnavailableError("Генерация видео завершилась с ошибкой")
                await asyncio.sleep(10)
        raise ProviderUnavailableError("Истекло время ожидания генерации видео")

    async def generate_image(self, prompt: str, aspect_ratio: str = "1:1") -> GenerationResult:
        raise ProviderUnavailableError("Генерация изображений через KIE не настроена")

    async def generate_audio(self, prompt: str, audio_type: str = "voice") -> GenerationResult:
        raise ProviderUnavailableError("Генерация аудио через KIE не настроена")

    async def edit_image(self, image_bytes: bytes, prompt: str) -> GenerationResult:
        raise ProviderUnavailableError("Редактирование изображений через KIE не настроена")

    async def chat(self, messages: list[dict], system: str = "") -> ChatResult:
        raise ProviderUnavailableError("Чат через KIE не поддерживается")
