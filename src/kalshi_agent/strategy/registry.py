from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kalshi_agent.strategy.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, **params: Any) -> Strategy:
    _load_builtins()
    try:
        return _REGISTRY[name](**params)
    except KeyError as exc:
        raise KeyError(f"unknown strategy {name!r}; available: {sorted(_REGISTRY)}") from exc


def list_strategies() -> dict[str, type[Strategy]]:
    _load_builtins()
    return dict(_REGISTRY)


_loaded = False


def _load_builtins() -> None:
    global _loaded
    if not _loaded:
        from kalshi_agent.strategy import builtin  # noqa: F401

        _loaded = True


StrategyFactory = Callable[..., Strategy]
