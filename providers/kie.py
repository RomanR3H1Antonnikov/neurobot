"""
Адаптер для KIE (kie.ai).
Использует job-based API: POST /api/v1/jobs/createTask → ждёт callback.
Callback приходит на наш webhook-сервер (services/kie_webhook.py).
"""
import asyncio
import logging
import uuid
import aiohttp
from contextvars import ContextVar
from providers.openai_compat import OpenAICompatProvider
from providers.base import GenerationResult, ChatResult, ProviderUnavailableError, ProviderContentPolicyError
from services.kie_webhook import register_pending, unregister_pending

# Устанавливается в _run_generation перед вызовом generate_*; читается в _create_job
_pending_job_ctx: ContextVar[dict | None] = ContextVar("_pending_job_ctx", default=None)


def set_pending_job_ctx(
    telegram_id: int, chat_id: int,
    model_label: str, media_type: str, prompt: str | None,
) -> None:
    _pending_job_ctx.set({
        "telegram_id": telegram_id, "chat_id": chat_id,
        "model_label": model_label, "media_type": media_type, "prompt": prompt,
    })

logger = logging.getLogger(__name__)

_KIE_API_BASE = "https://api.kie.ai/api/v1"
_JOB_TIMEOUT = 600        # секунд ожидания callback'а (10 минут)
_JOB_TIMEOUT_AUDIO = 1200  # 20 минут для аудио-генерации

# Формат соотношения сторон для KIE: "1:1" → "1:1" (совпадает), "auto" для произвольного
_RATIO_MAP: dict[str, str] = {}  # пустой = передаём as-is

# Маппинг resolution → quality для моделей Seedream (basic=2K, high=3K, ultra=4K)
_SEEDREAM_QUALITY: dict[str, str] = {
    "2K": "basic",
    "3K": "high",
    "4K": "ultra",
}


def _kie_ratio(aspect_ratio: str) -> str:
    return _RATIO_MAP.get(aspect_ratio, aspect_ratio)


def _image_input(
    model: str, prompt: str, aspect_ratio: str, resolution: str,
    style_reference_urls: list[str] | None = None,
) -> dict:
    """Формирует поле input для createTask в зависимости от семейства модели."""
    if model.startswith("seedream/"):
        return {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "quality": _SEEDREAM_QUALITY.get(resolution, "basic"),
            "output_format": "png",
        }
    if model.startswith("flux-2/"):
        # KIE docs: flux-2 text-to-image принимает только prompt/aspect_ratio/resolution.
        # image_input: [] вызывает 501 — Flex воспринимает его как пустой image-to-image.
        return {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "resolution": resolution,
        }
    if model == "nano-banana-2-lite":
        # Lite использует image_urls (не image_input) и не принимает resolution/output_format
        return {
            "prompt": prompt,
            "image_urls": list(style_reference_urls) if style_reference_urls else [],
            "aspect_ratio": _kie_ratio(aspect_ratio),
        }
    if model.startswith("grok-imagine-image-2-0/"):
        payload: dict = {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "resolution": resolution,
        }
        if style_reference_urls:
            payload["image_input"] = {
                "image_list": [{"image_url": u} for u in style_reference_urls],
            }
        return payload
    if model in ("gpt-image-2-text-to-image", "gpt-image-2-5-flare-text-to-image", "gpt-image-2-5-sunburst-text-to-image"):
        payload = {
            "prompt": prompt,
            "aspect_ratio": _kie_ratio(aspect_ratio),
            "resolution": resolution,
        }
        if style_reference_urls:
            payload["image_urls"] = list(style_reference_urls)
        return payload
    # Nano Banana 2 и прочие — стандартный формат; поддерживает до 14 ориентиров
    result: dict = {
        "prompt": prompt,
        "image_input": list(style_reference_urls) if style_reference_urls else [],
        "aspect_ratio": _kie_ratio(aspect_ratio),
        "resolution": resolution,
        "output_format": "png",
    }
    return result


KLING_OMNI_GEN_ALIAS = "kling-3.0-omni/video"


def _kling_omni_request(
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    audio: bool,
    first_frame_url: str | None,
    last_frame_url: str | None,
    style_reference_urls: list[str] | None,
    video_reference_urls: list[str] | None,
    characters: list[dict] | None = None,
    audio_reference_urls: list[str] | None = None,
) -> tuple[str, dict]:
    """Kling 3.0 Omni у KIE — три отдельные модели, а не одна: выбираем по входным данным.

    - видео-референс           → reference-to-video (видео + до 4 фото)
    - фото-референсы           → reference-to-video (до 7 фото)
    - первый/последний кадр    → image-to-video (image_urls: [первый, последний])
    - только текст             → text-to-video
    Поле resolution у Omni — в нижнем регистре ("4k"); поля mode у него нет.
    """
    frames = [u for u in (first_frame_url, last_frame_url) if u]
    refs = list(style_reference_urls or [])
    res = (resolution or "720p").lower()
    ratio = aspect_ratio or "16:9"
    input_data: dict = {"prompt": prompt, "resolution": res}

    if video_reference_urls:
        model = "kling-3.0-omni/reference-to-video"
        input_data["video_urls"] = list(video_reference_urls)[:1]
        images = (frames + refs)[:4]
        if images:
            input_data.update(image_urls=images, duration=duration, aspect_ratio=ratio)
        else:
            # Только видео: KIE требует aspect_ratio="auto", длительность берётся из видео
            input_data["aspect_ratio"] = "auto"
        # С видео-входом звук у Omni всегда выключен (audio_urls не передаём)
        input_data["audio"] = False
        if characters:
            input_data["elements"] = list(characters)
        return model, input_data

    input_data.update(duration=duration, aspect_ratio=ratio, audio=bool(audio))
    if audio_reference_urls:
        input_data["audio_urls"] = list(audio_reference_urls)[:1]
    if refs:
        model = "kling-3.0-omni/reference-to-video"
        input_data["image_urls"] = (frames + refs)[:7]
    elif frames:
        model = "kling-3.0-omni/image-to-video"
        input_data["image_urls"] = frames
        # KIE на деле принимает здесь только "auto" (формат берётся из кадра): при 16:9/9:16/1:1
        # ответ 422 «aspect_ratio must be auto for image-to-video without custom multi-shot»
        input_data["aspect_ratio"] = "auto"
    else:
        model = "kling-3.0-omni/text-to-video"
    if characters:
        # Персонажи — «элементы» Kling; поддерживаются всеми тремя вариантами Omni
        input_data["elements"] = list(characters)
    return model, input_data


