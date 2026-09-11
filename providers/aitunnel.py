import base64
import aiohttp
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult

# aitunnel ограничивает gpt-image-2: длинная сторона ≤ 3840px
_GPT2_LONG_EDGE = {"1K": 1024, "2K": 2048, "4K": 3840}


def _gpt2_size(aspect_ratio: str, resolution: str) -> str:
    """Вычисляет WxH для gpt-image-2, используя ДЛИННУЮ сторону как базу."""
    max_long = _GPT2_LONG_EDGE.get(resolution, 1024)
    try:
        w_r, h_r = map(int, aspect_ratio.split(":"))
    except Exception:
        return f"{max_long}x{max_long}"
    if w_r >= h_r:
        w = max_long
        h = max(64, round(max_long * h_r / w_r / 64) * 64)
    else:
        h = max_long
        w = max(64, round(max_long * w_r / h_r / 64) * 64)
    return f"{w}x{h}"


class AitunnelProvider(OpenAICompatProvider):
    provider_id = "aitunnel"
    base_url = "https://api.aitunnel.ru/v1"
    image_model = "flux-1-schnell"
    video_model = "wan2.1-t2v-14b"
    audio_model = "tts-1"
    image_edit_model = "gpt-image-1"
    chat_model = "gpt-4o-mini"

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None, style_reference_urls: list[str] | None = None,
    ) -> GenerationResult:
        actual_model = model or self.image_model
        if actual_model == "gpt-image-2":
            # gpt-image-2 через aitunnel: длинная сторона ≤ 3840px
            size = _gpt2_size(aspect_ratio, resolution)
            _refs = "\n".join(f"Reference image: {u}" for u in (style_reference_urls or []))
            effective_prompt = f"{_refs}\n{prompt}" if _refs else prompt
            async with self._session() as session:
                async with session.post(f"{self.base_url}/images/generations", json={
                    "model": actual_model,
                    "prompt": effective_prompt,
                    "size": size,
                    "response_format": "b64_json",
                    "n": 1,
                }) as resp:
                    data = await self._handle_response(resp)
            image_bytes = base64.b64decode(data["data"][0]["b64_json"])
            return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png")
        return await super().generate_image(prompt, aspect_ratio, resolution, model, style_reference_urls)

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_urls: list[str] | None = None,
        provider_task_id: str | None = None, resolution: str | None = None,
    ) -> GenerationResult:
        actual_model = model or self.image_edit_model
        if actual_model == "gpt-image-2":
            # gpt-image-2 edit: длинная сторона ≤ 3840px (edit всегда square)
            size = _gpt2_size("1:1", resolution or "1K")
            _refs = "\n".join(f"Reference image: {u}" for u in (style_reference_urls or []))
            effective_prompt = f"{_refs}\n{prompt}" if _refs else prompt
            form = aiohttp.FormData()
            form.add_field("model", actual_model)
            form.add_field("prompt", effective_prompt)
            form.add_field("image", image_bytes, filename="image.png", content_type="image/png")
            form.add_field("response_format", "b64_json")
            form.add_field("size", size)
            headers = {"Authorization": f"Bearer {self._api_key}"}
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(f"{self.base_url}/images/edits", data=form) as resp:
                    data = await self._handle_response(resp)
            image_out = base64.b64decode(data["data"][0]["b64_json"])
            return GenerationResult(data=image_out, mime_type="image/png", filename="edited.png")
        return await super().edit_image(image_bytes, prompt, model, image_url, style_reference_urls, provider_task_id, resolution)
