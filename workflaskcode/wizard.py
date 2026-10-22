from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode import __version__
from workflaskcode.commands.registry import Interaction
from workflaskcode.config.credentials import CredentialStore
from workflaskcode.config.models import (
    InteractionMode,
    ModelRef,
    PermissionMode,
    ProviderConfig,
    ProviderKind,
    Settings,
)
from workflaskcode.config.paths import AgentHome
from workflaskcode.config.store import ConfigStore, settings_from_dict
from workflaskcode.errors import CredentialStoreUnavailable, ModelListingUnsupported, WorkFlaskError
from workflaskcode.http import HttpClient
from workflaskcode.providers.base import Provider
from workflaskcode.providers.catalog import ModelCatalog, filter_models, search_models, sort_models
from workflaskcode.providers.registry import KIND_LABELS, build_provider
from workflaskcode.providers.types import ModelInfo
from workflaskcode.ui.console import Console

BACK, CANCEL, NEXT = "back", "cancel", "next"
PERMISSION_HELP = {
    PermissionMode.READ_ONLY: "read-only: the agent can inspect but never write files or run commands",
    PermissionMode.APPROVE_ALL: "approve all: every file write and command asks you first",
    PermissionMode.APPROVE_RISKY: "approve risky (default): safe reads and project-local checks run; risky or destructive actions ask",
    PermissionMode.RESTRICTED_AUTONOMOUS: "restricted autonomous: allow-listed commands run unattended, destructive actions still ask",
}
DEFAULT_NAMES = {
    ProviderKind.OPENROUTER: "openrouter",
    ProviderKind.OPENAI: "openai",
    ProviderKind.ANTHROPIC: "anthropic",
    ProviderKind.CUSTOM: "custom",
}
ENV_DEFAULTS = {
    ProviderKind.OPENROUTER: "OPENROUTER_API_KEY",
    ProviderKind.OPENAI: "OPENAI_API_KEY",
    ProviderKind.ANTHROPIC: "ANTHROPIC_API_KEY",
    ProviderKind.CUSTOM: "CUSTOM_API_KEY",
}


def format_model(m: ModelInfo) -> str:
    ctx = f"{m.context_length // 1000}k ctx" if m.context_length else "ctx ?"
    tools = {True: "tools", False: "no tools", None: "tools ?"}[m.supports_tools]
    vision = " vision" if m.supports_vision else ""
    return f"{m.id}  ·  {ctx}  ·  {m.price_label()}  ·  {tools}{vision}"


@dataclass
class WizardResult:
    settings: Settings | None
    saved: bool
    notes: list[str] = field(default_factory=list)


