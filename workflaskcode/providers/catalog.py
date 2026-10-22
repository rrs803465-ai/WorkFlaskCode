from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from workflaskcode.providers.base import Provider
from workflaskcode.providers.types import ModelInfo
from workflaskcode.utils.fs import atomic_write_text

DEFAULT_TTL_SECONDS = 6 * 3600


class ModelCatalog:
    def __init__(self, cache_dir: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.time) -> None:
        self.cache_dir = cache_dir
        self.ttl = ttl_seconds
        self._clock = clock

    def _path(self, provider_name: str) -> Path:
        return self.cache_dir / f"models-{re.sub(r'[^a-z0-9_-]', '_', provider_name)}.json"

    def cached(self, provider_name: str) -> tuple[list[ModelInfo], float] | None:
        path = self._path(provider_name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [ModelInfo.from_dict(m) for m in data["models"]], float(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def get(self, provider: Provider, *, refresh: bool = False) -> tuple[list[ModelInfo], str]:
        if not refresh:
            hit = self.cached(provider.name)
            if hit and self._clock() - hit[1] < self.ttl:
                provider.remember_models(hit[0])
                return hit[0], f"cache ({int(self._clock() - hit[1])}s old)"
        models = provider.list_models()
        self.save(provider.name, models)
        return models, "live"

    def save(self, provider_name: str, models: list[ModelInfo]) -> None:
        payload = {"fetched_at": self._clock(), "models": [m.to_dict() for m in models]}
        atomic_write_text(self._path(provider_name), json.dumps(payload))

    def clear(self, provider_name: str | None = None) -> None:
        paths = [self._path(provider_name)] if provider_name else self.cache_dir.glob("models-*.json")
        for p in paths:
            p.unlink(missing_ok=True)


def search_models(models: list[ModelInfo], query: str) -> list[ModelInfo]:
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return list(models)
    out = []
    for m in models:
        hay = f"{m.id} {m.name} {m.description or ''}".lower()
        if all(t in hay for t in terms):
            out.append(m)
    return out


def filter_models(
    models: list[ModelInfo],
    *,
    free: bool | None = None,
    tools: bool | None = None,
    vision: bool | None = None,
    min_context: int | None = None,
) -> list[ModelInfo]:
    out = []
    for m in models:
        if free is not None and m.is_free is not free:
            continue
        if tools is not None and m.supports_tools is not tools:
            continue
        if vision is not None and m.supports_vision is not vision:
            continue
        if min_context is not None and (m.context_length or 0) < min_context:
            continue
        out.append(m)
    return out


SORT_KEYS: dict[str, Callable[[ModelInfo], tuple]] = {
    "name": lambda m: (m.name.lower(),),
    "id": lambda m: (m.id.lower(),),
    "context": lambda m: (-(m.context_length or 0), m.id),
    "price": lambda m: (m.prompt_price is None, m.prompt_price or 0.0, m.id),
    "newest": lambda m: (m.created is None, _neg_str(m.created or ""), m.id),
}


def _neg_str(value: str) -> tuple[int, ...]:
    return tuple(-ord(c) for c in value)


def sort_models(models: list[ModelInfo], key: str = "name") -> list[ModelInfo]:
    if key not in SORT_KEYS:
        raise ValueError(f"unknown sort key '{key}' (choose from {', '.join(SORT_KEYS)})")
    return sorted(models, key=SORT_KEYS[key])


def find_model(models: list[ModelInfo], ref: str) -> ModelInfo | None:
    for m in models:
        if m.id == ref:
            return m
    low = ref.lower()
    exact = [m for m in models if m.id.lower() == low or m.name.lower() == low]
    if len(exact) == 1:
        return exact[0]
    return None
