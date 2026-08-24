# Base URL уточнить по документации bratuha.ru
from providers.openai_compat import OpenAICompatProvider


class BratuhaProvider(OpenAICompatProvider):
    provider_id = "bratuha"
    base_url = "https://api.bratuha.ru/v1"
    image_model = "flux-1-schnell"
    chat_model = "gpt-4o-mini"
