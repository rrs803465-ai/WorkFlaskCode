from __future__ import annotations

from workflaskcode.config.credentials import CredentialStore
from workflaskcode.config.models import ProviderConfig, ProviderKind, Settings
from workflaskcode.errors import ConfigError
from workflaskcode.http import HttpClient
from workflaskcode.providers.anthropic import ANTHROPIC_BASE, AnthropicProvider
from workflaskcode.providers.base import Provider
from workflaskcode.providers.openai_compat import DEFAULT_OPENAI_BASE, OpenAICompatProvider
from workflaskcode.providers.openrouter import OPENROUTER_BASE, OpenRouterProvider

DEFAULT_BASES = {
    ProviderKind.OPENROUTER: OPENROUTER_BASE,
    ProviderKind.OPENAI: DEFAULT_OPENAI_BASE,
    ProviderKind.ANTHROPIC: ANTHROPIC_BASE,
}

KIND_LABELS = {
    ProviderKind.OPENROUTER: "OpenRouter",
    ProviderKind.OPENAI: "OpenAI (and compatible)",
    ProviderKind.ANTHROPIC: "Anthropic (native API)",
    ProviderKind.CUSTOM: "Custom OpenAI-compatible endpoint",
}


def build_provider(config: ProviderConfig, api_key: str | None, http: HttpClient | None = None) -> Provider:
    if config.kind is ProviderKind.OPENROUTER:
        return OpenRouterProvider(config, api_key, http)
    if config.kind is ProviderKind.ANTHROPIC:
        return AnthropicProvider(config, api_key, http)
    if config.kind in (ProviderKind.OPENAI, ProviderKind.CUSTOM):
        return OpenAICompatProvider(config, api_key, http)
    raise ConfigError(f"unsupported provider kind: {config.kind}")


class ProviderRegistry:
    def __init__(
        self,
        settings: Settings,
        credentials: CredentialStore,
        http: HttpClient | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.http = http
        self._cache: dict[str, Provider] = {}

    def get(self, name: str) -> Provider:
        if name not in self._cache:
            cfg = self.settings.providers.get(name)
            if cfg is None:
                raise ConfigError(f"provider '{name}' is not configured", hint="run @providers")
            cred = self.credentials.get(name, cfg)
            self._cache[name] = build_provider(cfg, cred.value if cred else None, self.http)
        return self._cache[name]

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._cache.clear()
        else:
            self._cache.pop(name, None)

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._cache.clear()