KLING3_ALIAS = "kling-3.0/video"
KLING3_MOTION_ALIAS = "kling-3.0/motion-control"
_KLING3_MODE = {"720p": "std", "1080p": "pro", "4K": "4K", "4k": "4K"}


def _kling3_request(
    prompt: str, duration: int, aspect_ratio: str | None, resolution: str | None, audio: bool,
    first_frame_url: str | None, last_frame_url: str | None,
    characters: list[dict] | None = None,
) -> dict:
    """Kling 3.0 (kling-3.0/video) по документации KIE: mode вместо resolution (std=720p, pro=1080p),
    sound вместо audio, обязательное multi_shots; aspect_ratio — только 16:9 / 9:16 / 1:1."""
    ratio = aspect_ratio if aspect_ratio in ("16:9", "9:16", "1:1") else "16:9"
    input_data: dict = {
        "prompt": prompt,
        "duration": str(duration),
        "aspect_ratio": ratio,
        "mode": _KLING3_MODE.get(resolution or "720p", "std"),
        "sound": bool(audio),
        "multi_shots": False,
    }
    frames = [u for u in (first_frame_url, last_frame_url) if u]
    if frames:
        input_data["image_urls"] = frames
    if characters:
        # Персонажи — «элементы» Kling; в промпте на них ссылаются как @name
        input_data["kling_elements"] = list(characters)
    return input_data


def _kling3_motion_request(
    prompt: str, resolution: str | None, image_url: str | None, video_url: str | None,
    character_orientation: str | None,
) -> dict:
    """Kling 3.0 Motion Control: одно фото (input_urls) + одно видео с движением (video_urls).
    Длительность берётся из видео; duration/aspect_ratio/audio/mode модель не принимает."""
    if not image_url or not video_url:
        raise ProviderUnavailableError("Kling Motion Control: нужны и фото, и видео")
    return {
        "prompt": prompt,
        "input_urls": [image_url],
        "video_urls": [video_url],
        "character_orientation": character_orientation or "video",
        "background_source": "input_video" if (character_orientation or "video") == "video" else "input_image",
    }


HAPPYHORSE_EDIT = "happyhorse/video-edit"


def _happyhorse_edit_request(
    prompt: str, resolution: str | None, audio: bool, video_url: str, style_reference_urls: list[str] | None,
) -> dict:
    """HappyHorse Video Edit у KIE: video_url — строка, reference_image — до 5 фото (@image1…),
    audio_setting: auto (звук генерирует модель) / origin (звук исходного видео). Длительности в запросе нет."""
    input_data: dict = {
        "prompt": prompt,
        "video_url": video_url,
        "resolution": (resolution or "1080p").lower(),
        "audio_setting": "auto" if audio else "origin",
    }
    refs = list(style_reference_urls or [])[:5]
    if refs:
        input_data["reference_image"] = refs
    return input_data


KLING_OMNI_TRANSFORM = "kling-3.0-omni/transformation"


def _kling_omni_transformation_request(
    prompt: str, duration: int, aspect_ratio: str | None, resolution: str | None, audio: bool,
    style_reference_urls: list[str] | None, video_url: str,
) -> dict:
    """Kling 3.0 Omni Transformation (редактирование видео): длительность берётся из исходного видео,
    поле duration с одним видео KIE отклоняет (422), aspect_ratio — только auto. С фото-ориентирами
    (до 4) duration и 16:9/9:16/1:1 уже допустимы."""
    input_data: dict = {
        "prompt": prompt,
        "video_urls": [video_url],
        "resolution": (resolution or "720p").lower(),
        "audio": bool(audio),
    }
    images = list(style_reference_urls or [])[:4]
    if images:
        input_data["image_urls"] = images
        input_data["duration"] = str(duration)
        input_data["aspect_ratio"] = aspect_ratio if aspect_ratio in ("16:9", "9:16", "1:1") else "16:9"
    else:
        input_data["aspect_ratio"] = "auto"
    return input_data


SEEDANCE2_PREFIX = "bytedance/seedance-2"


