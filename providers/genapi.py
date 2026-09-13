"""
Адаптер для GenAPI (api.gen-api.ru).
Использует callback-based API: POST /api/v1/networks/{model} с callback_url →
ждёт POST на наш /genapi/callback/{corr_id}.
"""
import asyncio
import logging
import uuid
import aiohttp
from providers.base import AbstractProvider, GenerationResult, ChatResult, ProviderUnavailableError, ProviderContentPolicyError
from services.kie_webhook import register_pending, unregister_pending

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.gen-api.ru/api/v1"
_CALLBACK_TIMEOUT = 600  # 10 минут


def _extract_url(body: dict) -> str | None:
    """Извлекает URL результата из callback-тела GenAPI."""
    result = body.get("result")
    # GenAPI возвращает result как список URL-строк напрямую
    if isinstance(result, list) and result:
        item = result[0]
        return item if isinstance(item, str) else item.get("url")
    # Fallback: result как словарь или другие варианты вложенности
    for container in (result, body.get("data"), body):
        if not isinstance(container, dict):
            continue
        for key in ("url", "image_url", "video_url", "audio_url", "output_url", "file_url"):
            if container.get(key):
                return container[key]
        for key in ("urls", "images", "videos", "files", "outputs"):
            lst = container.get(key)
            if isinstance(lst, list) and lst:
                item = lst[0]
                return item if isinstance(item, str) else item.get("url")
    return None


