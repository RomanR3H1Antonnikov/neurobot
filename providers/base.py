from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class TaskType(str, Enum):
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"
    AUDIO_GENERATION = "audio_generation"
    IMAGE_EDIT = "image_edit"
    VIDEO_EDIT = "video_edit"
    CHAT = "chat"
    DOCUMENT = "document"


class ProviderError(Exception):
    """Базовый класс ошибок провайдера."""


class ProviderUnavailableError(ProviderError):
    """Сервис агрегатора недоступен или у него закончились средства."""


class ProviderContentPolicyError(ProviderError):
    """Контент не прошёл проверку политики безопасности агрегатора."""


@dataclass
class GenerationResult:
    data: bytes          # бинарные данные (изображение, видео, аудио)
    mime_type: str       # например 'image/png', 'video/mp4', 'audio/mpeg'
    filename: str
    variants: list[bytes] = None  # доп. варианты (например, 4 картинки Midjourney)


@dataclass
class ChatResult:
    text: str


class AbstractProvider(ABC):
    """Единый интерфейс для всех агрегаторов."""

    provider_id: str  # переопределяется в каждом адаптере

    @abstractmethod
    async def generate_image(
        self, prompt: str, aspect_ratio: str = "1:1", resolution: str = "1K",
        model: str | None = None, style_reference_url: str | None = None,
    ) -> GenerationResult:
        ...

    @abstractmethod
    async def generate_video(self, prompt: str, duration: int = 5) -> GenerationResult:
        ...

    @abstractmethod
    async def generate_audio(self, prompt: str, audio_type: str = "voice") -> GenerationResult:
        ...

    @abstractmethod
    async def edit_image(
        self, image_bytes: bytes, prompt: str, model: str | None = None,
        image_url: str | None = None, style_reference_url: str | None = None,
    ) -> GenerationResult:
        ...

    @abstractmethod
    async def edit_video(self, video_bytes: bytes, prompt: str) -> GenerationResult:
        ...

    @abstractmethod
    async def chat(self, messages: list[dict], system: str = "") -> ChatResult:
        ...
