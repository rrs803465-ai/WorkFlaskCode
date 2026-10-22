from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from workflaskcode.errors import ErrorKind, ProviderError
from workflaskcode.providers.openai_compat import OpenAICompatProvider, _int
from workflaskcode.providers.types import ModelInfo, ProviderUsageInfo, ValidationResult

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _price(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


class OpenRouterProvider(OpenAICompatProvider):
    @property
    def base_url(self) -> str:
        return (self.config.base_url or OPENROUTER_BASE).rstrip("/")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"X-Title": "WorkFlaskCode"}
        headers.update(super()._headers(extra))
        return headers

    def _parse_model(self, raw: dict[str, Any]) -> ModelInfo | None:
        mid = raw.get("id")
        if not isinstance(mid, str) or not mid:
            return None
        pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
        prompt = _price(pricing.get("prompt"))
        completion = _price(pricing.get("completion"))
        is_free: bool | None
        if prompt is not None and completion is not None:
            is_free = prompt == 0 and completion == 0
        else:
            is_free = True if mid.endswith(":free") else None
        supported = raw.get("supported_parameters")
        supports_tools: bool | None = None
        if isinstance(supported, list):
            supports_tools = "tools" in supported
        arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
        modalities = arch.get("input_modalities")
        vision: bool | None = None
        if isinstance(modalities, list):
            vision = "image" in modalities
        top = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
        created = raw.get("created")
        created_s = None
        if isinstance(created, (int, float)) and created > 0:
            created_s = datetime.fromtimestamp(created, UTC).date().isoformat()
        return ModelInfo(
            id=mid,
            name=str(raw.get("name") or mid),
            provider=self.name,
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            context_length=_int(raw.get("context_length")) or _int(top.get("context_length")),
            prompt_price=prompt,
            completion_price=completion,
            is_free=is_free,
            supports_tools=supports_tools,
            supports_vision=vision,
            max_output_tokens=_int(top.get("max_completion_tokens")),
            routing="openrouter",
            created=created_s,
        )

    def validate_credentials(self) -> ValidationResult:
        if not self._api_key:
            return ValidationResult(False, "no API key supplied", ErrorKind.INVALID_CREDENTIALS.value)
        try:
            self._json("GET", "key")
        except ProviderError as exc:
            return ValidationResult(False, str(exc), exc.kind.value)
        return ValidationResult(True, "key accepted by OpenRouter")

    def get_usage_information(self) -> ProviderUsageInfo | None:
        try:
            data = self._json("GET", "key")
        except ProviderError:
            return None
        info = data.get("data") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return None
        return ProviderUsageInfo(
            label=info.get("label"),
            used=_price(info.get("usage")),
            limit=_price(info.get("limit")),
            remaining=_price(info.get("limit_remaining")),
            is_free_tier=info.get("is_free_tier") if isinstance(info.get("is_free_tier"), bool) else None,
            raw={k: v for k, v in info.items() if k != "label"},
        )