def _is_failed(body: dict) -> bool:
    status = (body.get("status") or "").lower()
    return status in ("failed", "error", "cancelled", "rejected")


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

    def _callback_base(self) -> str:
        from config import config
        return getattr(config, "kie_callback_base_url", "https://bot.rehy.ru").rstrip("/")

    async def _run(self, model: str, prompt: str, extra: dict | None = None) -> dict:
        """POST /networks/{model} с callback_url → ждёт callback."""
        corr_id = uuid.uuid4().hex
        callback_url = f"{self._callback_base()}/genapi/callback/{corr_id}"

        payload = {"prompt": prompt, "callback_url": callback_url}
        if extra:
            payload.update(extra)

        fut = register_pending(corr_id)
        try:
            async with aiohttp.ClientSession(
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as session:
                async with session.post(f"{_BASE_URL}/networks/{model}", json=payload) as resp:
                    if resp.status == 402:
                        raise ProviderUnavailableError("Недостаточно средств на балансе GenAPI")
                    if resp.status == 400 or resp.status == 422:
                        body = await resp.text()
                        if "content" in body.lower() or "policy" in body.lower():
                            raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности GenAPI")
                        raise ProviderUnavailableError(f"GenAPI ошибка запроса: {body[:300]}")
                    if resp.status >= 400:
                        raise ProviderUnavailableError(f"GenAPI ответил HTTP {resp.status}")
                    task = await resp.json()

            logger.info("GenAPI task created: model=%s, id=%s", model, task.get("id"))

            result = await asyncio.wait_for(fut, timeout=_CALLBACK_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("GenAPI: истекло время ожидания результата")
        finally:
            unregister_pending(corr_id)

        logger.info("GenAPI callback body keys: %s", list(result.keys()))
        logger.info("GenAPI callback result field: %s", str(result.get("result"))[:500])

        if _is_failed(result):
            raise ProviderUnavailableError("GenAPI: задача завершилась с ошибкой")

        return result

    # ─── Генерация изображений ────────────────────────────────────────────────

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None, style_reference_urls: list[str] | None = None,
    ) -> GenerationResult:
        actual_model = model or "midjourney"
        # model_id формата "network/version" → URL /networks/{network}, тело {"model": version}
        if "/" in actual_model:
            network, version = actual_model.split("/", 1)
            extra: dict | None = {"model": version}
        else:
            network, extra = actual_model, None
        # Midjourney принимает URL ориентиров в начале промпта (пробел-разделитель)
        _refs_prefix = " ".join(style_reference_urls) + " " if style_reference_urls else ""
        effective_prompt = f"{_refs_prefix}{prompt}"
        data = await self._run(network, effective_prompt, extra=extra)

        result = data.get("result")
        # GenAPI возвращает список URL в result — качаем все параллельно
        if isinstance(result, list) and result:
            urls = [u for u in result if isinstance(u, str)]
        else:
            url = _extract_url(data)
            if not url:
                raise ProviderUnavailableError(f"GenAPI: не получен URL изображения (тело: {str(data)[:200]})")
            urls = [url]

        images = await asyncio.gather(*[self._download(u) for u in urls])
        return GenerationResult(
            data=images[0],
            mime_type="image/png",
            filename="image.png",
            variants=list(images[1:]) if len(images) > 1 else None,
        )

    # ─── Генерация аудио ──────────────────────────────────────────────────────

    async def generate_audio(
        self, prompt: str, audio_type: str = "music", model: str | None = None,
        music_params: dict | None = None,
    ) -> GenerationResult:
        actual_model = model or "udio"
        extra: dict | None = None
        if music_params:
            extra = {}
            duration = music_params.get("music_duration")
            if duration:
                extra["music_length"] = duration
            if music_params.get("music_instrumental"):
                extra["Force_instrumental"] = True
            if music_params.get("music_respect_durations"):
                extra["respect_sections_durations"] = True
            fmt = music_params.get("music_format")
            if fmt:
                extra["output_format"] = fmt
            composition_plan: dict = {}
            pos_styles = music_params.get("music_positive_styles") or []
            neg_styles = music_params.get("music_negative_styles") or []
            sections = music_params.get("music_sections") or []
            if pos_styles:
                composition_plan["positive_styles"] = pos_styles
            if neg_styles:
                composition_plan["negative_styles"] = neg_styles
            if sections:
                composition_plan["sections"] = [{"prompt": s} for s in sections]
            if composition_plan:
                extra["composition_plan"] = composition_plan
            if not extra:
                extra = None

        data = await self._run(actual_model, prompt, extra=extra)

        url = _extract_url(data)
        if not url:
            raise ProviderUnavailableError(f"GenAPI: не получен URL аудио (тело: {str(data)[:200]})")

        fmt_out = (music_params or {}).get("music_format", "")
        if fmt_out.startswith("pcm"):
            mime, filename = "audio/wav", "audio.wav"
        else:
            mime, filename = "audio/mpeg", "audio.mp3"

        audio_bytes = await self._download(url)
        return GenerationResult(data=audio_bytes, mime_type=mime, filename=filename)

    # ─── Редактирование и видео ───────────────────────────────────────────────

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_urls: list[str] | None = None,
        provider_task_id: str | None = None, resolution: str | None = None,
    ) -> GenerationResult:
        """Редактирование через Midjourney: URL изображения передаётся в начале промпта."""
        if not image_url:
            raise ProviderUnavailableError("GenAPI edit_image: не передан URL изображения")
        actual_model = model or "midjourney"
        if "/" in actual_model:
            network, version = actual_model.split("/", 1)
            extra: dict | None = {"model": version}
        else:
            network, extra = actual_model, None
        # Основное фото + ориентиры + промпт
        _all_urls = [image_url] + (list(style_reference_urls) if style_reference_urls else [])
        full_prompt = " ".join(_all_urls) + " " + prompt
        data = await self._run(network, full_prompt, extra=extra)

        result = data.get("result")
        if isinstance(result, list) and result:
            urls = [u for u in result if isinstance(u, str)]
        else:
            url = _extract_url(data)
            if not url:
                raise ProviderUnavailableError(f"GenAPI: не получен URL изображения (тело: {str(data)[:200]})")
            urls = [url]

        images = await asyncio.gather(*[self._download(u) for u in urls])
        return GenerationResult(
            data=images[0],
            mime_type="image/png",
            filename="edited.png",
            variants=list(images[1:]) if len(images) > 1 else None,
        )

    async def edit_video(self, video_bytes: bytes, prompt: str, model: str | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Редактирование видео через GenAPI не поддерживается")

    async def generate_video(self, prompt: str, duration: int = 5, model: str | None = None, style_reference_urls: list[str] | None = None, aspect_ratio: str | None = None, resolution: str | None = None, audio_reference_urls: list[str] | None = None, first_frame_url: str | None = None, last_frame_url: str | None = None, video_reference_urls: list[str] | None = None, output_format: str | None = None) -> GenerationResult:
        raise ProviderUnavailableError("Генерация видео через GenAPI не поддерживается")

    # ─── Загрузка результата ──────────────────────────────────────────────────

    async def _download(self, url: str) -> bytes:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.get(url) as resp:
                if resp.status >= 400:
                    raise ProviderUnavailableError("GenAPI: не удалось скачать результат")
                return await resp.read()

    async def chat(self, messages: list[dict], system: str = "", model: str | None = None) -> ChatResult:
        raise ProviderUnavailableError("Чат через GenAPI не настроен")