def _seedance2_request(
    model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    audio: bool,
    output_format: str | None,
    first_frame_url: str | None,
    last_frame_url: str | None,
    style_reference_urls: list[str] | None,
    video_reference_urls: list[str] | None,
    audio_reference_urls: list[str] | None,
) -> dict:
    """Seedance 2.x (2, 2-fast, 2-mini, 2.5) у KIE. Названия полей по документации KIE:
    reference_image_urls / reference_video_urls / reference_audio_urls и generate_audio.
    Поля style_reference_urls, audio, video_urls, audio_urls у этих моделей НЕТ — KIE молча
    игнорирует неизвестные поля, поэтому раньше фото и видео-референсы в генерацию не попадали.

    Три взаимоисключающих сценария: первый/последний кадр — ИЛИ мультимодальные референсы.
    Если пользователь задал и кадры, и референсы, кадры уходят как обычные фото-референсы.
    """
    is_25 = model == "bytedance/seedance-2-5"
    max_img, max_vid, max_aud = (30, 10, 10) if is_25 else (9, 3, 3)
    images = list(style_reference_urls or [])
    videos = list(video_reference_urls or [])
    audios = list(audio_reference_urls or [])
    frames = [u for u in (first_frame_url, last_frame_url) if u]

    input_data: dict = {
        "prompt": prompt,
        "duration": int(duration),
        "resolution": (resolution or "720p").lower(),
        "generate_audio": bool(audio),
    }
    if is_25 and output_format:
        input_data["output_format"] = output_format

    if images or videos or audios:
        input_data["aspect_ratio"] = aspect_ratio or "adaptive"
        all_images = (frames + images)[:max_img]
        if all_images:
            input_data["reference_image_urls"] = all_images
        if videos:
            input_data["reference_video_urls"] = videos[:max_vid]
        if audios:
            input_data["reference_audio_urls"] = audios[:max_aud]
    elif frames:
        # Только «Оживить фото»: last_frame_url допустим лишь вместе с first_frame_url
        input_data["aspect_ratio"] = "adaptive"
        if first_frame_url:
            input_data["first_frame_url"] = first_frame_url
            if last_frame_url:
                input_data["last_frame_url"] = last_frame_url
        else:
            input_data["reference_image_urls"] = [last_frame_url]
    else:
        input_data["aspect_ratio"] = aspect_ratio or "16:9"
    return input_data


WAN27_EDIT_ALIAS = "wan/2-7-video-to-video"


def _wan27_videoedit_request(
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    audio: bool,
    video_url: str,
    style_reference_urls: list[str] | None,
) -> tuple[str, dict]:
    """Wan 2.7 Video Edit у KIE: модель wan/2-7-videoedit, видео — строкой video_url,
    референс — один reference_image (не списком). audio_setting: auto | origin."""
    input_data: dict = {
        "prompt": prompt,
        "video_url": video_url,
        "resolution": (resolution or "720p").lower(),
        "duration": int(duration),
        "audio_setting": "auto" if audio else "origin",
    }
    if aspect_ratio:
        input_data["aspect_ratio"] = aspect_ratio
    if style_reference_urls:
        input_data["reference_image"] = style_reference_urls[0]
    return "wan/2-7-videoedit", input_data


MINIMAX_H3_GEN_ALIAS = "minimax-h3/text-to-video"


def _minimax_h3_request(
    prompt: str,
    duration: int,
    aspect_ratio: str | None,
    resolution: str | None,
    first_frame_url: str | None,
    last_frame_url: str | None,
    style_reference_urls: list[str] | None,
    video_reference_urls: list[str] | None,
    audio_reference_urls: list[str] | None,
) -> tuple[str, dict]:
    """MiniMax H3 у KIE — три отдельные модели: выбираем по входным данным.

    - видео/фото-референсы    → reference-to-video (reference_*_urls; кадры идут как фото)
    - первый/последний кадр   → image-to-video (без aspect_ratio)
    - только текст            → text-to-video (aspect_ratio обязателен)
    Разрешение у KIE — "768P" / "2K" (заглавная P). Поля audio у MiniMax нет.
    """
    res = (resolution or "2K").upper()
    frames = [u for u in (first_frame_url, last_frame_url) if u]
    images = (frames + list(style_reference_urls or []))[:9]
    videos = list(video_reference_urls or [])[:3]
    input_data: dict = {"prompt": prompt, "duration": int(duration), "resolution": res}

    if videos or (style_reference_urls and images):
        if images:
            input_data["reference_image_urls"] = images
        if videos:
            input_data["reference_video_urls"] = videos
        if audio_reference_urls:
            input_data["reference_audio_urls"] = list(audio_reference_urls)[:3]
        input_data["aspect_ratio"] = aspect_ratio or "adaptive"
        return "minimax-h3/reference-to-video", input_data

    if frames:
        if first_frame_url:
            input_data["first_frame_url"] = first_frame_url
        if last_frame_url:
            input_data["last_frame_url"] = last_frame_url
        return "minimax-h3/image-to-video", input_data

    input_data["aspect_ratio"] = aspect_ratio or "16:9"
    return MINIMAX_H3_GEN_ALIAS, input_data


def _extract_url(body: dict) -> str | None:
    """Извлекает URL результата из callback-тела KIE."""
    import json as _json
    data = body.get("data") or body

    # Veo 3.1 формат: data.info.resultUrls (объект, не строка)
    info = data.get("info")
    if isinstance(info, dict):
        for key in ("resultUrls", "originUrls"):
            urls = info.get(key)
            if isinstance(urls, list) and urls:
                return urls[0]

    # Основной формат KIE: data.resultJson — JSON-строка {"resultUrls": ["url", ...]}
    result_json_str = data.get("resultJson")
    if result_json_str:
        try:
            result_obj = _json.loads(result_json_str)
            urls = result_obj.get("resultUrls") or result_obj.get("resultUrl")
            if isinstance(urls, list) and urls:
                return urls[0]
            if isinstance(urls, str) and urls:
                return urls
        except Exception:
            pass

    # Запасные форматы (legacy / другие модели)
    for key in ("resultList", "result_list", "results", "images", "videos"):
        lst = data.get(key)
        if isinstance(lst, list) and lst:
            url = lst[0].get("url") or lst[0].get("image_url") or lst[0].get("video_url")
            if url:
                return url
    for key in ("url", "image_url", "video_url", "output_url"):
        url = data.get(key)
        if url:
            return url
    return None


def _is_failed(body: dict) -> bool:
    data = body.get("data") or body
    # Современный формат KIE: data.state
    state = (data.get("state") or "").lower()
    if state in ("fail", "failed", "error"):
        return True
    # Легаси: data.status
    status = data.get("status") or body.get("status")
    if isinstance(status, str):
        return status.lower() in ("failed", "error", "fail")
    if isinstance(status, int):
        return status in (3, -1)
    # code 4xx/5xx от KIE = ошибка генерации
    code = body.get("code")
    if isinstance(code, int) and (400 <= code < 600):
        return True
    return False


