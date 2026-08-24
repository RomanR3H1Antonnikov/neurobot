from providers.openai_compat import OpenAICompatProvider
from providers.base import ProviderUnavailableError, GenerationResult


class RouteraiProvider(OpenAICompatProvider):
    provider_id = "routerai"
    base_url = "https://api.routerai.ru/v1"
    image_model = "sdxl-turbo"
    chat_model = "gpt-4o-mini"

    async def generate_video(self, prompt: str, duration: int = 5) -> GenerationResult:
        raise ProviderUnavailableError("Генерация видео через routerai не настроена")

    async def generate_audio(self, prompt: str, audio_type: str = "voice") -> GenerationResult:
        raise ProviderUnavailableError("Генерация аудио через routerai не настроена")
