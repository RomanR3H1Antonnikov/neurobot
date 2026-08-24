# Base URL уточнить по документации polza.ai
from providers.openai_compat import OpenAICompatProvider


class PolzaAiProvider(OpenAICompatProvider):
    provider_id = "polzaai"
    base_url = "https://polza.ai/api/v1"
    image_model = "flux-1-schnell"
    chat_model = "gpt-4o-mini"
