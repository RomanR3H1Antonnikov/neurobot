"""
Адаптер для KIE (kie.ai).
Видео-генерация через polling (задача → опрос статуса).
Изображения, аудио, чат — через стандартный OpenAI-совместимый API.
"""
import asyncio
import aiohttp
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult, ProviderUnavailableError


class KieProvider(OpenAICompatProvider):
    provider_id = "kie"
    base_url = "https://api.kie.ai/v1"
    image_model = "nano-banana-2"
    video_model = "kling-v1"
    audio_model = "elevenlabs-v3"
    chat_model = "gpt-5.5"

    async def generate_video(
        self, prompt: str, duration: int = 5, model: str | None = None
    ) -> GenerationResult:
        actual_model = model or self.video_model

        async with self._session(timeout=300) as session:
            async with session.post(f"{self.base_url}/video/generations", json={
                "model": actual_model,
                "prompt": prompt,
                "duration": duration,
            }) as resp:
                data = await self._handle_response(resp)

        if "task_id" in data:
            video_bytes = await self._poll_task(data["task_id"])
        else:
            item = data.get("data", [{}])[0]
            if "url" in item:
                async with aiohttp.ClientSession() as s:
                    async with s.get(item["url"]) as vresp:
                        video_bytes = await vresp.read()
            else:
                raise ProviderUnavailableError("Неожиданный формат ответа от KIE")

        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    async def _poll_task(self, task_id: str, max_attempts: int = 30) -> bytes:
        async with self._session(timeout=30) as session:
            for _ in range(max_attempts):
                async with session.get(f"{self.base_url}/video/tasks/{task_id}") as resp:
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