class SetupWizard:
    def __init__(
        self,
        home: AgentHome,
        console: Console,
        interaction: Interaction,
        *,
        http: HttpClient | None = None,
        existing: Settings | None = None,
        git_available: bool | None = None,
    ) -> None:
        self.home = home
        self.console = console
        self.io = interaction
        self.http = http
        self.store = ConfigStore(home)
        self.credentials = CredentialStore(home)
        self.catalog = ModelCatalog(home.cache_dir)
        self.existing = existing or (self.store.load_or_default() if self.store.exists() else Settings())
        self.git_available = shutil.which("git") is not None if git_available is None else git_available
        self.data: dict[str, Any] = {}
        self.notes: list[str] = []

    def _ask(self, prompt: str, default: str | None = None, *, secret: bool = False) -> str | None:
        shown = f"{prompt} [{default}]" if default else prompt
        answer = self.io.ask(shown + "  (back / cancel)", secret=secret, default=default)
        if answer is None:
            return CANCEL
        answer = answer.strip()
        if answer.lower() in ("cancel", "quit", "exit"):
            return CANCEL
        if answer.lower() == "back":
            return BACK
        return answer if answer else (default or "")

    def run(self) -> WizardResult:
        steps = [
            self.step_welcome,
            self.step_workspace,
            self.step_provider,
            self.step_credentials,
            self.step_models,
            self.step_choose_model,
            self.step_test,
            self.step_mode,
            self.step_permissions,
            self.step_fallbacks,
            self.step_git,
            self.step_browser,
            self.step_validate,
        ]
        i = 0
        while i < len(steps):
            outcome = steps[i]()
            if outcome == CANCEL:
                self.console.info("setup cancelled; nothing was changed")
                return WizardResult(None, False, self.notes)
            if outcome == BACK:
                i = max(i - 1, 0)
                continue
            i += 1
        return self.step_summary()

    def _header(self, number: int, title: str) -> None:
        self.console.line()
        self.console.heading(f"Step {number}/14  {title}")

    def step_welcome(self) -> str:
        c = self.console
        self._header(1, "Welcome")
        c.panel(
            f"WorkFlaskCode {__version__}",
            [
                "A terminal coding agent that works inside a project folder you choose.",
                "It edits files and runs commands only within the permission mode you pick.",
                "Your API keys never go into config files. Telemetry is off.",
                "",
                "At every question you can type 'back' or 'cancel'.",
            ],
        )
        answer = self._ask("press Enter to begin", "")
        return answer if answer in (BACK, CANCEL) else NEXT

    def step_workspace(self) -> str:
        c = self.console
        self._header(2, "Workspace and data directory")
        c.kv(
            [
                ("agent home", str(self.home.root)),
                ("config file", str(self.home.config_file)),
                ("database", str(self.home.db_path)),
            ]
        )
        c.dim(
            "The agent home is private state. Projects live elsewhere. Change it by setting WORKFLASKCODE_HOME."
        )
        default_root = self.existing.default_project_root or ""
        answer = self._ask(
            "default project folder (blank = ask each time, the current folder is used when you start inside a project)",
            default_root,
        )
        if answer in (BACK, CANCEL):
            return answer
        if answer:
            path = Path(answer).expanduser()
            if not path.is_dir():
                if self.io.confirm(f"{path} does not exist. Create it?", False):
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    c.warn("skipped; no default project folder will be saved")
                    answer = ""
            if answer:
                resolved = path.resolve()
                if resolved == self.home.root or self.home.root in resolved.parents:
                    c.warn("a project cannot live inside the agent home; ignoring")
                    answer = ""
                else:
                    answer = str(resolved)
        self.data["default_project_root"] = answer or None
        choice = self.io.choose(
            "Where should project metadata (task state, handoff, design lock) be stored?",
            ["home", "project"],
            lambda v: {
                "home": "agent home (nothing added to your repo)",
                "project": "inside the project in .workflaskcode/ (can be committed)",
            }[v],
        )
        self.data["metadata_location"] = choice or self.existing.metadata_location
        return NEXT

    def step_provider(self) -> str:
        self._header(3, "Provider")
        kinds = list(ProviderKind)
        kind = self.io.choose("Choose a provider type", kinds, lambda k: KIND_LABELS[k])
        if kind is None:
            return CANCEL
        name = self._ask("name for this provider (lowercase letters, digits, - _)", DEFAULT_NAMES[kind])
        if name in (BACK, CANCEL):
            return name
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,39}$", name):
            self.console.warn("invalid name; use a short lowercase slug")
            return self.step_provider()
        base_url = None
        if kind is ProviderKind.CUSTOM or kind is ProviderKind.OPENAI:
            default = None if kind is ProviderKind.OPENAI else ""
            answer = self._ask(
                "base URL (e.g. https://host/v1)"
                + (" - blank for the official endpoint" if kind is ProviderKind.OPENAI else ""),
                default,
            )
            if answer in (BACK, CANCEL):
                return answer
            base_url = answer or None
        env_var = self._ask("environment variable that holds the key", ENV_DEFAULTS[kind])
        if env_var in (BACK, CANCEL):
            return env_var
        try:
            config = ProviderConfig(name=name, kind=kind, base_url=base_url, env_var=env_var or None)
        except ValueError as exc:
            self.console.warn(f"invalid provider settings: {exc}")
            return self.step_provider()
        self.data["provider"] = config
        return NEXT

    def step_credentials(self) -> str:
        self._header(4, "API credentials")
        config: ProviderConfig = self.data["provider"]
        name = config.name
        existing = self.credentials.get(name, config)
        if existing:
            self.console.ok(f"found a key: {existing.masked} (from {existing.source})")
            if self.io.confirm("use it?", True):
                self.data["key"] = None
                self.data["key_source"] = existing.source
                return self._validate_key(config)
        self.console.dim(
            f"Paste your key (input is hidden), or leave blank to supply it later via the {config.env_var or 'environment'} variable."
        )
        key = self._ask("API key", None, secret=True)
        if key in (BACK, CANCEL):
            return key
        if not key:
            self.data["key"] = None
            self.data["key_source"] = None
            self.console.warn(
                "no key supplied: models cannot be listed or tested; you will enter a model id by hand"
            )
            return NEXT
        self.data["key"] = key
        self.data["key_source"] = "pending"
        return self._validate_key(config)

    def _provider(self, config: ProviderConfig) -> Provider:
        key = self.data.get("key")
        if not key:
            cred = self.credentials.get(config.name, config)
            key = cred.value if cred else None
        return build_provider(config, key, self.http)

    def _validate_key(self, config: ProviderConfig) -> str:
        provider = self._provider(config)
        with self.console.spinner("checking the credential"):
            try:
                result = provider.validate_credentials()
            except WorkFlaskError as exc:
                self.console.warn(f"could not check the key: {exc}")
                return self._retry_key()
        if result.ok:
            self.console.ok(result.detail or "credential accepted")
            return NEXT
        if result.inconclusive:
            self.console.warn(f"the key could not be confirmed ({result.detail}); continuing")
            return NEXT
        self.console.error(f"credential rejected: {result.detail}")
        return self._retry_key()

    def _retry_key(self) -> str:
        if self.io.confirm("enter a different key?", True):
            self.data.pop("key", None)
            existing = self.credentials.get(self.data["provider"].name, self.data["provider"])
            if existing and not self.data.get("key"):
                pass
            return self.step_credentials_force()
        if self.io.confirm("continue anyway (model listing will likely fail)?", False):
            return NEXT
        return BACK

    def step_credentials_force(self) -> str:
        key = self._ask("API key", None, secret=True)
        if key in (BACK, CANCEL):
            return key
        self.data["key"] = key or None
        self.data["key_source"] = "pending" if key else None
        return self._validate_key(self.data["provider"]) if key else NEXT

    def step_models(self) -> str:
        self._header(5, "Model discovery")
        config: ProviderConfig = self.data["provider"]
        provider = self._provider(config)
        self.data["models"] = []
        with self.console.spinner("fetching the provider's model list"):
            try:
                models, origin = self.catalog.get(provider, refresh=True)
            except ModelListingUnsupported as exc:
                self.console.warn(
                    f"this endpoint does not list models ({exc}). You will enter a model id manually."
                )
                return NEXT
            except WorkFlaskError as exc:
                self.console.warn(f"model list unavailable: {exc}")
                return NEXT
            except Exception as exc:
                self.console.warn(f"model list unavailable: {type(exc).__name__}: {exc}")
                return NEXT
        self.data["models"] = models
        self.console.ok(f"{len(models)} models returned by {config.name} ({origin})")
        return NEXT

    def step_choose_model(self) -> str:
        self._header(6, "Choose a model")
        models: list[ModelInfo] = self.data.get("models", [])
        config: ProviderConfig = self.data["provider"]
        if not models:
            answer = self._ask("model id to use (exactly as the provider names it)")
            if answer in (BACK, CANCEL):
                return answer
            if not answer:
                self.console.warn("a model id is required")
                return self.step_choose_model()
            self.data["model_id"] = answer
            self.data["model_info"] = None
            return NEXT
        filters = self._ask("filter: free, tools, vision, a search word, or blank for all", "")
        if filters in (BACK, CANCEL):
            return filters
        pool = list(models)
        words = []
        for token in filters.split():
            low = token.lower()
            if low == "free":
                pool = filter_models(pool, free=True)
            elif low == "tools":
                pool = filter_models(pool, tools=True)
            elif low == "vision":
                pool = filter_models(pool, vision=True)
            else:
                words.append(token)
        pool = search_models(pool, " ".join(words))
        if not pool:
            self.console.warn("no model matches that filter")
            return self.step_choose_model()
        pool = sort_models(pool, "name")
        choice = self.io.choose(f"Select a model ({len(pool)} match; type to filter)", pool, format_model)
        if choice is None:
            return BACK
        self.data["model_id"] = choice.id
        self.data["model_info"] = choice
        info: ModelInfo = choice
        if info.is_free is False and not config.allow_paid:
            self.console.warn(
                f"{info.id} is a paid model ({info.price_label()}). Paid inference uses your account balance."
            )
            if not self.io.confirm("allow paid inference on this provider?", False):
                self.console.info("choose a free model, or allow paid usage")
                return self.step_choose_model()
            self.data["provider"] = config.model_copy(update={"allow_paid": True})
        return NEXT

    def step_test(self) -> str:
        self._header(7, "Connectivity and capability test")
        config: ProviderConfig = self.data["provider"]
        model_id = self.data["model_id"]
        if not (self.data.get("key") or self.credentials.get(config.name, config)):
            self.console.warn("no key available; skipping live tests. The model is saved but unverified.")
            self.data["verified"] = False
            return NEXT
        provider = self._provider(config)
        with self.console.spinner(f"asking {model_id} for a minimal reply"):
            started = time.monotonic()
            result = provider.validate_model(model_id)
            latency = time.monotonic() - started
        if not result.ok:
            self.console.error(f"the model did not respond: {result.detail}")
            choice = self.io.choose(
                "What now?", ["retry", "choose another model", "save anyway (unverified)"], str
            )
            if choice == "retry":
                return self.step_test()
            if choice == "choose another model":
                return BACK
            if choice is None:
                return BACK
            self.data["verified"] = False
            return NEXT
        self.console.ok(f"responded in {latency:.1f}s")
        with self.console.spinner("probing tool-calling support"):
            tools = provider.test_tool_calling(model_id)
        info: ModelInfo | None = self.data.get("model_info")
        if tools.ok:
            self.console.ok("tool calling works (required by the coding agent)")
        elif tools.inconclusive:
            self.console.warn(f"tool-calling probe inconclusive: {tools.detail}")
        else:
            self.console.warn(
                f"this model did not make a usable tool call ({tools.detail}). It can chat but the coding agent will likely fail."
            )
            if not self.io.confirm("keep this model anyway?", False):
                return BACK
        if info is not None:
            self.console.kv(
                [
                    ("context", f"{info.context_length or 'unknown'}"),
                    ("price", info.price_label()),
                    ("tools (catalogue)", str(info.supports_tools)),
                    ("vision", str(info.supports_vision)),
                ]
            )
        self.data["verified"] = True
        return NEXT

    def step_mode(self) -> str:
        self._header(8, "Default interaction mode")
        choice = self.io.choose(
            "Start in",
            [InteractionMode.CODING, InteractionMode.CHAT],
            lambda m: {
                InteractionMode.CODING: "@codingagent: plans, edits files, runs tests",
                InteractionMode.CHAT: "@chat: questions only, read-only",
            }[m],
        )
        self.data["mode"] = choice or self.existing.mode
        return NEXT

    def step_permissions(self) -> str:
        self._header(9, "Command permission policy")
        modes = list(PermissionMode)
        choice = self.io.choose(
            "How much should the agent ask before acting?", modes, lambda m: PERMISSION_HELP[m]
        )
        self.data["permission_mode"] = choice or PermissionMode.APPROVE_RISKY
        self.console.dim("This is an approval policy, not a sandbox. Review commands the agent proposes.")
        return NEXT

    def step_fallbacks(self) -> str:
        self._header(10, "Fallback models (optional)")
        self.data.setdefault("fallbacks", [])
        models: list[ModelInfo] = self.data.get("models", [])
        if not models:
            self.console.dim("skipped: no model list is available")
            return NEXT
        while self.io.confirm(
            "add a fallback model used when the primary is rate-limited or unavailable?", False
        ):
            pool = [m for m in sort_models(models, "name") if m.id != self.data["model_id"]]
            choice = self.io.choose("Fallback model", pool, format_model)
            if choice is None:
                break
            if choice.is_free is False and not self.data["provider"].allow_paid:
                self.console.warn(f"{choice.id} is paid; it will be skipped unless paid inference is enabled")
            self.data["fallbacks"].append(ModelRef(provider=self.data["provider"].name, model_id=choice.id))
        return NEXT

    def step_git(self) -> str:
        self._header(11, "Git integration")
        if not self.git_available:
            self.console.warn("git was not found; integration will be disabled")
            self.data["git"] = False
            return NEXT
        self.data["git"] = self.io.confirm(
            "enable git integration (status, diffs, checkpoints; never pushes automatically)?", True
        )
        return NEXT

    def step_browser(self) -> str:
        self._header(12, "Browser testing (optional)")
        from workflaskcode.browser import playwright_status

        ok, message = playwright_status()
        self.console.dim(message)
        self.data["browser"] = self.io.confirm("enable browser preview/testing?", False) if ok else False
        if not ok:
            self.console.dim("skipped; install later with the command above")
        return NEXT

    def build_settings(self) -> Settings:
        providers = dict(self.existing.providers)
        config: ProviderConfig = self.data["provider"]
        providers[config.name] = config
        raw = self.existing.to_toml_dict()
        raw.update(
            {
                "default_provider": config.name,
                "coding_model": str(ModelRef(provider=config.name, model_id=self.data["model_id"])),
                "chat_model": str(ModelRef(provider=config.name, model_id=self.data["model_id"])),
                "fallback_models": [str(m) for m in self.data.get("fallbacks", [])],
                "mode": self.data.get("mode", InteractionMode.CODING).value,
                "metadata_location": self.data.get("metadata_location", self.existing.metadata_location),
                "providers": {k: v.model_dump(mode="json", exclude_none=True) for k, v in providers.items()},
            }
        )
        if self.data.get("default_project_root"):
            raw["default_project_root"] = self.data["default_project_root"]
        raw.setdefault("permissions", {})["mode"] = self.data.get(
            "permission_mode", PermissionMode.APPROVE_RISKY
        ).value
        raw.setdefault("git", {})["enabled"] = bool(self.data.get("git", True))
        raw.setdefault("browser", {})["enabled"] = bool(self.data.get("browser", False))
        return settings_from_dict(raw)

    def step_validate(self) -> str:
        self._header(13, "Validate configuration")
        try:
            self.data["settings"] = self.build_settings()
        except WorkFlaskError as exc:
            self.console.error(f"the configuration is not valid: {exc}")
            return BACK
        except Exception as exc:
            self.console.error(f"the configuration is not valid: {exc}")
            return BACK
        self.console.ok("configuration is valid")
        return NEXT

    def step_summary(self) -> WizardResult:
        self._header(14, "Summary")
        settings: Settings = self.data["settings"]
        config: ProviderConfig = self.data["provider"]
        storage = (
            "will be stored securely after you confirm"
            if self.data.get("key")
            else (
                f"existing ({self.data.get('key_source')})"
                if self.data.get("key_source")
                else "not set; export " + (config.env_var or "the key variable")
            )
        )
        self.console.kv(
            [
                ("provider", f"{config.name} ({config.kind.value})"),
                ("coding model", str(settings.coding_model)),
                ("fallbacks", ", ".join(map(str, settings.fallback_models)) or "none"),
                ("mode", settings.mode.value),
                ("permissions", settings.permissions.mode.value),
                ("paid inference", "allowed" if config.allow_paid else "blocked"),
                ("git", "on" if settings.git.enabled else "off"),
                ("browser", "on" if settings.browser.enabled else "off"),
                ("metadata", settings.metadata_location),
                ("API key", storage),
                ("verified live", "yes" if self.data.get("verified") else "no"),
            ],
            title="configuration",
        )
        if not self.io.confirm("save this configuration?", True):
            self.console.info("nothing was saved")
            return WizardResult(None, False, self.notes)
        key = self.data.get("key")
        if key:
            try:
                where = self.credentials.set(config.name, key)
            except CredentialStoreUnavailable as exc:
                self.console.warn(str(exc))
                if self.io.confirm(
                    f"store the key in a file only you can read ({self.credentials._file_path(config.name)}, mode 0600)?",
                    False,
                ):
                    where = self.credentials.set(config.name, key, allow_file_fallback=True)
                else:
                    where = None
                    self.notes.append(f"key not stored; export {config.env_var} before each run")
            if where:
                self.console.ok(f"API key stored in the {where}")
        self.store.save(settings)
        self.console.ok(f"saved {self.store.path}")
        return WizardResult(settings, True, self.notes)
