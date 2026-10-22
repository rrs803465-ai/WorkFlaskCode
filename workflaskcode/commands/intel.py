from __future__ import annotations

import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from workflaskcode.audit.checks import find_cycles, import_graph, iter_files, rel
from workflaskcode.audit.deadcode import find_dead_code
from workflaskcode.audit.engine import AuditEngine, AuditReport
from workflaskcode.audit.models import CATEGORIES, SEVERITIES, Finding
from workflaskcode.commands.helpers import agent_task, info_lines, run_user_command
from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.errors import PathSecurityError
from workflaskcode.git import GitRepo
from workflaskcode.testing.runner import (
    benchmark_command,
    compare_benchmarks,
    load_benchmarks,
    parse_test_summary,
    save_benchmark,
)
from workflaskcode.utils.fs import atomic_write_text, read_text_safely
from workflaskcode.utils.redact import redact

SEVERITY_TOKEN = {"critical": "err", "high": "err", "medium": "warn", "low": "muted", "info": "muted"}
MAX_PROMPT_CHARS = 14000
ROUTE_RX = re.compile(
    r"""(?:@\w+\.(?:route|get|post|put|delete|patch)\(|\b(?:app|router)\.(?:get|post|put|delete|patch|use)\(|Route::\w+\()\s*['"]([^'"]+)['"]"""
)
TABLE_RX = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)", re.I)
MODEL_RX = re.compile(r"^class\s+(\w+)\((?:[\w.]*(?:Model|Base|db\.Model|SQLModel))\)", re.M)


def _git(ctx: CommandContext) -> GitRepo | None:
    git = ctx.rt.git
    return git if git is not None and GitRepo.available() and git.is_repo() else None


