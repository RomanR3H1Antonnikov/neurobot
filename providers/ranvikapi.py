# Документация: https://api.ranvik.ru/docs
from providers.openai_compat import OpenAICompatProvider


class RanvikApiProvider(OpenAICompatProvider):
    provider_id = "ranvikapi"
    base_url = "https://api.ranvik.ru/v1"
    image_model = "flux-1-schnell"
    video_model = "kling-v1"
    audio_model = "tts-1"
    image_edit_model = "gpt-image-1"
    chat_model = "gpt-4o-mini"