class KieProvider(OpenAICompatProvider):
    provider_id = "kie"
    base_url = "https://api.kie.ai/v1"  # для chat через OpenAI-compat
    chat_model = "gpt-4o-mini"

    def __init__(self, api_key: str) -> None:
        super().__init__(api_key)
        # callback base URL берётся из конфига при первом вызове
        self._callback_base: str | None = None

    def _get_callback_base(self) -> str:
        if self._callback_base is None:
            from config import config
            self._callback_base = getattr(config, "kie_callback_base_url", "").rstrip("/")
        return self._callback_base

    async def _create_job(self, model: str, input_data: dict, corr_id: str) -> str:
        """Создаёт job на KIE, возвращает taskId."""
        callback_url = f"{self._get_callback_base()}/kie/callback/{corr_id}"
        payload = {
            "model": model,
            "callBackUrl": callback_url,
            "input": input_data,
        }
        async with self._session(timeout=30) as session:
            async with session.post(f"{_KIE_API_BASE}/jobs/createTask", json=payload) as resp:
                if resp.status == 402:
                    raise ProviderUnavailableError("Недостаточно средств на балансе KIE")
                if resp.status == 400:
                    body = await resp.text()
                    logger.error("KIE createTask HTTP 400: model=%s input=%s body=%s", model, input_data, body[:400])
                    if "content" in body.lower() or "policy" in body.lower():
                        raise ProviderContentPolicyError("Запрос не прошёл проверку безопасности KIE")
                    raise ProviderUnavailableError(f"Ошибка KIE: {body[:200]}")
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("KIE createTask HTTP %s: model=%s input=%s body=%s", resp.status, model, input_data, body[:400])
                    raise ProviderUnavailableError(f"KIE ответил HTTP {resp.status}")
                data = await resp.json()

        if data.get("code") != 200:
            logger.error(
                "KIE createTask failed: code=%s msg=%s model=%s input=%s",
                data.get("code"), data.get("msg"), model, input_data,
            )
            raise ProviderUnavailableError(f"KIE: {data.get('msg', 'неизвестная ошибка')}")
        task_id = data["data"]["taskId"]
        logger.info("KIE task created: model=%s taskId=%s", model, task_id)
        ctx = _pending_job_ctx.get()
        if ctx:
            try:
                from db.queries import save_pending_job
                await save_pending_job(corr_id, task_id=task_id, **ctx)
            except Exception as _e:
                logger.warning("KIE: не удалось сохранить контекст job %s: %s", corr_id, _e)
        return task_id

    @staticmethod
    def _to_responses_content(content: str | list) -> str | list:
        """Конвертирует OpenAI chat/completions формат контента в KIE Responses API формат."""
        if isinstance(content, str):
            return content
        result = []
        for item in content:
            if item.get("type") == "text":
                result.append({"type": "input_text", "text": item["text"]})
            elif item.get("type") == "image_url":
                url = item["image_url"]["url"] if isinstance(item.get("image_url"), dict) else item.get("image_url", "")
                result.append({"type": "input_image", "image_url": url})
        return result

    async def chat(self, messages: list[dict], system: str = "", model: str | None = None) -> ChatResult:
        """GPT-5.5 у KIE — Responses API (/codex/v1/responses), а не chat/completions."""
        actual_model = model or "gpt-5-5"
        payload_input: list[dict] = []
        if system:
            payload_input.append({"role": "system", "content": system})
        # KIE Responses API использует input_text/input_image вместо text/image_url
        converted = [
            {**m, "content": self._to_responses_content(m["content"])} for m in messages
        ]
        payload_input.extend(converted)
        async with self._session(timeout=180) as session:
            async with session.post(
                "https://api.kie.ai/codex/v1/responses",
                json={"model": actual_model, "input": payload_input, "stream": False},
            ) as resp:
                data = await self._handle_response(resp)
        # Ошибки KIE приходят телом {"code": 4xx, "msg": ...} даже при HTTP 200
        if isinstance(data.get("code"), int) and data["code"] >= 400:
            logger.error("KIE chat failed: model=%s code=%s msg=%s", actual_model, data["code"], data.get("msg"))
            raise ProviderUnavailableError(f"KIE: {data.get('msg', 'ошибка')}")
        parts: list[str] = []
        for item in data.get("output") or []:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text" and c.get("text"):
                        parts.append(c["text"])
        text = "".join(parts).strip()
        if not text:
            logger.error("KIE chat: пустой ответ, model=%s keys=%s", actual_model, list(data.keys()))
            raise ProviderUnavailableError("KIE: пустой ответ модели")
        return ChatResult(text=text)

    async def create_omni_character(self, name: str, description: str, image_urls: list[str]) -> str:
        """Gemini Omni: создаёт персонажа у KIE и возвращает characterId (бесплатно, синхронно).
        image_urls: [портрет] или [портрет, фото в полный рост]."""
        async with self._session(timeout=90) as session:
            async with session.post(
                "https://api.kie.ai/api/v1/omni/character/create",
                json={"descriptions": description, "image_urls": image_urls[:2], "character_name": name},
            ) as resp:
                data = await resp.json(content_type=None)
        if data.get("code") != 200 or not (data.get("data") or {}).get("characterId"):
            logger.error("KIE omni character create failed: code=%s msg=%s", data.get("code"), data.get("msg"))
            raise ProviderUnavailableError(f"KIE: {data.get('msg', 'не удалось создать персонажа')}")
        return data["data"]["characterId"]

    async def fetch_task(self, task_id: str) -> dict | None:
        """Статус/результат задачи KIE (jobs API) — тело совместимо с callback'ом. None при ошибке."""
        try:
            async with self._session(timeout=30) as session:
                async with session.get(f"{_KIE_API_BASE}/jobs/recordInfo", params={"taskId": task_id}) as resp:
                    if resp.status >= 400:
                        return None
                    return await resp.json()
        except Exception as e:
            logger.warning("KIE recordInfo %s: %s", task_id, e)
            return None

    async def _await_job(self, corr_id: str) -> dict:
        """Ждёт callback от KIE с таймаутом."""
        fut = register_pending(corr_id)
        try:
            result = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания результата")
        finally:
            unregister_pending(corr_id)
        return result

    async def _create_veo_job(self, model: str, input_data: dict, corr_id: str) -> str:
        """Создаёт Veo-задачу через /veo/generate (отдельный endpoint KIE).
        Этот endpoint принимает поля на верхнем уровне, не вложенными в 'input'."""
        callback_url = f"{self._get_callback_base()}/kie/callback/{corr_id}"
        # /veo/generate — поля на корневом уровне, не внутри "input"
        payload = {"model": model, "callBackUrl": callback_url, **input_data}
        async with self._session(timeout=30) as session:
            async with session.post(f"{_KIE_API_BASE}/veo/generate", json=payload) as resp:
                if resp.status == 402:
                    raise ProviderUnavailableError("Недостаточно средств на балансе KIE")
                if resp.status == 400:
                    body = await resp.text()
                    raise ProviderUnavailableError(f"KIE Veo ошибка запроса: {body[:200]}")
                if resp.status >= 400:
                    raise ProviderUnavailableError(f"KIE Veo ответил HTTP {resp.status}")
                data = await resp.json()
        if data.get("code") != 200:
            logger.error("KIE Veo create failed: code=%s msg=%s model=%s", data.get("code"), data.get("msg"), model)
            raise ProviderUnavailableError(f"KIE Veo: {data.get('msg', 'неизвестная ошибка')}")
        task_id = data["data"]["taskId"]
        logger.info("KIE Veo task created: model=%s taskId=%s", model, task_id)
        ctx = _pending_job_ctx.get()
        if ctx:
            try:
                from db.queries import save_pending_job
                await save_pending_job(corr_id, **ctx)
            except Exception as _e:
                logger.warning("KIE: не удалось сохранить контекст Veo job %s: %s", corr_id, _e)
        return task_id

    # ─── Генерация аудио ──────────────────────────────────────────────────────

    async def generate_audio(
        self, prompt: str, audio_type: str = "voice", model: str | None = None,
        music_params: dict | None = None,
    ) -> GenerationResult:
        actual_model = model or "elevenlabs/text-to-dialogue-v3"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        # elevenlabs/text-to-dialogue-v3 требует массив dialogue с voice ID
        voice_id = "EkK5I93UQWFDigLMpZcX"
        stability = 0.5
        language_code = None
        dialogue_mode = False
        voice_id_2 = voice_id
        if music_params and music_params.get("_provider_model") == "elevenlabs-v3":
            voice_id = music_params.get("voice_id") or voice_id
            stability = music_params.get("voice_stability", 0.5)
            raw_lang = music_params.get("voice_language", "auto")
            language_code = None if raw_lang == "auto" else raw_lang
            dialogue_mode = bool(music_params.get("voice_dialogue_mode"))
            voice_id_2 = music_params.get("voice_id_2") or voice_id

        if dialogue_mode:
            # Каждая непустая строка — отдельная реплика, голоса чередуются
            raw_lines = [l.strip() for l in prompt.splitlines() if l.strip()]
            dialogue = [
                {"text": line, "voice": voice_id if i % 2 == 0 else voice_id_2}
                for i, line in enumerate(raw_lines)
            ] or [{"text": prompt, "voice": voice_id}]
        else:
            dialogue = [{"text": prompt, "voice": voice_id}]

        input_data: dict = {"dialogue": dialogue, "stability": stability}
        if language_code:
            input_data["language_code"] = language_code

        try:
            await self._create_job(actual_model, input_data, corr_id)
            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT_AUDIO)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания аудио")
        finally:
            unregister_pending(corr_id)

        if _is_failed(callback_body):
            _ad = callback_body.get("data") or callback_body
            logger.error("KIE audio failed: model=%s code=%s msg=%s failMsg=%s",
                         actual_model, callback_body.get("code"), callback_body.get("msg"), _ad.get("failMsg"))
            raise ProviderUnavailableError("KIE: генерация аудио завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            raise ProviderUnavailableError("KIE: не получен URL аудио")

        audio_bytes = await self._download(url)
        return GenerationResult(data=audio_bytes, mime_type="audio/mpeg", filename="audio.mp3")

    async def _download(self, url: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status >= 400:
                    raise ProviderUnavailableError("Не удалось скачать результат от KIE")
                return await resp.read()

    # ─── Генерация изображений ────────────────────────────────────────────────

    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None, style_reference_urls: list[str] | None = None,
        quality: str | None = None,
    ) -> GenerationResult:
        actual_model = model or "nano-banana-2"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)  # регистрируем ДО создания job (без гонки)

        try:
            task_id = await self._create_job(
                actual_model,
                _image_input(actual_model, prompt, aspect_ratio, resolution, style_reference_urls),
                corr_id,
            )

            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания изображения")
        finally:
            unregister_pending(corr_id)

        data = callback_body.get("data", {})
        logger.info("KIE image callback: model=%s state=%s code=%s resultJson=%s",
                    actual_model, data.get("state"), callback_body.get("code"),
                    str(data.get("resultJson", ""))[:200])

        if _is_failed(callback_body):
            logger.error("KIE image failed: model=%s failMsg=%s", actual_model, data.get("failMsg"))
            raise ProviderUnavailableError("KIE: генерация завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            logger.error("KIE image: URL not found in callback. data keys=%s", list(data.keys()))
            raise ProviderUnavailableError("KIE: не получен URL изображения")

        image_bytes = await self._download(url)
        # Grok не принимает TG-URL при редактировании — сохраняем оригинальный CDN URL
        gen_image_url = url if actual_model.startswith("grok-imagine") else None
        return GenerationResult(data=image_bytes, mime_type="image/png", filename="image.png",
                                provider_image_url=gen_image_url)

    # ─── Генерация видео ──────────────────────────────────────────────────────

    async def generate_video(
        self, prompt: str, duration: int = 5, model: str | None = None,
        first_frame_url: str | None = None,
        last_frame_url: str | None = None,
        style_reference_urls: list[str] | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        audio_reference_urls: list[str] | None = None,
        video_reference_urls: list[str] | None = None,
        output_format: str | None = None,
        audio: bool = True,
        character_orientation: str | None = None,
        characters: list[dict] | None = None,
    ) -> GenerationResult:
        actual_model = model or "kling-3.0/video"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)

        is_veo = actual_model.startswith("veo")
        is_kling = actual_model.startswith("kling")
        is_bytedance = actual_model.startswith("bytedance/")
        is_wan = actual_model.startswith("wan/")
        is_wan_prime = actual_model == "wan/3-0-video-prime"
        is_pixverse = actual_model.startswith("pixverse")
        is_google = actual_model.startswith("google/")
        is_grok_video = actual_model.startswith("grok-imagine-video")
        is_minimax = actual_model.startswith("minimax")

        # Bytedance (Seedance) требует "adaptive" когда задан первый/последний кадр;
        # Wan в image/reference-режиме не принимает aspect_ratio и resolution,
        # но video-to-video режим (video_reference_urls) принимает — не подавляем.
        # Minimax не принимает aspect_ratio в image-to-video режиме (первый/последний кадр).
        _has_frame = bool(first_frame_url or last_frame_url)
        _wan_video_edit = is_wan and bool(video_reference_urls)
        _wan_with_images = is_wan and not _wan_video_edit and (_has_frame or bool(style_reference_urls))
        _pixverse_video_edit = is_pixverse and bool(video_reference_urls)
        _minimax_with_frames = is_minimax and _has_frame
        _effective_ratio = (
            "adaptive"
            if is_bytedance and _has_frame
            else (aspect_ratio or "16:9")
        )
        input_data: dict = {
            "prompt": prompt,
            "duration": str(duration) if (is_kling or is_google) else duration,
        }
        if not _wan_with_images and not _pixverse_video_edit and not _minimax_with_frames:
            input_data["aspect_ratio"] = _effective_ratio
        # wan/3-0-video-prime не принимает поле resolution совсем (ни text, ни video-edit)
        # pixverse всегда использует поле quality (не resolution), во всех режимах
        if (is_kling or is_wan or is_bytedance or is_google or is_grok_video or is_pixverse or is_minimax) and not _wan_with_images and not is_wan_prime:
            if is_pixverse:
                input_data["quality"] = resolution or "720p"
            else:
                input_data["resolution"] = resolution or "720p"

        # ── Первый / последний кадр ──────────────────────────────────────────
        if is_bytedance:
            if first_frame_url:
                input_data["first_frame_url"] = first_frame_url
            if last_frame_url:
                input_data["last_frame_url"] = last_frame_url
        elif is_kling:
            if first_frame_url or last_frame_url:
                input_data["image_urls"] = [u for u in [first_frame_url, last_frame_url] if u]
        elif is_wan or is_pixverse or is_google or is_minimax:
            if first_frame_url:
                input_data["first_frame_url"] = first_frame_url
            if last_frame_url:
                input_data["last_frame_url"] = last_frame_url
        elif is_grok_video:
            # Grok uses image_urls for all images; first_frame occupies slot 0
            if first_frame_url:
                input_data["image_urls"] = [first_frame_url]

        # ── Фото-референсы (extra style refs) ───────────────────────────────
        if style_reference_urls:
            if is_bytedance:
                input_data["style_reference_urls"] = list(style_reference_urls)
            elif is_grok_video:
                # Append after first_frame (if any) — total cap 7 images
                existing = input_data.get("image_urls", [])
                input_data["image_urls"] = existing + list(style_reference_urls)
            elif is_wan_prime:
                # wan/3-0-video-prime не поддерживает image_urls — используем frame urls
                refs = list(style_reference_urls)
                if not input_data.get("first_frame_url"):
                    input_data["first_frame_url"] = refs[0]
                if len(refs) > 1 and not input_data.get("last_frame_url"):
                    input_data["last_frame_url"] = refs[1]
            else:
                # minimax-h3, wan, pixverse, google и прочие
                input_data["image_urls"] = list(style_reference_urls)

        # ── Формат вывода ────────────────────────────────────────────────────
        if output_format and is_bytedance:
            input_data["output_format"] = output_format

        # ── Ориентация персонажа (Kling Motion Control) ──────────────────────
        if character_orientation and "motion-control" in actual_model:
            input_data["character_orientation"] = character_orientation

        # ── Режим Kling (только для новых моделей вида kling-X.X/...) ────────
        # Старые модели kling/v*-{mode}-* кодируют режим в названии.
        # Новые (kling-3.0/video, kling-3.0-omni/video и др.) требуют явного mode.
        if is_kling and "-" in actual_model.split("/")[0] and "." in actual_model and "motion-control" not in actual_model:
            input_data.setdefault("mode", "std")

        # ── Флаг аудио ───────────────────────────────────────────────────────
        # WAN video-to-video использует audio_setting: "auto"/"origin", а не audio: bool
        # Pixverse video edit использует generate_audio_switch
        # Wan 3.0 (в т.ч. Prime) не принимает audio_setting (KIE 422 "unsupported field") — у него обычное audio: bool
        if _wan_video_edit and not actual_model.startswith("wan/3-"):
            input_data["audio_setting"] = "origin" if not audio else "auto"
        elif _pixverse_video_edit:
            input_data["generate_audio_switch"] = audio
        else:
            input_data["audio"] = audio

        # ── Аудио-референсы ─────────────────────────────────────────────────
        if audio_reference_urls:
            if is_google:
                # Gemini ожидает поле audio_ids
                input_data["audio_ids"] = list(audio_reference_urls)
            else:
                input_data["audio_urls"] = list(audio_reference_urls)

        # ── Персонажи Gemini Omni (id, созданные через omni/character/create) ─
        if characters and is_google:
            input_data["character_ids"] = [c["character_id"] for c in characters if c.get("character_id")]

        # ── Видео-референсы ──────────────────────────────────────────────────
        if video_reference_urls:
            if is_google:
                # Gemini ожидает video_list с объектами {url, start, ends}
                input_data["video_list"] = [
                    {"url": url, "start": 0, "ends": duration}
                    for url in video_reference_urls
                ]
            else:
                input_data["video_urls"] = list(video_reference_urls)

        if actual_model == KLING_OMNI_GEN_ALIAS:
            # Omni собирается отдельно: три модели KIE с разными наборами полей
            actual_model, input_data = _kling_omni_request(
                prompt, duration, aspect_ratio, resolution, audio,
                first_frame_url, last_frame_url, style_reference_urls, video_reference_urls,
                characters, audio_reference_urls,
            )

        if actual_model == HAPPYHORSE_EDIT and video_reference_urls:
            input_data = _happyhorse_edit_request(
                prompt, resolution, audio, video_reference_urls[0], style_reference_urls,
            )

        if actual_model == KLING_OMNI_TRANSFORM and video_reference_urls:
            input_data = _kling_omni_transformation_request(
                prompt, duration, aspect_ratio, resolution, audio, style_reference_urls, video_reference_urls[0],
            )

        if is_pixverse and characters:
            # PixVerse Fusion: именованные фото-референсы (Subject / Background), в промпте — @имя
            actual_model = "pixverse-v6/reference-to-video"
            input_data = {
                "prompt": prompt,
                "image_references": list(characters)[:7],
                "aspect_ratio": aspect_ratio or "16:9",
                "quality": resolution or "720p",
                "duration": int(duration),
                "generate_audio_switch": bool(audio),
            }

        if is_pixverse and not characters and (first_frame_url or last_frame_url) and not video_reference_urls:
            # text-to-video игнорирует кадры: для кадров у PixVerse отдельные модели KIE
            _pv_common = {
                "prompt": prompt,
                "quality": resolution or "720p",
                "duration": int(duration),
                "generate_audio_switch": bool(audio),
            }
            if first_frame_url and last_frame_url:
                actual_model = "pixverse-v6/transition"
                input_data = {
                    **_pv_common,
                    "first_frame_image_url": first_frame_url,
                    "last_frame_image_url": last_frame_url,
                }
            elif first_frame_url:
                actual_model = "pixverse-v6/image-to-video"
                input_data = {**_pv_common, "image_urls": [first_frame_url]}
            else:
                raise ProviderUnavailableError("PixVerse: конец видео можно задать только вместе с началом")

        if actual_model == KLING3_ALIAS:
            input_data = _kling3_request(
                prompt, duration, aspect_ratio, resolution, audio, first_frame_url, last_frame_url,
                characters,
            )

        if actual_model == KLING3_MOTION_ALIAS:
            # В Motion Control фото лежит в слоте «первый кадр», видео движения — в слоте «последний кадр»
            input_data = _kling3_motion_request(
                prompt, resolution, first_frame_url, last_frame_url, character_orientation,
            )

        if actual_model.startswith(SEEDANCE2_PREFIX):
            input_data = _seedance2_request(
                actual_model, prompt, duration, aspect_ratio, resolution, audio, output_format,
                first_frame_url, last_frame_url, style_reference_urls, video_reference_urls,
                audio_reference_urls,
            )

        if actual_model == WAN27_EDIT_ALIAS and video_reference_urls:
            # Wan 2.7 Video Edit — отдельная модель KIE с другими именами полей
            actual_model, input_data = _wan27_videoedit_request(
                prompt, duration, aspect_ratio, resolution, audio,
                video_reference_urls[0], style_reference_urls,
            )

        if actual_model == MINIMAX_H3_GEN_ALIAS:
            # MiniMax H3 — тоже три модели KIE с разными полями
            actual_model, input_data = _minimax_h3_request(
                prompt, duration, aspect_ratio, resolution, first_frame_url, last_frame_url,
                style_reference_urls, video_reference_urls, audio_reference_urls,
            )

        try:
            if is_veo:
                # KIE Veo: resolution передаётся строчной (4k, не 4K)
                _veo_res = (resolution or "1080p").replace("K", "k")
                veo_input: dict = {
                    "prompt": prompt,
                    "duration": duration,
                    "resolution": _veo_res,
                    # Veo принимает только 16:9 и 9:16; у модели нет выбора масштаба, и в aspect_ratio
                    # может остаться значение от другой модели (1:1, 4:3…) → KIE 422 "Ratio error"
                    "aspect_ratio": aspect_ratio if aspect_ratio in ("16:9", "9:16") else "16:9",
                }
                if video_reference_urls:
                    veo_input["generationType"] = "REFERENCE_2_VIDEO"
                    veo_input["videoUrls"] = list(video_reference_urls)
                elif first_frame_url:
                    veo_input["generationType"] = "FIRST_AND_LAST_FRAMES_2_VIDEO"
                    veo_input["imageUrls"] = [first_frame_url]
                elif style_reference_urls:
                    if actual_model == "veo3":
                        # Quality не поддерживает REFERENCE_2_VIDEO
                        veo_input["generationType"] = "FIRST_AND_LAST_FRAMES_2_VIDEO"
                    else:
                        veo_input["generationType"] = "REFERENCE_2_VIDEO"
                    veo_input["imageUrls"] = list(style_reference_urls)
                else:
                    veo_input["generationType"] = "TEXT_2_VIDEO"
                veo_input["audio"] = audio
                await self._create_veo_job(actual_model, veo_input, corr_id)
            else:
                await self._create_job(actual_model, input_data, corr_id)

            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания видео")
        finally:
            unregister_pending(corr_id)

        if _is_failed(callback_body):
            _data = callback_body.get("data", {})
            _fail_msg = (_data.get("failMsg") or _data.get("fail_msg") or "").lower()
            logger.error("KIE video failed: model=%s failMsg=%s", actual_model, _fail_msg)
            if any(k in _fail_msg for k in ("validation", "policy", "safety", "content")):
                raise ProviderContentPolicyError("Изображение не прошло проверку безопасности — попробуйте другое фото")
            if "character" in _fail_msg and ("not" in _fail_msg or "no valid" in _fail_msg or "detect" in _fail_msg):
                raise ProviderContentPolicyError("Персонаж не распознан. Пришлите новое видео")
            raise ProviderUnavailableError("KIE: генерация видео завершилась с ошибкой")

        url = _extract_url(callback_body)
        if not url:
            logger.error("KIE video: URL not found. data keys=%s", list(callback_body.get("data", {}).keys()))
            raise ProviderUnavailableError("KIE: не получен URL видео")

        video_bytes = await self._download(url)
        if output_format == "mov":
            return GenerationResult(data=video_bytes, mime_type="video/quicktime", filename="video.mov")
        return GenerationResult(data=video_bytes, mime_type="video/mp4", filename="video.mp4")

    # ─── Редактирование изображений ───────────────────────────────────────────

    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_urls: list[str] | None = None,
        provider_task_id: str | None = None, resolution: str | None = None,
        quality: str | None = None, aspect_ratio: str | None = None,
    ) -> GenerationResult:
        """Job-based редактирование через KIE createTask."""
        actual_model = model or "google/nano-banana-edit"
        corr_id = uuid.uuid4().hex
        fut = register_pending(corr_id)
        _srefs = list(style_reference_urls) if style_reference_urls else []

        if actual_model.startswith("grok-imagine"):
            # Grok не принимает TG-URL — нужен оригинальный CDN URL от KIE.
            # provider_task_id здесь — это сохранённый CDN URL (provider_image_url из генерации).
            src_url = provider_task_id or image_url
            if not src_url:
                raise ProviderUnavailableError(
                    "Grok Image: для редактирования нужно сначала сгенерировать фото этой же моделью"
                )
            input_data: dict = {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio or "1:1",
                "image_urls": [src_url] + _srefs,
            }
        elif actual_model.startswith("flux-2/"):
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "input_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "auto",
                "resolution": resolution or "1K",
            }
        elif actual_model.startswith("seedream/") and "image-to-image" in actual_model:
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "1:1",
                "quality": _SEEDREAM_QUALITY.get(resolution or "2K", "basic"),
                "output_format": "png",
            }
        elif actual_model == "nano-banana-2-lite":
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "auto",
            }
        elif actual_model == "nano-banana-pro":
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            # nano-banana-pro использует image_input (массив URL), а не image_urls
            input_data = {
                "prompt": prompt,
                "image_input": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "1:1",
                "resolution": resolution or "1K",
                "output_format": "png",
            }
        elif actual_model in ("gpt-image-2-image-to-image", "gpt-image-2-5-flare-image-to-image", "gpt-image-2-5-sunburst-image-to-image"):
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            input_data = {
                "prompt": prompt,
                "input_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio or "auto",
                "resolution": resolution or "1K",
            }
        else:
            if not image_url:
                raise ProviderUnavailableError("KIE edit_image: не передан URL изображения")
            # google/nano-banana-edit и прочие — поддерживает несколько ориентиров
            input_data = {
                "prompt": prompt,
                "image_urls": [image_url] + _srefs,
                "aspect_ratio": aspect_ratio,
                "output_format": "png",
            }
            if not aspect_ratio:
                input_data.pop("aspect_ratio")

        try:
            await self._create_job(actual_model, input_data, corr_id)
            callback_body = await asyncio.wait_for(fut, timeout=_JOB_TIMEOUT)
        except asyncio.TimeoutError:
            raise ProviderUnavailableError("KIE: истекло время ожидания редактирования")
        finally:
            unregister_pending(corr_id)

        data = callback_body.get("data", {})
        logger.info("KIE edit callback: model=%s state=%s code=%s resultJson=%s",
                    actual_model, data.get("state"), callback_body.get("code"),
                    str(data.get("resultJson", ""))[:200])

        if _is_failed(callback_body):
            logger.error("KIE edit failed: model=%s failMsg=%s", actual_model, data.get("failMsg"))
            raise ProviderUnavailableError("KIE: редактирование завершилось с ошибкой")

        result_url = _extract_url(callback_body)
        if not result_url:
            logger.error("KIE edit: URL not found in callback. data keys=%s", list(data.keys()))
            raise ProviderUnavailableError("KIE: не получен URL результата редактирования")

        image_out = await self._download(result_url)
        return GenerationResult(data=image_out, mime_type="image/png", filename="edited.png")

    # ─── Редактирование видео ─────────────────────────────────────────────────

    async def edit_video(
        self, video_bytes: bytes, prompt: str, model: str | None = None,
        video_url: str | None = None,
        duration: int = 5,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        audio: bool = True,
        audio_url: str | None = None,
        style_reference_urls: list[str] | None = None,
    ) -> GenerationResult:
        if not video_url:
            raise ProviderUnavailableError("KIE edit_video: не передан URL видео")
        return await self.generate_video(
            prompt=prompt,
            duration=duration,
            model=model,
            video_reference_urls=[video_url],
            aspect_ratio=aspect_ratio or "16:9",
            resolution=resolution,
            audio=audio,
            audio_reference_urls=[audio_url] if audio_url else None,
            style_reference_urls=style_reference_urls,
        )
