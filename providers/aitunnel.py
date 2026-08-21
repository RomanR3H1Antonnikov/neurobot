"""
Адаптер для aitunnel.ru.
API совместим с OpenAI: https://aitunnel.ru/docs
Base URL: https://api.aitunnel.ru/v1
"""
import base64
import aiohttp
from providers.base import (
    AbstractProvider, GenerationResult, ChatResult,
    ProviderUnavailableError, ProviderContentPolicyError,
)

BASE_URL = "https://api.aitunnel.ru/v1"

# Маппинг моделей по задачам — заполняется по факту доступных моделей у агрегатора
IMAGE_MODEL = "flux-1-schnell"
VIDEO_MODEL = "wan2.1-t2v-14b"
AUDIO_MODEL = "tts-1"
IMAGE_EDIT_MODEL = "gpt-image-1"
CHAT_MODEL = "gpt-4o-mini"


class AitunnelProvider(AbstractProvider):
    provider_id = "aitunnel"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=self._headers, timeout=aiohttp.ClientTimeout(total=120))

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict:
        if response.status == 402:
            raise ProviderUnavailableError("Недостаточно средств на балансе агрегатора")
        if response.status == 400:
            body = await response.json()
            if "content_policy" in str(body).lower() or "safety" in str(body).lower():
                raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности")
            raise ProviderUnavailableError(f"Ошибка запроса: {body}")
        if response.status >= 500:
            raise ProviderUnavailableError("Агрегатор временно недоступен")
        if response.status >= 400:
            raise ProviderUnavailableError(f"Ошибка агрегатора: HTTP {response.status}")
        return await response.json()

    async def generate_image(self, prompt: str, aspect_ratio: str = "1:1") -> GenerationResult:
        size_map = {"1:1": "1024x1024", "9:16": "1024x1792", "16:9": "1792x1024"}
        size = size_map.get(aspect_ratio, "1024x1024")

        async with self._session() as session:
            async with session.post(f"{BASE_URL}/images/generations", json={
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "size": size,
                "response_format": "b64_json",
                "n": 1,
            }) as resp:
                data = await self._handle_response(resp)

        image_bytes = base64.b64decode(data["data"][0]["b64_json"])
        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")

    async def generate_video(self, prompt: str, duration: int = 5) -> GenerationResult:
        # Endpoint и параметры уточняются по документации aitunnel.ru для видео
        async with self._session() as session:
            async with session.post(f"{BASE_URL}/video/generations", json={
                "model": VIDEO_MODEL,
                "prompt": prompt,
                "duration": duration,
            }) as resp:
                data = await self._handle_response(resp)

        # Предполагаем, что возвращается URL или base64 — адаптируем по документации
        if "url" in data.get("data", [{}])[0]:
            video_url = data["data"][0]["url"]
            async with aiohttp.ClientSession() as s:
                async with s.get(video_url) as vresp:
                    video_bytes = await vresp.read()
        else:
            video_bytes = base64.b64decode(data["data"][0]["b64_video"])

        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    async def generate_audio(self, prompt: str, audio_type: str = "voice") -> GenerationResult:
        if audio_type == "voice":
            # TTS
            async with self._session() as session:
                async with session.post(f"{BASE_URL}/audio/speech", json={
                    "model": AUDIO_MODEL,
                    "input": prompt,
                    "voice": "alloy",
                    "response_format": "mp3",
                }) as resp:
                    if resp.status >= 400:
                        await self._handle_response(resp)
                    audio_bytes = await resp.read()
        else:
            # Генерация музыки — endpoint уточняется
            async with self._session() as session:
                async with session.post(f"{BASE_URL}/audio/generations", json={
                    "model": "music-gen",
                    "prompt": prompt,
                }) as resp:
                    data = await self._handle_response(resp)
            audio_bytes = base64.b64decode(data["data"][0]["b64_audio"])

        return GenerationResult(data=audio_bytes, mime_type="audio/mpeg", filename="audio.mp3")

    async def edit_image(self, image_bytes: bytes, prompt: str) -> GenerationResult:
        form = aiohttp.FormData()
        form.add_field("model", IMAGE_EDIT_MODEL)
        form.add_field("prompt", prompt)
        form.add_field("image", image_bytes, filename="image.png", content_type="image/png")
        form.add_field("response_format", "b64_json")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(f"{BASE_URL}/images/edits", data=form) as resp:
                data = await self._handle_response(resp)

        image_out = base64.b64decode(data["data"][0]["b64_json"])
        return GenerationResult(data=image_out, mime_type="image/png", filename="edited.png")

    async def chat(self, messages: list[dict], system: str = "") -> ChatResult:
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        async with self._session() as session:
            async with session.post(f"{BASE_URL}/chat/completions", json={
                "model": CHAT_MODEL,
                "messages": payload_messages,
            }) as resp:
                data = await self._handle_response(resp)

        return ChatResult(text=data["choices"][0]["message"]["content"])
