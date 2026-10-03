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
    chain = get_provider_chain_for_model(task_type, model_slug)
    return chain[0]


def get_provider_chain_for_model(task_type: TaskType, model_slug: str) -> list[tuple[AbstractProvider, dict]]:
    """
    Возвращает упорядоченный список (провайдер, конфиг) для попытки: primary первым,
    затем fallbacks из конфига (поле `fallbacks: [{provider, model_id}]`).
    """
    task_cfg = config.models.get(task_type.value, {})
    base_cfg: dict | None = None
    for m in task_cfg.get("models", []):
        if m.get("id") == model_slug:
            if not m.get("enabled"):
                raise ProviderUnavailableError(f"Модель '{model_slug}' временно отключена")
            if m.get("provider") not in _registry:
                raise ProviderUnavailableError(f"Провайдер '{m.get('provider')}' не настроен")
            base_cfg = m
            break
    if base_cfg is None:
        raise ProviderUnavailableError(f"Модель '{model_slug}' не найдена в конфигурации")

    chain: list[tuple[AbstractProvider, dict]] = [(_registry[base_cfg["provider"]], base_cfg)]
    for fb in base_cfg.get("fallbacks", []):
        fb_prov = fb.get("provider")
        fb_model_id = fb.get("model_id")
        if fb_prov and fb_model_id and fb_prov in _registry:
            # Eff-конфиг = исходный конфиг с подменёнными provider/model_id
            eff = {**base_cfg, "provider": fb_prov, "model_id": fb_model_id}
            chain.append((_registry[fb_prov], eff))
    return chain


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
