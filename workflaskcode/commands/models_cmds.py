from __future__ import annotations

import time

from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.config.models import ModelRef, ProviderConfig, ProviderKind
from workflaskcode.config.store import set_dotted
from workflaskcode.errors import CredentialStoreUnavailable, ModelListingUnsupported, WorkFlaskError
from workflaskcode.providers.base import Provider
from workflaskcode.providers.catalog import filter_models, find_model, search_models, sort_models
from workflaskcode.providers.registry import KIND_LABELS, build_provider
from workflaskcode.providers.types import Message, ModelInfo
from workflaskcode.wizard import DEFAULT_NAMES, ENV_DEFAULTS, format_model


def _provider_name(ctx: CommandContext, explicit: str | None = None) -> str:
    s = ctx.rt.settings
    name = (
        explicit
        or (str(ctx.flag("provider")) if ctx.flag("provider") not in (None, True) else None)
        or s.default_provider
        or (s.coding_model.provider if s.coding_model else None)
    )
    if not name:
        raise CommandError("no provider is configured", hint="run @setup")
    if name not in s.providers:
        raise CommandError(f"provider '{name}' is not configured", hint="see @providers")
    return name


def _models(ctx: CommandContext, name: str, refresh: bool = False) -> tuple[Provider, list[ModelInfo], str]:
    provider = ctx.rt.providers.get(name)
    if provider._api_key is None and provider.config.kind is not ProviderKind.CUSTOM:
        raise CommandError(f"no API key for provider '{name}'", hint=f"@providers key {name}")
    try:
        with ctx.console.spinner(f"loading models from {name}"):
            models, origin = ctx.rt.catalog.get(provider, refresh=refresh)
    except ModelListingUnsupported as exc:
        raise CommandError(
            f"{name} does not support model listing ({exc})",
            hint="use @changemodel provider:model-id with an id from the provider's documentation",
        ) from exc
    ctx.rt.tracker.load_prices(name, models)
    return provider, models, origin


def cmd_models(ctx: CommandContext) -> None:
    c = ctx.console
    refresh = ctx.sub == "refresh"
    query_words = ctx.positional[1:] if ctx.sub in ("refresh", "search") else ctx.positional
    name = _provider_name(ctx)
    _, models, origin = _models(ctx, name, refresh)
    pool = list(models)
    if ctx.flag("free"):
        pool = filter_models(pool, free=True)
    if ctx.flag("tools"):
        pool = filter_models(pool, tools=True)
    if ctx.flag("vision"):
        pool = filter_models(pool, vision=True)
    if ctx.flag("min-context"):
        pool = filter_models(pool, min_context=ctx.int_flag("min-context", 0))
    pool = search_models(pool, " ".join(query_words))
    try:
        pool = sort_models(pool, str(ctx.flag("sort", "name")))
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    limit = ctx.int_flag("limit", 40)
    c.heading(f"{name} models", f"{len(pool)} shown of {len(models)} returned ({origin})")
    rows = []
    for m in pool[:limit]:
        rows.append(
            [
                m.id,
                f"{m.context_length // 1000}k" if m.context_length else "?",
                m.price_label(),
                {True: "yes", False: "no", None: "?"}[m.supports_tools],
                {True: "yes", False: "no", None: "?"}[m.supports_vision],
            ]
        )
    if rows:
        c.table(
            ["model id", "ctx", "price", "tools", "vision"],
            rows,
            aligns=["left", "right", "left", "left", "left"],
            shrink_order=[0, 2],
        )
    if len(pool) > limit:
        c.dim(f"{len(pool) - limit} more; narrow with a search word or --free --tools --sort price --limit N")
    c.dim("'?' means the provider did not report it. Select with @changemodel <id>.")


