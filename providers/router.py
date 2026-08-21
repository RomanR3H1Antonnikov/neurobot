"""
Маршрутизатор провайдеров: выбирает самый дешёвый enabled-провайдер для задачи.
"""
from config import config
from providers.base import AbstractProvider, TaskType, ProviderUnavailableError
from providers.aitunnel import AitunnelProvider
from providers.routerai import RouteraiProvider
from providers.kie import KieProvider


def _build_registry() -> dict[str, AbstractProvider]:
    """Инициализирует все адаптеры с ключами из .env."""
    registry: dict[str, AbstractProvider] = {}
    if config.aitunnel_api_key:
        registry["aitunnel"] = AitunnelProvider(config.aitunnel_api_key)
    if config.routerai_api_key:
        registry["routerai"] = RouteraiProvider(config.routerai_api_key)
    if config.kie_api_key:
        registry["kie"] = KieProvider(config.kie_api_key)
    return registry


_registry: dict[str, AbstractProvider] = _build_registry()


def get_provider(task_type: TaskType) -> tuple[AbstractProvider, dict]:
    """
    Возвращает (провайдер, конфиг_модели) с минимальным cost_usd для задачи.
    Raises ProviderUnavailableError если нет ни одного доступного провайдера.
    """
    task_cfg = config.models.get(task_type.value)
    if not task_cfg:
        raise ProviderUnavailableError(f"Задача {task_type.value} не настроена в models_config.yaml")

    candidates = [
        m for m in task_cfg["models"]
        if m.get("enabled") and m["provider"] in _registry
    ]
    if not candidates:
        raise ProviderUnavailableError("Нет доступных провайдеров для этой задачи")

    best = min(candidates, key=lambda m: m["cost_usd"])
    return _registry[best["provider"]], best


def get_task_cost(task_type: TaskType) -> int:
    """Возвращает стоимость задачи в кредитах."""
    task_cfg = config.models.get(task_type.value, {})
    return task_cfg.get("cost_credits", 0)


def get_task_rate_limit(task_type: TaskType) -> int:
    """Возвращает лимит запросов в час для задачи."""
    task_cfg = config.models.get(task_type.value, {})
    return task_cfg.get("rate_limit_per_hour", 100)