def _clip(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    text = redact(text)
    return text if len(text) <= limit else text[:limit] + f"\n[truncated {len(text) - limit} chars]"


def _plugin_findings(ctx: CommandContext, root: Path) -> list[Finding]:
    out: list[Finding] = []
    for name, fn in ctx.rt.plugins.audit_checks:
        try:
            rows = fn(root)
        except Exception as exc:
            ctx.console.warn(f"audit check {name} failed: {type(exc).__name__}: {exc}")
            continue
        for row in rows or []:
            try:
                out.append(
                    Finding(
                        row.get("category", "code-quality"),
                        row.get("severity", "low"),
                        row.get("rule", name),
                        row.get("file"),
                        row.get("line"),
                        str(row.get("evidence", ""))[:400],
                        str(row.get("explanation", "")),
                        str(row.get("fix", "")),
                        row.get("confidence", "low"),
                        bool(row.get("verified", False)),
                        name,
                    )
                )
            except (ValueError, TypeError) as exc:
                ctx.console.warn(f"{name} returned an invalid finding ({exc})")
    return out


def _run_audit(ctx: CommandContext, categories: list[str] | None, use_external: bool = True) -> AuditReport:
    root = ctx.project()
    try:
        with ctx.console.spinner("auditing project files"):
            report = AuditEngine(root).run(
                categories, use_external=use_external, has_git=ctx.rt.info().is_git
            )
    except ValueError as exc:
        raise CommandError(str(exc), hint="categories: " + ", ".join(CATEGORIES)) from exc
    extra = _plugin_findings(ctx, root)
    if extra:
        wanted = set(categories) if categories else set(CATEGORIES)
        report.findings.extend(f for f in extra if f.category in wanted)
        report.tools_run.extend(sorted({f.tool for f in extra}))
    ctx.rt.db.kv_set("audit", str(root), report.to_dict())
    return report


def _parse_categories(ctx: CommandContext) -> list[str] | None:
    raw = ctx.flag("category")
    if raw in (None, True):
        return None
    return [c.strip() for c in str(raw).split(",") if c.strip()]


def _show_report(ctx: CommandContext, report: AuditReport, min_severity: str, limit: int, title: str) -> None:
    c = ctx.console
    findings = report.filtered(min_severity)
    counts = report.counts()
    c.heading(title, f"{len(report.findings)} finding(s) in {report.duration:.1f}s")
    c.line(
        "  ".join(c.p.paint(f"{k} {v}", SEVERITY_TOKEN[k]) for k, v in counts.items() if v)
        or c.p.paint("no findings", "ok")
    )
    for f in findings[:limit]:
        tag = c.p.paint(f"{f.severity.upper():8}", SEVERITY_TOKEN[f.severity])
        verified = "" if f.verified else c.p.paint("  [unverified]", "muted")
        c.line(f"{tag} {f.id}  {f.location}{verified}")
        c.line(f"         {f.rule}: {f.evidence[:140]}")
    if len(findings) > limit:
        c.dim(f"... {len(findings) - limit} more; use --limit, --severity or --out report.md")
    c.dim(
        "tools: "
        + (", ".join(report.tools_run) or "none")
        + (
            "   skipped: " + ", ".join(f"{k} ({v})" for k, v in report.tools_skipped.items())
            if report.tools_skipped
            else ""
        )
    )
    if report.tools_skipped:
        c.dim(
            "External analyzers add depth when installed (pip install ruff bandit pip-audit; semgrep). Native checks always run and are heuristic."
        )


def _emit_report(ctx: CommandContext, report: AuditReport, label: str) -> None:
    c = ctx.console
    target = ctx.flag("out")
    if target in (None, True) and not ctx.flag("json") and not ctx.flag("md"):
        return
    fmt = "json" if ctx.flag("json") else "md"
    body = report.to_json() if fmt == "json" else report.to_markdown()
    path = (
        Path(str(target)).expanduser()
        if target not in (None, True)
        else ctx.rt.home.reports_dir / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.{fmt}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, redact(body))
    c.ok(f"report written to {path}")


def _find_stored(ctx: CommandContext, ident: str) -> dict | None:
    data = ctx.rt.db.kv_get("audit", str(ctx.project()))
    if not data:
        return None
    wanted = ident.upper() if ident.upper().startswith("WFC-") else f"WFC-{ident.lower()}"
    return next((f for f in data["findings"] if f["id"].upper() == wanted.upper()), None)


def cmd_audit(ctx: CommandContext) -> None:
    c = ctx.console
    sub = ctx.sub
    if sub in ("show", "fix"):
        ident = ctx.need(2, f"audit {sub} <finding-id>")[1]
        f = _find_stored(ctx, ident)
        if f is None:
            raise CommandError(
                f"finding {ident} not in the last audit",
                hint="run @audit first; ids change when the evidence changes",
            )
        if sub == "show":
            c.kv(
                [
                    ("id", f["id"]),
                    ("severity", f["severity"]),
                    ("category", f["category"]),
                    ("rule", f["rule"]),
                    ("location", f"{f['file']}:{f['line']}" if f["line"] else str(f["file"])),
                    ("confidence", f["confidence"] + (", verified" if f["verified"] else ", unverified")),
                    ("tool", f["tool"]),
                    ("evidence", f["evidence"]),
                    ("why it matters", f["explanation"]),
                    ("suggested fix", f["fix"]),
                ],
                title="finding",
            )
            return
        agent_task(
            ctx,
            f"Fix audit finding {f['id']} ({f['severity']} {f['rule']}) in {f['file']}:{f['line']}.\nEvidence: {f['evidence']}\n"
            f"Explanation: {f['explanation']}\nSuggested fix: {f['fix']}\n\nFirst read the code and confirm the finding is real (it may be a false positive; "
            "if so say why and change nothing). Make the smallest correct change, add or update a test if a test framework exists, run the tests, then `finish` "
            "with what you verified.",
        )
        return
    if sub == "last":
        data = ctx.rt.db.kv_get("audit", str(ctx.project()))
        if not data:
            raise CommandError("no audit has been run for this project yet")
        c.info(
            f"last audit: {time.strftime('%Y-%m-%d %H:%M', time.localtime(data['started']))}, counts {data['counts']}"
        )
        for f in data["findings"][: ctx.int_flag("limit", 30)]:
            c.line(f"  {f['severity']:8} {f['id']}  {f['file']}:{f['line']}  {f['rule']}")
        return
    sev = str(ctx.flag("severity", "info"))
    if sev not in SEVERITIES:
        raise CommandError(f"--severity must be one of {', '.join(SEVERITIES)}")
    report = _run_audit(
        ctx,
        _parse_categories(ctx) or ([ctx.positional[0]] if sub in CATEGORIES else None),
        use_external=not ctx.flag("no-external"),
    )
    _show_report(ctx, report, sev, ctx.int_flag("limit", 25), "audit")
    _emit_report(ctx, report, "audit")
    if report.findings:
        c.dim("@audit show <id> explains a finding; @audit fix <id> asks the agent to fix and verify it")


def cmd_security(ctx: CommandContext) -> None:
    categories = ["security", "configuration", "dependencies"]
    report = _run_audit(ctx, categories, use_external=not ctx.flag("no-external"))
    _show_report(ctx, report, str(ctx.flag("severity", "info")), ctx.int_flag("limit", 25), "security audit")
    _emit_report(ctx, report, "security")
    ctx.console.dim(
        "Static analysis finds patterns, not proof of exploitability. Verified findings are confirmed by tool or by exact match; unverified ones need review."
    )


def _manifest_deps(root: Path) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for path in sorted(root.glob("requirements*.txt")):
        for line in (read_text_safely(path) or "").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith(("-", "git+", "http")):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$", line)
            if m:
                spec = m.group(3).strip()
                rows.append(
                    (
                        path.name,
                        m.group(1),
                        spec or "unpinned",
                        "pinned" if spec.startswith("==") else "range" if spec else "unpinned",
                    )
                )
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        project = data.get("project", {})
        groups = {"pyproject": project.get("dependencies", [])}
        for name, items in (project.get("optional-dependencies") or {}).items():
            groups[f"pyproject[{name}]"] = items
        for group, items in groups.items():
            for item in items:
                m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$", item)
                if m:
                    spec = m.group(3).strip()
                    rows.append(
                        (
                            group,
                            m.group(1),
                            spec or "unpinned",
                            "pinned" if spec.startswith("==") else "range" if spec else "unpinned",
                        )
                    )
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name, spec in (data.get(section) or {}).items():
                kind = (
                    "pinned"
                    if re.match(r"^\d", str(spec))
                    else "range"
                    if re.match(r"^[\^~<>=]", str(spec))
                    else "other"
                )
                rows.append((f"package.json:{section}", name, str(spec), kind))
    return rows


def cmd_deps(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    rows = _manifest_deps(root)
    locks = [
        n
        for n in (
            "poetry.lock",
            "uv.lock",
            "Pipfile.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "Cargo.lock",
            "go.sum",
            "composer.lock",
        )
        if (root / n).exists()
    ]
    if not rows:
        c.dim("no dependency manifest found (requirements*.txt, pyproject.toml, package.json)")
    else:
        c.heading("dependencies", f"{len(rows)} declared; lockfiles: {', '.join(locks) or 'none'}")
        c.table(
            ["source", "package", "constraint", "pinning"],
            [list(r) for r in rows[: ctx.int_flag("limit", 60)]],
        )
        unpinned = sum(1 for r in rows if r[3] == "unpinned")
        if unpinned:
            c.warn(f"{unpinned} dependency declaration(s) have no version constraint")
        if not locks:
            c.warn("no lockfile: installs are not reproducible")
    report = _run_audit(ctx, ["dependencies"], use_external=not ctx.flag("no-external"))
    for f in report.findings[:20]:
        c.line(f"  {f.severity:8} {f.location}  {f.rule}: {f.evidence[:110]}")
    if ctx.flag("outdated"):
        if (root / "package.json").is_file() and shutil.which("npm"):
            result = run_user_command(ctx, ["npm", "outdated"])
        elif shutil.which("pip"):
            result = run_user_command(ctx, ["pip", "list", "--outdated"])
        else:
            raise CommandError("neither npm nor pip is available to check for updates")
        c.line(result.output or "everything is up to date")
        c.dim("The outdated check contacts the package registry through your own network.")
    else:
        c.dim(
            "--outdated queries the package registry for newer versions (needs network). Vulnerability data needs pip-audit or npm audit."
        )


def cmd_search(ctx: CommandContext) -> None:
    c = ctx.console
    rt = ctx.rt
    assert rt.files is not None
    sub = ctx.sub
    if sub == "files":
        pattern = ctx.need(2, "search files <glob>")[1]
        c.line(rt.files.find_files(pattern, str(ctx.flag("path", "."))))
        return
    if sub == "symbol":
        name = ctx.need(2, "search symbol <name>")[1]
        c.line(rt.files.find_symbol(name, str(ctx.flag("path", "."))))
        return
    if not ctx.positional:
        raise CommandError(
            "usage: @search <text> [--regex] [--glob '*.py'] [--path dir] [--ignore-case] [--limit n]"
        )
    pattern = " ".join(ctx.positional)
    c.line(
        rt.files.search(
            pattern,
            str(ctx.flag("path", ".")),
            glob=None if ctx.flag("glob") in (None, True) else str(ctx.flag("glob")),
            regex=bool(ctx.flag("regex")),
            ignore_case=bool(ctx.flag("ignore-case") or ctx.flag("i")),
            limit=ctx.int_flag("limit", 80),
        )
    )


def _entrypoints(root: Path) -> list[str]:
    found = []
    for path in iter_files(root, {".py"}):
        text = read_text_safely(path, 400_000) or ""
        if '__name__ == "__main__"' in text or "__name__ == '__main__'" in text:
            found.append(rel(root, path))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        for m in re.finditer(
            r"^\s*([\w\-]+)\s*=\s*\"([\w.]+:[\w]+)\"",
            pyproject.read_text(encoding="utf-8", errors="replace"),
            re.M,
        ):
            found.append(f"script {m.group(1)} -> {m.group(2)}")
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            for key in ("main", "bin"):
                if data.get(key):
                    found.append(f"package.json {key}: {data[key]}")
        except (ValueError, OSError):
            pass
    return found[:20]


def cmd_architecture(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    info = ctx.rt.info()
    langs: Counter[str] = Counter()
    top: Counter[str] = Counter()
    routes: list[str] = []
    tables: set[str] = set()
    total = 0
    for path in iter_files(root):
        total += 1
        langs[path.suffix.lower() or "(none)"] += 1
        parts = path.relative_to(root).parts
        top[parts[0] if len(parts) > 1 else "(root)"] += 1
        if path.suffix.lower() in (".py", ".js", ".ts", ".php", ".jsx", ".tsx"):
            text = read_text_safely(path, 400_000) or ""
            for m in ROUTE_RX.finditer(text):
                routes.append(f"{m.group(1)}  ({rel(root, path)})")
            tables.update(MODEL_RX.findall(text))
        if path.suffix.lower() in (".sql", ".py", ".js", ".ts", ".php"):
            tables.update(TABLE_RX.findall(read_text_safely(path, 400_000) or ""))
    c.heading("architecture", f"{total} files scanned")
    c.kv(
        info_lines(info) + [("top-level layout", ", ".join(f"{k}/ {v}" for k, v in top.most_common(10)))],
        title="project",
    )
    c.table(["extension", "files"], [[k, str(v)] for k, v in langs.most_common(8)])
    entries = _entrypoints(root)
    if entries:
        c.line(c.p.paint("entry points", "accent"))
        c.lines(f"  {e}" for e in entries)
    graph = import_graph(root)
    if graph:
        fan_in: Counter[str] = Counter()
        for deps in graph.values():
            fan_in.update(deps)
        c.line(
            c.p.paint(
                f"python import graph: {len(graph)} modules, {sum(len(v) for v in graph.values())} internal edges",
                "accent",
            )
        )
        c.table(
            ["most depended-on module", "imported by", "imports"],
            [[m, str(n), str(len(graph.get(m, ())))] for m, n in fan_in.most_common(8)],
        )
        cycles = find_cycles(graph)
        if cycles:
            c.warn(f"{len(cycles)} import cycle(s):")
            for cyc in cycles[:5]:
                c.line("  " + " -> ".join(cyc[:6]) + (" ..." if len(cyc) > 6 else ""))
        else:
            c.ok("no import cycles")
    if routes:
        c.line(c.p.paint(f"routes ({len(routes)})", "accent"))
        c.lines(f"  {r}" for r in routes[:25])
    if tables:
        c.line(c.p.paint("data models / tables: ", "accent") + ", ".join(sorted(tables)[:30]))
    c.dim(
        "Derived from static scans of imports, decorators and CREATE TABLE statements; dynamic wiring is not visible."
    )
    if ctx.flag("explain"):
        agent_task(
            ctx,
            "Using the project files, write a concise architecture overview: components, data flow, boundaries, and the riskiest coupling. Cite files.",
            mode="chat",
        )


def _test_argv(ctx: CommandContext) -> list[str]:
    cmd = ctx.flag("cmd")
    info = ctx.rt.info()
    if cmd not in (None, True):
        import shlex

        argv = shlex.split(str(cmd))
    elif info.test_commands:
        argv = list(info.test_commands[0])
    else:
        raise CommandError(
            "no test command detected for this project",
            hint="pass one: @test --cmd 'pytest -q', or add tests/config so detection can find them",
        )
    framework = " ".join(argv)
    paths = ctx.positional
    keyword = ctx.flag("keyword")
    if "pytest" in framework:
        if ctx.flag("failed"):
            argv.append("--lf")
        if keyword not in (None, True):
            argv += ["-k", str(keyword)]
    elif keyword not in (None, True):
        if "jest" in framework or "vitest" in framework or "npm" in framework:
            argv += ["-t", str(keyword)] if "npm" not in framework else ["--", "-t", str(keyword)]
        elif framework.startswith("go test"):
            argv += ["-run", str(keyword)]
        else:
            raise CommandError(
                "--keyword filtering is not known for this test command; pass --cmd with the filter included"
            )
    return argv + paths


def _run_tests(ctx: CommandContext, argv: list[str]):
    c = ctx.console
    rt = ctx.rt
    timeout = (
        float(ctx.flag("timeout", rt.settings.permissions.command_timeout_seconds))
        if ctx.flag("timeout") not in (None, True)
        else None
    )
    with c.spinner("running tests: " + " ".join(argv)):
        result = run_user_command(ctx, argv, timeout=timeout)
    summary = parse_test_summary(result.output, result.exit_code)
    if rt.ctx is not None:
        rt.ctx.extra["last_test"] = {
            "command": " ".join(argv),
            "ok": result.ok,
            "summary": summary.headline,
            "failures": summary.failures,
        }
    return result, summary


def cmd_test(ctx: CommandContext) -> None:
    c = ctx.console
    argv = _test_argv(ctx)
    result, summary = _run_tests(ctx, argv)
    if result.timed_out:
        c.warn(f"timed out after {result.duration:.0f}s; use --timeout or run a narrower selection")
    elif result.cancelled:
        c.warn("cancelled")
    (c.ok if result.ok else c.warn)(f"{summary.headline}  ({result.duration:.1f}s)")
    for failure in summary.failures[:15]:
        c.line(f"  FAILED {failure}")
    if not result.ok or ctx.flag("output"):
        c.dim("--- captured output (tail) ---")
        c.line("\n".join(result.output.splitlines()[-40:]))
    if not result.ok:
        c.dim(
            "@debug has the agent diagnose and fix the failure; @test --failed reruns only what failed (pytest)"
        )
    current = ctx.rt.ledger.current() if ctx.rt.ledger else None
    if current is not None:
        ctx.rt.ledger.record_validation(current.id, "tests: " + " ".join(argv), result.ok, summary.headline)
        c.dim(f"result recorded as validation evidence for task {current.ordinal}")


def cmd_debug(ctx: CommandContext) -> None:
    c = ctx.console
    description = " ".join(ctx.positional)
    info = ctx.rt.info()
    evidence = ""
    if info.test_commands or ctx.flag("cmd") not in (None, True):
        argv = _test_argv(ctx.__class__(**{**ctx.__dict__, "positional": []}))
        result, summary = _run_tests(ctx, argv)
        if result.ok and not description:
            c.ok(
                f"tests pass ({summary.headline}); nothing to debug. Describe the problem: @debug <what is wrong>"
            )
            return
        if not result.ok:
            c.warn(f"tests fail: {summary.headline}")
            evidence = f"Test command: {' '.join(argv)}\nSummary: {summary.headline}\nFailing: {', '.join(summary.failures[:15]) or 'unparsed'}\n\nOutput (tail):\n{_clip(chr(10).join(result.output.splitlines()[-120:]), 9000)}"
    elif not description:
        raise CommandError(
            "no test command detected and no problem description given",
            hint="@debug <error message or symptom>",
        )
    prompt = "Debug and fix the problem below. Reproduce first, find the root cause (not just the symptom), change only what is needed, then rerun the tests and report the result with `finish`. Do not weaken or delete tests.\n"
    if description:
        prompt += f"\nUser report: {description}\n"
    if evidence:
        prompt += "\n" + evidence
    agent_task(ctx, prompt)


def _target(ctx: CommandContext, usage: str) -> tuple[str, str]:
    if not ctx.positional:
        raise CommandError(f"usage: {usage}")
    first = ctx.positional[0]
    try:
        real = ctx.rt.guard.resolve(first)
    except PathSecurityError:
        return "text", " ".join(ctx.positional)
    if real.exists():
        return "path", ctx.rt.guard.relative(real)
    return "text", " ".join(ctx.positional)


def cmd_explain(ctx: CommandContext) -> None:
    if not ctx.positional:
        agent_task(
            ctx,
            "Explain this project to a new engineer: purpose, structure, how to run and test it, key modules and how data flows. Cite the files you read.",
            mode="chat",
        )
        return
    kind, target = _target(ctx, "@explain <file|symbol|question>")
    what = f"the file `{target}`" if kind == "path" else f"`{target}`"
    agent_task(
        ctx,
        f"Explain {what}. Read the code first, cite file and line, say what it does, why, and any risks or surprising behaviour. Do not change files.",
        mode="chat",
    )


def _diff_text(ctx: CommandContext, *, staged: bool = False, rev: str | None = None) -> str:
    git = _git(ctx)
    if git is None:
        raise CommandError(
            "this project is not a git repository", hint="review a specific file with @review <path>"
        )
    text = git.diff(staged=staged, rev=rev)
    if not text.strip() and rev is None and not staged:
        text = git.diff(staged=True)
    untracked = [e.path for e in git.status() if e.untracked] if rev is None else []
    if untracked:
        text += "\n(untracked files not shown in diff: " + ", ".join(untracked[:20]) + ")"
    return text


def cmd_review(ctx: CommandContext) -> None:
    if ctx.positional and _target(ctx, "@review [path]")[0] == "path":
        _, target = _target(ctx, "@review [path]")
        agent_task(
            ctx,
            f"Review `{target}` as a senior reviewer. Read it and the code that calls it. List concrete defects with file:line, ordered by severity, each with a fix. Say explicitly if you found nothing in a category. Do not change files.",
            mode="chat",
        )
        return
    diff = _diff_text(ctx, staged=bool(ctx.flag("staged")))
    if not diff.strip():
        ctx.console.dim("no changes to review. Pass a path: @review <file>")
        return
    agent_task(
        ctx,
        f"Review these uncommitted changes as a senior reviewer. Check correctness, edge cases, security, tests and style consistency. Open the surrounding files when the diff lacks context. Findings by severity with file:line and a fix.\n\n```diff\n{_clip(diff)}\n```",
        mode="chat",
    )


def cmd_explain_diff(ctx: CommandContext) -> None:
    rev = ctx.positional[0] if ctx.positional else None
    git = _git(ctx)
    if git is None:
        raise CommandError("this project is not a git repository")
    text = git.diff(rev=rev) if rev else _diff_text(ctx, staged=bool(ctx.flag("staged")))
    if not text.strip():
        ctx.console.dim("the diff is empty")
        return
    agent_task(
        ctx,
        f"Explain this diff in plain language: what changed, why it likely changed, what could break, and what should be tested.\n\n```diff\n{_clip(text)}\n```",
        mode="chat",
    )


def cmd_refactor(ctx: CommandContext) -> None:
    if len(ctx.positional) < 2:
        raise CommandError("usage: @refactor <path> <what to improve>")
    kind, target = _target(ctx, "@refactor <path> <goal>")
    if kind != "path":
        raise CommandError(f"'{ctx.positional[0]}' is not a file or directory in this project")
    goal = " ".join(ctx.positional[1:])
    agent_task(
        ctx,
        f"Refactor `{target}`: {goal}\nBehaviour must not change. Run the existing tests first to get a baseline, refactor in small steps, rerun the tests after each step, and stop if they regress. Use @checkpoint-style discipline: do not touch unrelated files.",
    )


def cmd_docs(ctx: CommandContext) -> None:
    target = ctx.positional[0] if ctx.positional else "the project"
    agent_task(
        ctx,
        f"Write or update documentation for {target}. Read the real code; document only behaviour you verified (usage, configuration, commands, limitations). Keep existing docs accurate; do not invent features. Run any documented commands you can to confirm they work.",
    )


def cmd_generate_tests(ctx: CommandContext) -> None:
    kind, target = _target(ctx, "@generate-tests <path|symbol>")
    agent_task(
        ctx,
        f"Generate tests for `{target}` using the project's existing test framework and conventions. Cover normal paths, edge cases and failure paths. Run them with `run_tests`; a test that fails because of a real bug must be reported, not weakened. Finish only when the new tests pass or the failures are explained.",
    )


def _run_configured(ctx: CommandContext, commands: list[list[str]], label: str, missing: str) -> None:
    c = ctx.console
    if not commands:
        raise CommandError(f"no {label} command detected for this project", hint=missing)
    argv = list(commands[0])
    if ctx.flag("cmd") not in (None, True):
        import shlex

        argv = shlex.split(str(ctx.flag("cmd")))
    argv += ctx.positional
    with c.spinner(f"{label}: " + " ".join(argv)):
        result = run_user_command(ctx, argv)
    c.line("\n".join(result.output.splitlines()[-60:]))
    (c.ok if result.ok else c.warn)(
        f"{label} finished with exit code {result.exit_code} in {result.duration:.1f}s"
    )
    return result


def cmd_format(ctx: CommandContext) -> None:
    info = ctx.rt.info()
    git = _git(ctx)
    before = git.dirty_paths() if git else set()
    argv_check = ctx.flag("check")
    commands = [
        list(c) + (["--check"] if argv_check and c[0] in ("ruff", "black") else [])
        for c in info.format_commands
    ]
    _run_configured(
        ctx,
        commands,
        "format",
        "install a formatter (ruff format, black, prettier) and add it to the project config, or pass --cmd",
    )
    if git and not argv_check:
        changed = sorted(git.dirty_paths() - before)
        ctx.console.dim(
            f"{len(changed)} file(s) newly modified by the formatter"
            + (": " + ", ".join(changed[:10]) if changed else "")
            + "; review with @diff"
        )


def cmd_types(ctx: CommandContext) -> None:
    result = _run_configured(
        ctx,
        ctx.rt.info().type_commands,
        "type check",
        "install and configure a type checker (mypy, pyright, tsc) or pass --cmd",
    )
    errors = len(re.findall(r"\berror\b", result.output, re.I))
    ctx.console.dim(f"{errors} line(s) mention an error" if errors else "no error lines in the output")


def cmd_deadcode(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    with c.spinner("looking for unused code"):
        findings = find_dead_code(root)
    c.heading("dead code candidates", f"{len(findings)} found")
    rows = [
        [f.location, f.rule.removeprefix("dead-code."), f.evidence[:90], "yes" if f.verified else "check"]
        for f in findings[: ctx.int_flag("limit", 60)]
    ]
    if rows:
        c.table(["location", "kind", "detail", "certain"], rows)
    else:
        c.ok("no unused imports or unreferenced top-level definitions found")
    if shutil.which("ruff"):
        result = run_user_command(
            ctx, ["ruff", "check", "--select", "F401,F841", "--output-format", "concise", "."], confirm=False
        )
        lines = [
            ln
            for ln in result.output.splitlines()
            if ln.strip() and not ln.startswith(("Found", "All checks"))
        ]
        if lines:
            c.line(c.p.paint(f"ruff F401/F841: {len(lines)}", "accent"))
            c.lines("  " + ln for ln in lines[:25])
    else:
        c.dim("ruff is not installed; its unused-variable check (F841) was skipped")
    c.dim(
        "Static and name-based. Entry points, plugins, reflection and external callers can make live code look unused; never delete on this list alone."
    )


def cmd_impact(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    if not ctx.positional:
        git = _git(ctx)
        if git and git.dirty_paths():
            targets = sorted(p for p in git.dirty_paths() if p.endswith(".py"))[:15]
            c.info("no target given: analysing the changed Python files from git")
        else:
            raise CommandError("usage: @impact <file|module|symbol>")
    else:
        kind, value = _target(ctx, "@impact <file|symbol>")
        targets = [value] if kind == "path" else []
        if kind == "text":
            c.heading("symbol references", value)
            c.line(ctx.rt.files.search(value, ".", regex=False, limit=60))
            return
    graph = import_graph(root)
    reverse: dict[str, set[str]] = {}
    for mod, deps in graph.items():
        for d in deps:
            reverse.setdefault(d, set()).add(mod)
    modules = {m: p for m, p in ((m, None) for m in graph)}
    del modules
    for target in targets:
        path = Path(target)
        mod = ".".join(path.with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        direct = sorted(reverse.get(mod, set()))
        seen = set(direct)
        frontier = list(direct)
        depth2: set[str] = set()
        while frontier:
            current = frontier.pop()
            for nxt in reverse.get(current, ()):
                if nxt not in seen and nxt != mod:
                    seen.add(nxt)
                    depth2.add(nxt)
                    frontier.append(nxt)
        tests = sorted(
            m
            for m in seen
            if m.split(".")[0] in ("tests", "test")
            or ".test_" in "." + m
            or m.rsplit(".", 1)[-1].startswith("test_")
        )
        c.heading(f"impact of {target}", f"{len(direct)} direct, {len(depth2)} indirect dependent module(s)")
        if mod not in graph:
            c.dim("not a Python module in the import graph; falling back to text references")
            c.line(ctx.rt.files.search(path.stem, ".", limit=40))
            continue
        if direct:
            c.line(c.p.paint("imported directly by", "accent"))
            c.lines(f"  {m}" for m in direct[:20])
        if depth2:
            c.line(c.p.paint("transitively affected", "accent"))
            c.lines(f"  {m}" for m in sorted(depth2)[:20])
        c.line(
            c.p.paint("tests that exercise it", "accent")
            + (": " + ", ".join(tests[:10]) if tests else ": none found in the import graph")
        )
        git = _git(ctx)
        if git:
            try:
                authors = git.blame_authors(target)
            except Exception:
                authors = {}
            if authors:
                c.dim(
                    "main authors: "
                    + ", ".join(f"{a} ({n})" for a, n in sorted(authors.items(), key=lambda kv: -kv[1])[:3])
                )
    c.dim("Python imports only; dynamic imports, other languages and runtime coupling are not included.")


def cmd_compare(ctx: CommandContext) -> None:
    c = ctx.console
    if len(ctx.positional) != 2:
        raise CommandError("usage: @compare <fileA> <fileB>  or  @compare <rev> <rev>")
    a, b = ctx.positional
    pa, pb = Path(a), Path(b)
    try:
        ra, rb = ctx.rt.guard.resolve(a), ctx.rt.guard.resolve(b)
    except PathSecurityError:
        ra = rb = None
    if ra is not None and ra.is_file() and rb is not None and rb.is_file():
        import difflib

        left = (read_text_safely(ra) or "").splitlines(keepends=True)
        right = (read_text_safely(rb) or "").splitlines(keepends=True)
        text = "".join(difflib.unified_diff(left, right, pa.as_posix(), pb.as_posix()))
        c.diff(text) if text else c.ok("files are identical")
        return
    git = _git(ctx)
    if git is None:
        raise CommandError("arguments are not two project files, and this is not a git repository")
    try:
        stat = git.run("diff", "--stat", f"{a}..{b}")
        body = git.run("diff", f"{a}..{b}")
    except Exception as exc:
        raise CommandError(f"git could not compare {a} and {b}: {exc}") from exc
    c.heading(f"{a} .. {b}")
    c.line(stat or "no differences")
    if body and ctx.flag("full"):
        c.diff(body)
    elif body:
        c.dim("add --full for the complete diff")


def cmd_benchmark(ctx: CommandContext) -> None:
    c = ctx.console
    rt = ctx.rt
    root = ctx.project()
    import shlex

    if ctx.sub == "history":
        items = load_benchmarks(rt.home.reports_dir)
        c.table(
            ["file", "command", "runs", "median", "stdev"],
            [
                [i["_file"], i["command"][:40], str(i["runs"]), f"{i['median']:.3f}s", f"{i['stdev']:.3f}s"]
                for i in items[-15:]
            ],
        ) if items else c.dim("no saved benchmarks")
        return
    cmd = ctx.flag("cmd")
    if cmd in (None, True):
        if not rt.info().test_commands:
            raise CommandError(
                "no command to benchmark", hint="@benchmark --cmd 'python3 script.py' [--runs 5] [--warmup 1]"
            )
        argv = list(rt.info().test_commands[0])
    else:
        argv = shlex.split(str(cmd))
    decision = rt.policy.evaluate(argv=argv, cwd=root)
    if decision.action == "deny":
        raise CommandError(f"blocked by the permission policy: {decision.reason}")
    runs, warmup = ctx.int_flag("runs", 5), ctx.int_flag("warmup", 1)
    if (
        not ctx.interaction.confirm(
            f"run `{' '.join(argv)}` {runs + warmup} times? ({decision.reason})", False
        )
        and decision.action == "ask"
    ):
        return
    with c.spinner(f"benchmarking ({runs} runs)"):
        result = benchmark_command(
            argv,
            root,
            runs=runs,
            warmup=warmup,
            timeout=float(ctx.flag("timeout", 300)) if ctx.flag("timeout") not in (None, True) else 300.0,
            cancel=rt.cancel_event,
            label=str(ctx.flag("label", "")) if ctx.flag("label") not in (None, True) else "",
        )
    previous = load_benchmarks(rt.home.reports_dir, result.command)
    path = save_benchmark(result, rt.home.reports_dir)
    c.kv(
        [
            ("command", result.command),
            ("runs", str(result.runs)),
            ("median", f"{result.median:.4f}s"),
            ("mean", f"{result.mean:.4f}s"),
            ("stdev", f"{result.stdev:.4f}s"),
            ("best / worst", f"{result.best:.4f}s / {result.worst:.4f}s"),
            ("exit codes", ",".join(str(x) for x in result.exit_codes)),
            ("saved", str(path)),
        ],
        title="benchmark",
    )
    if previous:
        c.line(compare_benchmarks(previous[-1], result.to_dict()))
    c.dim(
        "Timings include process start-up and depend on this machine's load; compare runs from the same environment only."
    )


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    g = "code"
    add(
        "audit",
        cmd_audit,
        "evidence-based project audit",
        "[category] [--category a,b] [--severity s] [--json|--md] [--out file] [--no-external] | show <id> | fix <id> | last",
        g,
        needs_project=True,
    )
    add(
        "security",
        cmd_security,
        "security-focused audit (security, config, dependencies)",
        "[--severity s] [--json] [--out file]",
        g,
        needs_project=True,
    )
    add(
        "debug",
        cmd_debug,
        "run tests, then have the agent diagnose and fix a real failure",
        "[problem description] [--cmd ...]",
        g,
        needs_project=True,
    )
    add(
        "test",
        cmd_test,
        "run the project's tests and summarise failures",
        "[paths] [--cmd ...] [--keyword expr] [--failed] [--timeout s] [--output]",
        g,
        needs_project=True,
    )
    add(
        "explain",
        cmd_explain,
        "explain a file, symbol or the project (read-only)",
        "[file|symbol|question]",
        g,
        needs_project=True,
    )
    add(
        "refactor",
        cmd_refactor,
        "agent refactors a path without changing behaviour",
        "<path> <goal>",
        g,
        needs_project=True,
    )
    add(
        "review",
        cmd_review,
        "review the uncommitted diff or a file (read-only)",
        "[path] [--staged]",
        g,
        needs_project=True,
    )
    add(
        "deps",
        cmd_deps,
        "dependency inventory, pinning and health",
        "[--outdated] [--limit n]",
        g,
        needs_project=True,
    )
    add(
        "search",
        cmd_search,
        "search project files",
        "<text> [--regex] [--glob g] [--path p] [--ignore-case] | files <glob> | symbol <name>",
        g,
        ("grep",),
        needs_project=True,
    )
    add(
        "architecture",
        cmd_architecture,
        "structure, entry points, import graph, routes, tables",
        "[--explain]",
        g,
        ("arch",),
        needs_project=True,
    )
    add(
        "docs",
        cmd_docs,
        "agent writes or updates documentation from the real code",
        "[target]",
        g,
        needs_project=True,
    )
    add(
        "generate-tests",
        cmd_generate_tests,
        "agent generates and runs tests for a target",
        "<path|symbol>",
        g,
        ("gentests",),
        needs_project=True,
    )
    add(
        "benchmark",
        cmd_benchmark,
        "time a command over several runs and compare with history",
        "--cmd <command> [--runs n] [--warmup n] [--label l] | history",
        g,
        needs_project=True,
    )
    add("format", cmd_format, "run the project's formatter", "[--check] [--cmd ...]", g, needs_project=True)
    add("types", cmd_types, "run the project's type checker", "[--cmd ...]", g, needs_project=True)
    add(
        "deadcode",
        cmd_deadcode,
        "find unused imports and unreferenced definitions",
        "[--limit n]",
        g,
        needs_project=True,
    )
    add(
        "explain-diff",
        cmd_explain_diff,
        "explain the current diff or a revision (read-only)",
        "[rev] [--staged]",
        g,
        needs_project=True,
    )
    add("impact", cmd_impact, "what depends on a file or symbol", "<file|symbol>", g, needs_project=True)
    add(
        "compare",
        cmd_compare,
        "compare two files or two git revisions",
        "<a> <b> [--full]",
        g,
        needs_project=True,
    )