def cmd_changemodel(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    role = str(ctx.flag("role", "coding"))
    if role not in ("coding", "chat"):
        raise CommandError("--role must be coding or chat")
    target = " ".join(ctx.positional)
    if not target:
        name = _provider_name(ctx)
        _, models, _ = _models(ctx, name)
        pool = sort_models(models, "name")
        pick = ctx.interaction.choose(f"Select a {role} model from {name}", pool, format_model)
        if pick is None:
            raise CommandError("no model selected")
        target = f"{name}:{pick.id}"
    elif ":" not in target and rt.settings.default_provider:
        name = rt.settings.default_provider
        try:
            _, models, _ = _models(ctx, name)
            hit = find_model(models, target)
            if hit:
                target = f"{name}:{hit.id}"
        except WorkFlaskError:
            pass
    report = rt.switch_model(
        target, role=role, force=bool(ctx.flag("force")), probe_tools=not ctx.flag("no-probe")
    )
    c.ok(f"{role} model: {report.old or 'unset'}  ->  {report.new}")
    for step in report.steps:
        c.line(f"  {c.r.c['ok']} {step}")
    for warning in report.warnings:
        c.warn(warning)
    if rt.ledger and rt.ledger.list() and not report.resumed:
        c.dim("tasks, files and context were kept; @resume continues from the next unfinished step")


def _provider_config_from(ctx: CommandContext) -> ProviderConfig:
    kinds = list(ProviderKind)
    kind = ctx.interaction.choose("Provider type", kinds, lambda k: KIND_LABELS[k])
    if kind is None:
        raise CommandError("cancelled")
    name = ctx.interaction.ask("provider name", default=DEFAULT_NAMES[kind]) or ""
    base = None
    if kind in (ProviderKind.CUSTOM, ProviderKind.OPENAI):
        base = (
            ctx.interaction.ask(
                "base URL" + (" (blank = official)" if kind is ProviderKind.OPENAI else ""), default=None
            )
            or None
        )
    env_var = ctx.interaction.ask("environment variable for the key", default=ENV_DEFAULTS[kind])
    try:
        return ProviderConfig(name=name.strip(), kind=kind, base_url=base, env_var=env_var or None)
    except ValueError as exc:
        raise CommandError(f"invalid provider: {exc}") from exc


def _store_key(ctx: CommandContext, config: ProviderConfig, key: str) -> None:
    rt = ctx.rt
    probe = build_provider(config, key, rt.http)
    with ctx.console.spinner("checking the key"):
        result = probe.validate_credentials()
    if not result.ok and not result.inconclusive:
        raise CommandError(f"the provider rejected the key: {result.detail}", hint="nothing was stored")
    try:
        where = rt.credentials.set(config.name, key)
    except CredentialStoreUnavailable as exc:
        ctx.console.warn(str(exc))
        if not ctx.interaction.confirm("store it in a private file (mode 0600) in the agent home?", False):
            ctx.console.info(f"not stored. Export {config.env_var} instead.")
            return
        where = rt.credentials.set(config.name, key, allow_file_fallback=True)
    rt.providers.invalidate(config.name)
    rt.router.exhausted_providers.pop(config.name, None)
    ctx.console.ok(
        f"key for {config.name} stored in the {where} ({'rotated' if result.ok else 'unverified'})"
    )


def cmd_providers(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    sub = ctx.sub or "list"
    s = rt.settings
    if sub == "list":
        rows = []
        for name, cfg in s.providers.items():
            rows.append(
                [
                    name,
                    cfg.kind.value,
                    cfg.base_url or "(official)",
                    rt.credentials.describe(name, cfg),
                    "yes" if cfg.allow_paid else "no",
                    "default" if name == s.default_provider else "",
                ]
            )
        if not rows:
            c.dim("no providers yet; use @providers add")
            return
        c.table(["name", "kind", "endpoint", "key", "paid ok", ""], rows)
        return
    if sub == "add":
        config = _provider_config_from(ctx)
        if config.name in s.providers:
            raise CommandError(f"provider '{config.name}' already exists")
        rt.apply_settings(
            lambda cur: cur.model_copy(
                update={
                    "providers": {**cur.providers, config.name: config},
                    "default_provider": cur.default_provider or config.name,
                }
            )
        )
        c.ok(f"added provider {config.name}")
        key = ctx.interaction.ask(
            f"API key for {config.name} (hidden; blank to use ${config.env_var})", secret=True
        )
        if key:
            _store_key(ctx, config, key)
        return
    name = ctx.need(2, f"providers {sub} <name>")[1]
    cfg = s.providers.get(name)
    if cfg is None:
        raise CommandError(f"unknown provider '{name}'")
    if sub == "key":
        key = ctx.interaction.ask(f"new API key for {name} (hidden)", secret=True)
        if not key:
            raise CommandError("no key entered; nothing changed")
        _store_key(ctx, cfg, key)
    elif sub == "remove":
        used = [
            str(r) for r in [s.coding_model, s.chat_model, *s.fallback_models] if r and r.provider == name
        ]
        if used:
            raise CommandError(
                f"{name} is still used by: {', '.join(used)}", hint="change those models first"
            )
        if not ctx.interaction.confirm(f"remove provider {name}?", False):
            return
        rt.apply_settings(
            lambda cur: cur.model_copy(
                update={
                    "providers": {k: v for k, v in cur.providers.items() if k != name},
                    "default_provider": None if cur.default_provider == name else cur.default_provider,
                }
            )
        )
        if rt.credentials.get(name, cfg) and ctx.interaction.confirm(
            "also delete its stored API key (keyring/file)?", False
        ):
            rt.credentials.remove(name)
        c.ok(f"removed {name}")
    elif sub == "test":
        provider = rt.providers.get(name)
        started = time.monotonic()
        with c.spinner(f"testing {name}"):
            result = provider.validate_credentials()
            try:
                models = provider.list_models()
                listing = f"{len(models)} models listed"
            except ModelListingUnsupported:
                listing = "model listing unsupported by this endpoint"
            except WorkFlaskError as exc:
                listing = f"model listing failed: {exc}"
        (c.ok if result.ok else c.warn)(
            f"credentials: {result.detail}  ·  {listing}  ·  {time.monotonic() - started:.1f}s"
        )
    elif sub == "paid":
        flag = (ctx.positional[2] if len(ctx.positional) > 2 else "").lower()
        if flag not in ("on", "off"):
            raise CommandError("usage: @providers paid <name> on|off")
        if flag == "on" and not ctx.interaction.confirm(
            f"allow paid models on {name}? This can spend real money from your account (limits in @limits apply).",
            False,
        ):
            return
        rt.apply_settings(
            lambda cur: cur.model_copy(
                update={
                    "providers": {
                        **cur.providers,
                        name: cur.providers[name].model_copy(update={"allow_paid": flag == "on"}),
                    }
                }
            )
        )
        c.ok(f"paid inference on {name}: {flag}")
    elif sub == "show":
        c.kv(
            [
                ("name", name),
                ("kind", cfg.kind.value),
                ("endpoint", cfg.base_url or "(official)"),
                ("key", rt.credentials.describe(name, cfg)),
                ("env var", cfg.env_var or "-"),
                ("max concurrency", str(cfg.max_concurrency)),
                ("timeout", f"{cfg.timeout_seconds}s"),
                ("paid allowed", str(cfg.allow_paid)),
            ],
            title=name,
        )
    else:
        raise CommandError(
            "usage: @providers [list|add|key <n>|remove <n>|test <n>|paid <n> on|off|show <n>]"
        )


def cmd_testmodel(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    text = " ".join(ctx.positional) or str(rt.model_for(rt.mode) or "")
    if not text:
        raise CommandError("no model configured; pass provider:model-id")
    ref = ModelRef.parse(text, rt.settings.default_provider)
    provider = rt.providers.get(ref.provider)
    rows = []
    started = time.monotonic()
    basic = provider.validate_model(ref.model_id)
    rows.append(
        [
            "minimal request",
            "pass" if basic.ok else "FAIL",
            f"{basic.detail}  ({time.monotonic() - started:.1f}s)",
        ]
    )
    if basic.ok:
        t0 = time.monotonic()
        tools = provider.test_tool_calling(ref.model_id)
        rows.append(
            [
                "tool calling",
                "pass" if tools.ok else ("inconclusive" if tools.inconclusive else "FAIL"),
                f"{tools.detail}  ({time.monotonic() - t0:.1f}s)",
            ]
        )
        t0 = time.monotonic()
        try:
            events = list(
                provider.stream_response([Message("user", "Count from 1 to 5.")], ref.model_id, max_tokens=40)
            )
            chunks = sum(1 for e in events if e.kind == "text")
            rows.append(
                [
                    "streaming",
                    "pass" if chunks > 1 else "single chunk",
                    f"{chunks} text chunk(s)  ({time.monotonic() - t0:.1f}s)",
                ]
            )
        except WorkFlaskError as exc:
            rows.append(["streaming", "FAIL", str(exc)[:100]])
    caps = provider.get_model_capabilities(ref.model_id)
    rows.append(
        [
            "catalogue",
            "info",
            f"context {caps.context_length or '?'}, tools {caps.tools}, vision {caps.vision}"
            + (f"; {caps.notes[0]}" if caps.notes else ""),
        ]
    )
    c.heading(f"test of {ref}")
    c.table(["check", "result", "detail"], rows, shrink_order=[2])


def cmd_usage(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    all_sessions = bool(ctx.flag("all"))
    totals = rt.tracker.totals(session=not all_sessions)
    c.heading("usage", "all sessions" if all_sessions else "this session")
    c.line("  " + rt.tracker.format_totals(totals))
    rows = []
    for r in rt.tracker.by_model(session=not all_sessions):
        cost = f"${(r['cost'] or 0):.4f}" + ("~" if r["est"] else "") + ("+?" if r["unknown_cost"] else "")
        rows.append(
            [
                f"{r['provider']}:{r['model']}",
                str(r["n"]),
                str(r["i"] or 0),
                str(r["o"] or 0),
                str(r["c"] or 0),
                cost,
                str(r["errs"] or 0),
            ]
        )
    if rows:
        c.table(["model", "req", "in", "out", "cached", "cost", "err"], rows, aligns=["left"] + ["right"] * 6)
        c.dim(
            "cost: ~ = estimated from catalogue prices; +? = some requests had no price data. Tokens are provider-reported when available."
        )
    else:
        c.dim("no requests recorded yet")
    if ctx.sub == "task" and rt.ledger and rt.ledger.current():
        t = rt.tracker.totals(session=False, task_id=rt.ledger.current().id)
        c.line("  current task: " + rt.tracker.format_totals(t))
    for name in rt.settings.providers:
        try:
            info = rt.providers.get(name).get_usage_information()
        except WorkFlaskError:
            info = None
        if info is None:
            c.dim(f"{name}: no quota/balance endpoint available")
        else:
            parts = [
                f"used {info.used}" if info.used is not None else None,
                f"limit {info.limit}" if info.limit is not None else None,
                f"remaining {info.remaining}" if info.remaining is not None else None,
            ]
            c.line(
                f"  {name}: "
                + ", ".join(p for p in parts if p)
                + (" (free tier)" if info.is_free_tier else "")
            )


LIMIT_KEYS = {
    "max_tool_calls_per_task": "limits.max_tool_calls_per_task",
    "max_requests_per_task": "limits.max_requests_per_task",
    "max_task_seconds": "limits.max_task_seconds",
    "max_cost_per_task": "limits.max_cost_per_task",
    "max_parallel_requests": "limits.max_parallel_requests",
    "max_side_chats": "limits.max_side_chats",
    "max_test_repair_attempts": "limits.max_test_repair_attempts",
    "max_attempts": "retry.max_attempts",
}


def cmd_limits(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    if ctx.sub == "set":
        pos = ctx.need(3, "limits set <name> <value>")
        key = LIMIT_KEYS.get(pos[1])
        if key is None:
            raise CommandError(f"unknown limit; choose from {', '.join(LIMIT_KEYS)}")
        try:
            updated = set_dotted(rt.global_settings, key, pos[2])
        except (ValueError, KeyError) as exc:
            raise CommandError(f"invalid value: {exc}") from exc
        rt.config.save(updated)
        rt.reload_settings()
        c.ok(f"{pos[1]} = {pos[2]}")
        return
    if ctx.sub == "budget":
        pos = ctx.need(3, "limits budget <provider:model> <usd|off>")
        ModelRef.parse(pos[1])
        budgets = dict(rt.global_settings.limits.model_budgets)
        if pos[2].lower() == "off":
            budgets.pop(pos[1], None)
        else:
            try:
                budgets[pos[1]] = float(pos[2])
            except ValueError as exc:
                raise CommandError("budget must be a number of USD") from exc
        rt.apply_settings(
            lambda cur: cur.model_copy(
                update={"limits": cur.limits.model_copy(update={"model_budgets": budgets})}
            )
        )
        c.ok(f"budget for {pos[1]}: {pos[2]}")
        return
    lim, retry = rt.settings.limits, rt.settings.retry
    c.kv(
        [
            ("max tool calls / task", str(lim.max_tool_calls_per_task)),
            ("max requests / task", str(lim.max_requests_per_task)),
            ("max task seconds", f"{lim.max_task_seconds:.0f}"),
            (
                "max cost / task (USD)",
                "unlimited" if lim.max_cost_per_task is None else f"{lim.max_cost_per_task:.4f}",
            ),
            ("max parallel requests", str(lim.max_parallel_requests)),
            ("max side chats", str(lim.max_side_chats)),
            ("max test repair attempts", str(lim.max_test_repair_attempts)),
            ("retry attempts", str(retry.max_attempts)),
            (
                "backoff",
                f"{retry.base_delay_seconds}s base, {retry.max_delay_seconds}s cap, {retry.jitter:.0%} jitter",
            ),
            ("model budgets", ", ".join(f"{k}=${v}" for k, v in lim.model_budgets.items()) or "none"),
        ],
        title="limits",
    )
    events = rt.tracker.recent_rate_limits(5)
    if events:
        c.heading("recent rate-limit / quota events")
        for e in events:
            c.line(
                f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))}  {e['provider']}:{e['model']}  {e['error_kind']}"
            )
    c.dim(
        "change with @limits set <name> <value> or @limits budget <provider:model> <usd>. Providers' own quotas are enforced by the provider; use @usage to see what they report."
    )


def cmd_fallback(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    s = rt.settings
    sub = ctx.sub or "list"
    if sub in ("add", "remove"):
        text = " ".join(ctx.positional[1:])
        if not text:
            raise CommandError(f"usage: @fallback {sub} <provider:model-id>")
        ref = ModelRef.parse(text, s.default_provider)
        if ref.provider not in s.providers:
            raise CommandError(f"provider '{ref.provider}' is not configured")
        chain = [m for m in rt.global_settings.fallback_models if m != ref]
        if sub == "add":
            provider = rt.providers.get(ref.provider)
            try:
                _, models, _ = _models(ctx, ref.provider)
                info = next((m for m in models if m.id == ref.model_id), None)
            except WorkFlaskError:
                info = None
            if info is not None and info.is_free is False and not provider.config.allow_paid:
                c.warn(
                    f"{ref} is a paid model; it will be skipped unless you run @providers paid {ref.provider} on"
                )
            if info is not None and info.supports_tools is False:
                raise CommandError(
                    f"{ref} does not support tool calling; the coding agent cannot use it as a fallback"
                )
            chain.append(ref)
        rt.apply_settings(lambda cur: cur.model_copy(update={"fallback_models": chain}))
        c.ok(f"fallback chain: {' -> '.join(map(str, [s.coding_model, *chain]))}")
        return
    if sub == "clear":
        rt.apply_settings(lambda cur: cur.model_copy(update={"fallback_models": []}))
        c.ok("fallback chain cleared")
        return
    rows = []
    for i, ref in enumerate([s.coding_model, *s.fallback_models]):
        if ref is None:
            continue
        cfg = s.providers.get(ref.provider)
        key = "ok" if cfg and rt.credentials.get(ref.provider, cfg) else "no key"
        status = rt.router.exhausted_providers.get(ref.provider, "")
        rows.append(
            [
                "primary" if i == 0 else f"fallback {i}",
                str(ref),
                key,
                "paid ok" if cfg and cfg.allow_paid else "free only",
                status or "available",
            ]
        )
    c.table(["slot", "model", "key", "billing", "state"], rows) if rows else c.dim("no models configured")
    c.dim(
        "When every model fails the task pauses with progress saved. Fallbacks only use providers and keys you configured; keys are never rotated to dodge limits."
    )


def cmd_retry(ctx: CommandContext) -> None:
    rt, c = ctx.rt, ctx.console
    if ctx.sub == "policy":
        pos = ctx.positional[1:]
        if pos:
            updates = {}
            for key, value in zip(pos[::2], pos[1::2], strict=False):
                field = {
                    "attempts": "max_attempts",
                    "base": "base_delay_seconds",
                    "max": "max_delay_seconds",
                    "jitter": "jitter",
                }.get(key)
                if field is None:
                    raise CommandError("usage: @retry policy [attempts N] [base S] [max S] [jitter F]")
                updates[field] = float(value) if field != "max_attempts" else int(value)
            try:
                rt.apply_settings(
                    lambda cur: cur.model_copy(
                        update={
                            "retry": type(cur.retry).model_validate({**cur.retry.model_dump(), **updates})
                        }
                    )
                )
            except ValueError as exc:
                raise CommandError(f"invalid retry policy: {exc}") from exc
        r = rt.settings.retry
        c.kv(
            [
                ("max attempts / model", str(r.max_attempts)),
                ("base delay", f"{r.base_delay_seconds}s"),
                ("max delay", f"{r.max_delay_seconds}s"),
                ("jitter", f"{r.jitter:.0%}"),
            ],
            title="retry policy",
        )
        c.dim(
            "Transient errors (network, timeout, overload, model rate limit) are retried with exponential backoff and Retry-After. Invalid credentials and billing errors are never retried."
        )
        return
    if rt.worker_running:
        raise CommandError("a task is already running")
    rt.router.reset_exhaustion()
    c.info("provider availability reset; " + rt.resume_task())


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    add(
        "models",
        cmd_models,
        "list the provider's real models (dynamic, filterable)",
        "[refresh|search <words>] [--free] [--tools] [--vision] [--min-context N] [--sort name|id|context|price|newest] [--limit N]",
        "models",
        examples=("@models search llama --free --tools",),
    )
    add(
        "changemodel",
        cmd_changemodel,
        "switch model without losing work",
        "[provider:model-id] [--role coding|chat] [--force]",
        "models",
        ("model",),
        examples=("@changemodel openrouter:some/model",),
    )
    add(
        "providers",
        cmd_providers,
        "list, add, test and manage providers and keys",
        "[list|add|key <n>|remove <n>|test <n>|paid <n> on|off|show <n>]",
        "models",
    )
    add(
        "testmodel",
        cmd_testmodel,
        "test a model's connectivity, tools and streaming",
        "[provider:model-id]",
        "models",
    )
    add("usage", cmd_usage, "tokens, requests, cost and provider-reported quota", "[task] [--all]", "models")
    add(
        "limits",
        cmd_limits,
        "show or change request, cost and time limits",
        "[set <name> <value>|budget <provider:model> <usd|off>]",
        "models",
    )
    add(
        "fallback",
        cmd_fallback,
        "show or edit the fallback model chain",
        "[list|add <ref>|remove <ref>|clear]",
        "models",
    )
    add(
        "retry",
        cmd_retry,
        "resume after a failure, or show/set the retry policy",
        "[policy [attempts N] [base S] [max S] [jitter F]]",
        "models",
        needs_project=False,
    )
