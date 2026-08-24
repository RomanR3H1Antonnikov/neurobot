"""
Маршрутизатор провайдеров.
Предоставляет функции для выбора провайдера по задаче и по конкретной модели.
"""
from config import config
from providers.base import AbstractProvider, TaskType, ProviderUnavailableError
from providers.aitunnel import AitunnelProvider
from providers.routerai import RouteraiProvider
from providers.kie import KieProvider
from providers.genapi import GenApiProvider
from providers.polzaai import PolzaAiProvider
from providers.bratuha import BratuhaProvider
from providers.ranvikapi import RanvikApiProvider


def _build_registry() -> dict[str, AbstractProvider]:
    candidates = [
        ("aitunnel",  config.aitunnel_api_key,  AitunnelProvider),
        ("routerai",  config.routerai_api_key,   RouteraiProvider),
        ("kie",       config.kie_api_key,         KieProvider),
        ("genapi",    config.genapi_api_key,       GenApiProvider),
        ("polzaai",   config.polzaai_api_key,      PolzaAiProvider),
        ("bratuha",   config.bratuha_api_key,      BratuhaProvider),
        ("ranvikapi", config.ranvikapi_api_key,    RanvikApiProvider),
    ]
    return {pid: cls(key) for pid, key, cls in candidates if key}


_registry: dict[str, AbstractProvider] = _build_registry()


def get_models_for_task(task_type: TaskType) -> list[dict]:
    """Возвращает список включённых моделей для задачи (с доступными провайдерами)."""
    task_cfg = config.models.get(task_type.value, {})
    return [
        m for m in task_cfg.get("models", [])
        if m.get("enabled") and m.get("provider") in _registry
    ]


def get_provider_by_model_id(task_type: TaskType, model_slug: str) -> tuple[AbstractProvider, dict]:
    """
    Возвращает (провайдер, конфиг_модели) для конкретной модели по её slug.
    Raises ProviderUnavailableError если модель не найдена или отключена.
    """
    task_cfg = config.models.get(task_type.value, {})
    for m in task_cfg.get("models", []):
        if m.get("id") == model_slug:
            if not m.get("enabled"):
                raise ProviderUnavailableError(f"Модель '{model_slug}' временно отключена")
            if m.get("provider") not in _registry:
                raise ProviderUnavailableError(f"Провайдер '{m.get('provider')}' не настроен")
            return _registry[m["provider"]], m
    raise ProviderUnavailableError(f"Модель '{model_slug}' не найдена в конфигурации")


def get_provider(task_type: TaskType) -> tuple[AbstractProvider, dict]:
    """
    Возвращает (провайдер, конфиг_модели) с минимальным cost_usd для задачи.
    Используется для задач без пользовательского выбора (документы).
    """
    task_cfg = config.models.get(task_type.value)
    if not task_cfg:
        raise ProviderUnavailableError(f"Задача {task_type.value} не настроена")

    candidates = [
        m for m in task_cfg.get("models", [])
        if m.get("enabled") and m.get("provider") in _registry
    ]
    if not candidates:
        raise ProviderUnavailableError("Нет доступных провайдеров для этой задачи")

    best = min(candidates, key=lambda m: m.get("cost_usd", 0))
    return _registry[best["provider"]], best


def get_task_rate_limit(task_type: TaskType) -> int:
    task_cfg = config.models.get(task_type.value, {})
    return task_cfg.get("rate_limit_per_hour", 100)
