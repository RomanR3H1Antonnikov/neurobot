"""
Базовый класс для всех OpenAI-совместимых агрегаторов.
Конкретный адаптер указывает только: provider_id, base_url, и дефолтные модели.
Все методы принимают необязательный параметр model — он переопределяет дефолт класса.
"""
import base64
import aiohttp
from providers.base import (
    AbstractProvider, GenerationResult, ChatResult,
    ProviderUnavailableError, ProviderContentPolicyError,
)


class OpenAICompatProvider(AbstractProvider):
    base_url: str = ""
    image_model: str = "flux-1-schnell"
    video_model: str = "wan2.1-t2v-14b"
    audio_model: str = "tts-1"
    audio_voice: str = "alloy"
    image_edit_model: str = "gpt-image-1"
    video_edit_model: str = ""
    chat_model: str = "gpt-4o-mini"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _session(self, timeout: int = 120) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        )

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict:
        if response.status == 402:
            raise ProviderUnavailableError("Недостаточно средств на балансе агрегатора")
        if response.status == 400:
            body = await response.text()
            if "content_policy" in body.lower() or "safety" in body.lower():
                raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности")
            raise ProviderUnavailableError(f"Ошибка запроса: {body[:200]}")
        if response.status >= 500:
            raise ProviderUnavailableError("Агрегатор временно недоступен")
        if response.status >= 400:
            raise ProviderUnavailableError(f"Ошибка агрегатора: HTTP {response.status}")
        return await response.json()

    @staticmethod
    def _compute_size(aspect_ratio: str, resolution: str) -> str:
        base = {"1K": 1024, "2K": 2048, "4K": 4096}.get(resolution, 1024)
        try:
            w_r, h_r = map(int, aspect_ratio.split(":"))
        except Exception:
            return f"{base}x{base}"
        # Short side = base, long side aligned to nearest 64px
        if w_r >= h_r:
            h = base
            w = max(64, round(base * w_r / h_r / 64) * 64)
        else:
            w = base
            h = max(64, round(base * h_r / w_r / 64) * 64)
        return f"{w}x{h}"

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K", model: str | None = None
    ) -> GenerationResult:
        actual_model = model or self.image_model
        size = self._compute_size(aspect_ratio, resolution)

        async with self._session() as session:
            async with session.post(f"{self.base_url}/images/generations", json={
                "model": actual_model,
                "prompt": prompt,
                "size": size,
                "response_format": "b64_json",
                "n": 1,
            }) as resp:
                data = await self._handle_response(resp)

        image_bytes = base64.b64decode(data["data"][0]["b64_json"])
        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")

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

        item = data.get("data", [{}])[0]
        if "url" in item:
            async with aiohttp.ClientSession() as s:
                async with s.get(item["url"]) as vresp:
                    video_bytes = await vresp.read()
        else:
            video_bytes = base64.b64decode(item["b64_video"])

        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    async def generate_audio(
        self, prompt: str, audio_type: str = "voice", model: str | None = None
    ) -> GenerationResult:
        actual_model = model or self.audio_model

        if audio_type == "voice":
            async with self._session() as session:
                async with session.post(f"{self.base_url}/audio/speech", json={
                    "model": actual_model,
                    "input": prompt,
                    "voice": self.audio_voice,
                    "response_format": "mp3",
                }) as resp:
                    if resp.status >= 400:
                        await self._handle_response(resp)
                    audio_bytes = await resp.read()
        else:
            async with self._session() as session:
                async with session.post(f"{self.base_url}/audio/generations", json={
                    "model": actual_model,
                    "prompt": prompt,
                }) as resp:
                    data = await self._handle_response(resp)
            audio_bytes = base64.b64decode(data["data"][0]["b64_audio"])

        return GenerationResult(data=audio_bytes, mime_type="audio/mpeg", filename="audio.mp3")

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None,
    ) -> GenerationResult:
        actual_model = model or self.image_edit_model

        form = aiohttp.FormData()
        form.add_field("model", actual_model)
        form.add_field("prompt", prompt)
        form.add_field("image", image_bytes, filename="image.png", content_type="image/png")
        form.add_field("response_format", "b64_json")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(f"{self.base_url}/images/edits", data=form) as resp:
                data = await self._handle_response(resp)

        image_out = base64.b64decode(data["data"][0]["b64_json"])
        return GenerationResult(data=image_out, mime_type="image/png", filename="edited.png")

    async def edit_video(
        self, video_bytes: bytes, prompt: str, model: str | None = None
    ) -> GenerationResult:
        actual_model = model or self.video_edit_model

        form = aiohttp.FormData()
        form.add_field("model", actual_model)
        form.add_field("prompt", prompt)
        form.add_field("video", video_bytes, filename="video.mp4", content_type="video/mp4")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.post(f"{self.base_url}/video/edits", data=form) as resp:
                data = await self._handle_response(resp)

        item = data.get("data", [{}])[0]
        if "url" in item:
            async with aiohttp.ClientSession() as s:
                async with s.get(item["url"]) as vresp:
                    video_out = await vresp.read()
        else:
            video_out = base64.b64decode(item.get("b64_video", ""))

        return GenerationResult(data=video_out, mime_type="video/mp4", filename="edited.mp4")

    async def chat(
        self, messages: list[dict], system: str = "", model: str | None = None
    ) -> ChatResult:
        actual_model = model or self.chat_model
        payload = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(messages)

        async with self._session() as session:
            async with session.post(f"{self.base_url}/chat/completions", json={
                "model": actual_model,
                "messages": payload,
            }) as resp:
                data = await self._handle_response(resp)

        return ChatResult(text=data["choices"][0]["message"]["content"])
