import os
import yaml
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str
    admin_ids: list[int]
    db_path: str

    aitunnel_api_key: str
    routerai_api_key: str
    kie_api_key: str
    genapi_api_key: str
    polzaai_api_key: str
    bratuha_api_key: str
    ranvikapi_api_key: str

    models: dict = field(default_factory=dict)


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    admin_raw = os.getenv("ADMIN_IDS", "")
    admin_ids = [int(x.strip()) for x in admin_raw.split(",") if x.strip().isdigit()]

    with open("models_config.yaml", encoding="utf-8") as f:
        models = yaml.safe_load(f)

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        db_path=os.getenv("DB_PATH", "bot.db"),
        aitunnel_api_key=os.getenv("AITUNNEL_API_KEY", ""),
        routerai_api_key=os.getenv("ROUTERAI_API_KEY", ""),
        kie_api_key=os.getenv("KIE_API_KEY", ""),
        genapi_api_key=os.getenv("GENAPI_API_KEY", ""),
        polzaai_api_key=os.getenv("POLZAAI_API_KEY", ""),
        bratuha_api_key=os.getenv("BRATUHA_API_KEY", ""),
        ranvikapi_api_key=os.getenv("RANVIKAPI_API_KEY", ""),
        models=models.get("tasks", {}),
    )


config = load_config()
