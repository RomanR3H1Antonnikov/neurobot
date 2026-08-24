from providers.openai_compat import OpenAICompatProvider


class GenApiProvider(OpenAICompatProvider):
    provider_id = "genapi"
    base_url = "https://api.gen-api.ru/v1"
    image_model = "flux-1-schnell"
    video_model = "wan2.1-t2v-14b"
    audio_model = "tts-1"
    image_edit_model = "gpt-image-1"
    chat_model = "gpt-4o-mini"
