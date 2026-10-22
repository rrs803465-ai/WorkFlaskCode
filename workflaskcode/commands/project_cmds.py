from __future__ import annotations

import difflib
import re
import shlex
import shutil
import time
from pathlib import Path

from workflaskcode.audit.checks import iter_files, rel
from workflaskcode.commands.helpers import agent_task, run_user_command
from workflaskcode.commands.intel import _clip, _git, _run_tests, _test_argv
from workflaskcode.commands.registry import Command, CommandContext, CommandError, CommandRegistry
from workflaskcode.git import GitRepo
from workflaskcode.tasks.ledger import TaskStatus
from workflaskcode.utils.fs import atomic_write_text, read_text_safely
from workflaskcode.utils.redact import redact

MARKER_RX = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s(]*(.*)")
CONVENTIONAL_RX = re.compile(
    r"^(?P<type>feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<desc>.+)$"
)
SECTIONS = [
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("build", "Build"),
    ("ci", "CI"),
    ("chore", "Chores"),
]
VERSION_RX = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][\w.]+)?$")


def _need_git(ctx: CommandContext) -> GitRepo:
    git = _git(ctx)
    if git is None:
        raise CommandError("this project is not a git repository", hint="run `git init` in the project first")
    return git


def _milestones(ctx: CommandContext) -> list[dict]:
    meta = ctx.rt.meta
    return list(meta.read_json("milestones.json", []) or []) if meta else []


def _save_milestones(ctx: CommandContext, items: list[dict]) -> None:
    meta = ctx.rt.meta
    assert meta is not None
    meta.ensure()
    meta.write_json("milestones.json", items)


def cmd_milestones(ctx: CommandContext) -> None:
    c = ctx.console
    ledger = ctx.rt.ledger
    assert ledger is not None
    items = _milestones(ctx)
    sub = ctx.sub or "list"
    if sub == "add":
        title = " ".join(ctx.positional[1:])
        if not title:
            raise CommandError("usage: @milestones add <title> [--due YYYY-MM-DD]")
        due = ctx.flag("due")
        if due not in (None, True) and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)):
            raise CommandError("--due must look like 2026-12-31")
        items.append(
            {
                "id": len(items) + 1,
                "title": title,
                "due": None if due in (None, True) else str(due),
                "done": False,
                "tasks": [],
                "created": time.time(),
            }
        )
        _save_milestones(ctx, items)
        c.ok(f"milestone {len(items)} added")
        return
    if sub in ("done", "reopen", "remove", "link"):
        n = ctx.need(2, f"milestones {sub} <n>")[1]
        target = next((m for m in items if str(m["id"]) == n), None)
        if target is None:
            raise CommandError(f"no milestone {n}")
        if sub == "done":
            open_tasks = [
                t
                for t in (ledger.resolve(r) for r in target["tasks"])
                if t and t.status is not TaskStatus.COMPLETED
            ]
            if open_tasks and not ctx.flag("force"):
                raise CommandError(
                    f"{len(open_tasks)} linked task(s) are not completed", hint="finish them, or use --force"
                )
            target["done"] = True
        elif sub == "reopen":
            target["done"] = False
        elif sub == "remove":
            items.remove(target)
        else:
            refs = ctx.positional[2:]
            if not refs:
                raise CommandError("usage: @milestones link <n> <task-number>...")
            for ref in refs:
                task = ledger.resolve(ref)
                if task is None:
                    raise CommandError(f"unknown task '{ref}'")
                if task.id not in target["tasks"]:
                    target["tasks"].append(task.id)
        _save_milestones(ctx, items)
        c.ok(f"milestone {n}: {sub} applied")
        return
    if not items:
        c.dim(
            "no milestones. @milestones add <title> [--due date]; link tasks with @milestones link <n> <task>"
        )
        return
    rows = []
    for m in items:
        tasks = [ledger.get(t) for t in m["tasks"]]
        tasks = [t for t in tasks if t]
        done = sum(1 for t in tasks if t.status is TaskStatus.COMPLETED and t.verified)
        progress = f"{done}/{len(tasks)} verified" if tasks else "no tasks linked"
        rows.append(
            [str(m["id"]), "done" if m["done"] else "open", m["title"][:50], m.get("due") or "-", progress]
        )
    c.table(["#", "state", "milestone", "due", "progress"], rows)


def _scan_markers(root: Path, limit: int = 500) -> list[tuple[str, int, str, str]]:
    found = []
    for path in iter_files(root):
        text = read_text_safely(path, 400_000)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            m = MARKER_RX.search(line)
            if m and any(tok in line for tok in ("#", "//", "/*", "--", "<!--", "*")):
                found.append((rel(root, path), number, m.group(1), m.group(2).strip(" */>-")[:120]))
                if len(found) >= limit:
                    return found
    return found


def cmd_todo(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    if ctx.sub == "add":
        text = " ".join(ctx.positional[1:])
        if not text:
            raise CommandError("usage: @todo add <text>")
        c.ok(f"task {ctx.rt.ledger.add(text).ordinal} added to the task ledger")
        return
    if ctx.sub == "import":
        markers = [m for m in _scan_markers(root) if m[2] in ("TODO", "FIXME")]
        existing = {t.description for t in ctx.rt.ledger.list()}
        added = 0
        for file, line, kind, text in markers[: ctx.int_flag("limit", 20)]:
            description = f"{kind} {file}:{line} {text}".strip()
            if description not in existing:
                ctx.rt.ledger.add(description)
                added += 1
        c.ok(f"{added} marker(s) imported as tasks")
        return
    markers = _scan_markers(root)
    open_tasks = [
        t for t in ctx.rt.ledger.list() if t.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
    ]
    if open_tasks:
        c.line(c.p.paint("open tasks", "accent"))
        c.task_list(open_tasks)
    c.line(c.p.paint(f"code markers ({len(markers)})", "accent"))
    kinds: dict[str, int] = {}
    for _, _, kind, _ in markers:
        kinds[kind] = kinds.get(kind, 0) + 1
    if markers:
        c.table(
            ["location", "kind", "text"],
            [[f"{f}:{n}", k, t] for f, n, k, t in markers[: ctx.int_flag("limit", 40)]],
        )
        c.dim(
            ", ".join(f"{k} {v}" for k, v in kinds.items())
            + "  |  @todo import turns TODO/FIXME markers into tasks"
        )
    else:
        c.dim("no TODO/FIXME markers found")


def _project_version(root: Path) -> tuple[str | None, str | None]:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        m = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8", errors="replace"), re.M
        )
        if m:
            return m.group(1), "pyproject.toml"
    package_json = root / "package.json"
    if package_json.is_file():
        m = re.search(
            r'"version"\s*:\s*"([^"]+)"', package_json.read_text(encoding="utf-8", errors="replace")
        )
        if m:
            return m.group(1), "package.json"
    return None, None


def cmd_release(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    git = _need_git(ctx)
    version, source = _project_version(root)
    checks: list[tuple[str, bool, str]] = []
    dirty = git.dirty_paths()
    checks.append(
        ("working tree clean", not dirty, f"{len(dirty)} uncommitted path(s)" if dirty else "clean")
    )
    checks.append(("on a branch with commits", git.head() is not None, git.current_branch() or "detached"))
    tags = git.tags()
    last_tag = tags[0] if tags else None
    if version:
        checks.append((f"version declared ({source})", True, version))
        checks.append(
            (
                "tag not already used",
                f"v{version}" not in tags and version not in tags,
                f"existing tags: {', '.join(tags[:3]) or 'none'}",
            )
        )
    else:
        checks.append(("version declared", False, "no version in pyproject.toml or package.json"))
    changelog = next(
        (root / n for n in ("CHANGELOG.md", "CHANGELOG", "HISTORY.md") if (root / n).is_file()), None
    )
    if changelog and version:
        has_entry = version in (read_text_safely(changelog) or "")
        checks.append(("changelog mentions this version", has_entry, changelog.name))
    elif not changelog:
        checks.append(("changelog present", False, "none found; @changelog --write can create one"))
    if ctx.flag("skip-tests"):
        checks.append(("tests pass", False, "skipped by --skip-tests (counts as not verified)"))
    else:
        try:
            result, summary = _run_tests(
                ctx, _test_argv(ctx.__class__(**{**ctx.__dict__, "positional": [], "flags": {}}))
            )
            checks.append(("tests pass", result.ok, summary.headline))
        except CommandError as exc:
            checks.append(("tests pass", False, str(exc)))
    c.heading("release checks", f"since {last_tag or 'the first commit'}")
    for name, ok, detail in checks:
        (c.ok if ok else c.warn)(f"{name}: {detail}")
    failed = [n for n, ok, _ in checks if not ok]
    commits = git.log(200, "%h %s", f"{last_tag}..HEAD" if last_tag else None)
    c.dim(f"{len(commits)} commit(s) since the last tag")
    tag = ctx.flag("tag")
    if tag in (None, True):
        c.dim(
            "@release --tag v1.2.3 creates a local annotated tag after these checks. Nothing is pushed; publish with `git push origin <tag>` yourself."
        )
        return
    tag = str(tag)
    if not VERSION_RX.match(tag):
        raise CommandError("tag must look like v1.2.3")
    if tag in tags:
        raise CommandError(f"tag {tag} already exists")
    if failed and not ctx.flag("force"):
        raise CommandError(
            f"{len(failed)} check(s) did not pass: {', '.join(failed)}",
            hint="fix them, or accept the risk with --force",
        )
    if not ctx.interaction.confirm(
        f"create local annotated tag {tag} at {git.head()[:8]}? It will not be pushed.", False
    ):
        return
    git.run("tag", "-a", tag, "-m", f"Release {tag}")
    c.ok(f"tag {tag} created locally. Push when ready: git push origin {tag}")


def _base_ref(git: GitRepo, requested: str | None) -> str:
    if requested:
        return requested
    for candidate in ("origin/main", "origin/master", "main", "master"):
        if git.run("rev-parse", "--verify", "--quiet", candidate, check=False).strip():
            return candidate
    raise CommandError("could not find a base branch (main/master)", hint="pass one: @pr --base <branch>")


def _commit_subjects(git: GitRepo, rng: str) -> list[str]:
    return [ln for ln in git.run("log", "--no-merges", "--format=%s", rng, check=False).splitlines() if ln]


def cmd_pr(ctx: CommandContext) -> None:
    c = ctx.console
    git = _need_git(ctx)
    base = _base_ref(git, None if ctx.flag("base") in (None, True) else str(ctx.flag("base")))
    branch = git.current_branch() or "HEAD"
    rng = f"{base}..HEAD"
    subjects = _commit_subjects(git, rng)
    if not subjects:
        raise CommandError(f"no commits on {branch} that are not already in {base}")
    stat = git.run("diff", "--stat", rng, check=False).strip()
    files = git.run("diff", "--name-only", rng, check=False).split()
    tests_touched = [f for f in files if re.search(r"(^|/)(tests?|__tests__)/|test_|\.test\.|\.spec\.", f)]
    title = (
        ctx.flag("title")
        if ctx.flag("title") not in (None, True)
        else (subjects[0] if len(subjects) == 1 else f"{branch.replace('-', ' ').replace('/', ': ', 1)}")
    )
    body = [
        "## Summary",
        "",
        *[f"- {s}" for s in subjects[:30]],
        "",
        "## Changes",
        "",
        "```",
        stat,
        "```",
        "",
        "## Testing",
        "",
    ]
    last = ctx.rt.ctx.extra.get("last_test") if ctx.rt.ctx else None
    body.append(
        f"- Last test run in this session: {'PASS' if last['ok'] else 'FAIL'} {last['summary']}"
        if last
        else "- Tests not run in this session; add the results here before requesting review."
    )
    body.append(
        f"- Test files changed: {', '.join(tests_touched[:10])}"
        if tests_touched
        else "- No test files changed in this branch."
    )
    body += [
        "",
        "## Notes",
        "",
        "- Review the diff; this description was generated from commit subjects and diff statistics only.",
    ]
    text = redact("\n".join(body))
    c.heading(str(title), f"{branch} -> {base}")
    c.markdown(text)
    out = ctx.flag("out")
    if out not in (None, True):
        Path(str(out)).expanduser().write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
        c.ok(f"description written to {out}")
    if ctx.flag("create"):
        if shutil.which("gh") is None:
            raise CommandError(
                "the GitHub CLI (gh) is not installed",
                hint="copy the description above, or install gh from https://cli.github.com",
            )
        remote_has = (
            git.run("ls-remote", "--heads", "origin", branch, check=False).strip() if git.remote_url() else ""
        )
        if not remote_has:
            raise CommandError(
                f"branch {branch} is not on the remote; WorkFlaskCode never pushes for you",
                hint=f"run `git push -u origin {branch}` yourself, then repeat @pr --create",
            )
        if not ctx.interaction.confirm(
            f"create a pull request on GitHub from {branch} into {base.removeprefix('origin/')}?", False
        ):
            return
        result = run_user_command(
            ctx,
            [
                "gh",
                "pr",
                "create",
                "--title",
                str(title),
                "--body",
                text,
                "--base",
                base.removeprefix("origin/"),
            ],
        )
        c.line(result.output.strip())
    elif out in (None, True):
        c.dim(
            "--out file.md saves it; --create opens the PR with the GitHub CLI after confirmation (the branch must already be pushed)"
        )


def _group_commits(subjects: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    breaking: list[str] = []
    for s in subjects:
        m = CONVENTIONAL_RX.match(s)
        if m:
            scope = f"**{m.group('scope')}**: " if m.group("scope") else ""
            groups.setdefault(m.group("type"), []).append(scope + m.group("desc"))
            if m.group("bang"):
                breaking.append(scope + m.group("desc"))
        else:
            groups.setdefault("other", []).append(s)
    if breaking:
        groups["breaking"] = breaking
    return groups


def cmd_changelog(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    git = _need_git(ctx)
    since = ctx.flag("since")
    tags = git.tags()
    ref = str(since) if since not in (None, True) else (tags[0] if tags else None)
    subjects = _commit_subjects(git, f"{ref}..HEAD" if ref else "HEAD")
    if not subjects:
        c.dim(f"no commits since {ref or 'the beginning'}")
        return
    groups = _group_commits(subjects)
    heading = str(ctx.flag("version")) if ctx.flag("version") not in (None, True) else "Unreleased"
    lines = [f"## {heading} - {time.strftime('%Y-%m-%d')}", ""]
    if "breaking" in groups:
        lines += ["### Breaking changes", "", *[f"- {s}" for s in groups["breaking"]], ""]
    for key, title in SECTIONS:
        if key in groups:
            lines += [f"### {title}", "", *[f"- {s}" for s in groups[key]], ""]
    if "other" in groups:
        lines += ["### Other", "", *[f"- {s}" for s in groups["other"]], ""]
    entry = redact("\n".join(lines))
    c.heading("changelog", f"{len(subjects)} commit(s) since {ref or 'the beginning'}")
    c.markdown(entry)
    if not ctx.flag("write"):
        c.dim("--write [--version 1.2.0] inserts this at the top of CHANGELOG.md after showing the diff")
        return
    target = root / "CHANGELOG.md"
    old = read_text_safely(target) or ""
    header = "# Changelog\n\n"
    body = old[len(header) :] if old.startswith(header) else old
    new = header + entry + "\n" + body
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), "a/CHANGELOG.md", "b/CHANGELOG.md"
        )
    )
    c.diff(diff)
    if ctx.interaction.confirm("write CHANGELOG.md?", False):
        atomic_write_text(target, new)
        c.ok("CHANGELOG.md updated")


def cmd_review_commit(ctx: CommandContext) -> None:
    git = _need_git(ctx)
    rev = ctx.positional[0] if ctx.positional else "HEAD"
    if not re.match(r"^[\w./~^@{}\-]+$", rev):
        raise CommandError("invalid revision")
    try:
        message = git.run("show", "--no-patch", "--format=%h %an: %s%n%n%b", rev).strip()
        patch = git.run("show", "--format=", "--stat", "--patch", rev)
    except Exception as exc:
        raise CommandError(f"cannot read revision {rev}: {exc}") from exc
    agent_task(
        ctx,
        f"Review commit {rev}. Check that the message matches the change, look for bugs, missing tests, security problems and unrelated changes. Open surrounding files if needed. Findings by severity with file:line.\n\nCommit: {message}\n\n```diff\n{_clip(patch)}\n```",
        mode="chat",
    )


def cmd_worktree(ctx: CommandContext) -> None:
    c = ctx.console
    git = _need_git(ctx)
    sub = ctx.sub or "list"
    if sub == "list":
        rows = [
            [p, b or "(detached)", "current" if Path(p).resolve() == ctx.project().resolve() else ""]
            for p, b in git.worktrees()
        ]
        c.table(["path", "branch", ""], rows)
        return
    if sub == "add":
        branch = ctx.need(2, "worktree add <branch> [path]")[1]
        path = (
            Path(ctx.positional[2]).expanduser()
            if len(ctx.positional) > 2
            else ctx.project().parent / f"{ctx.project().name}-{branch.replace('/', '-')}"
        )
        path = path.resolve()
        if path.exists():
            raise CommandError(f"{path} already exists")
        git.add_worktree(path, branch)
        c.ok(f"worktree created at {path} on new branch {branch}. Open it with @open {path}")
        return
    if sub == "remove":
        target = Path(ctx.need(2, "worktree remove <path>")[1]).expanduser().resolve()
        known = {Path(p).resolve() for p, _ in git.worktrees()}
        if target not in known or target == ctx.project().resolve():
            raise CommandError("not a removable worktree of this repository")
        if not ctx.interaction.confirm(
            f"remove worktree {target}? Git refuses if it has uncommitted changes.", False
        ):
            return
        git.remove_worktree(target)
        c.ok("worktree removed; the branch is kept")
        return
    raise CommandError("usage: @worktree [list|add <branch> [path]|remove <path>]")


MIGRATION_TOOLS = [
    (
        "alembic",
        ["alembic.ini"],
        {
            "status": ["alembic", "current"],
            "plan": ["alembic", "history", "--verbose"],
            "run": ["alembic", "upgrade", "head"],
        },
    ),
    (
        "django",
        ["manage.py"],
        {
            "status": ["python3", "manage.py", "showmigrations"],
            "plan": ["python3", "manage.py", "migrate", "--plan"],
            "run": ["python3", "manage.py", "migrate"],
        },
    ),
    (
        "prisma",
        ["prisma/schema.prisma"],
        {
            "status": ["npx", "prisma", "migrate", "status"],
            "plan": [
                "npx",
                "prisma",
                "migrate",
                "diff",
                "--from-migrations",
                "prisma/migrations",
                "--to-schema-datamodel",
                "prisma/schema.prisma",
                "--script",
            ],
            "run": ["npx", "prisma", "migrate", "deploy"],
        },
    ),
    (
        "knex",
        ["knexfile.js", "knexfile.ts"],
        {
            "status": ["npx", "knex", "migrate:status"],
            "plan": ["npx", "knex", "migrate:list"],
            "run": ["npx", "knex", "migrate:latest"],
        },
    ),
    (
        "flyway",
        ["flyway.conf"],
        {"status": ["flyway", "info"], "plan": ["flyway", "info"], "run": ["flyway", "migrate"]},
    ),
]


def cmd_migrate(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    detected = next(
        ((n, cmds) for n, marks, cmds in MIGRATION_TOOLS if any((root / m).exists() for m in marks)), None
    )
    sub = ctx.sub or "status"
    sql_dir = next((root / d for d in ("migrations", "db/migrations", "sql") if (root / d).is_dir()), None)
    if sub == "create":
        description = " ".join(ctx.positional[1:])
        if not description:
            raise CommandError("usage: @migrate create <what the migration does>")
        tool = detected[0] if detected else "plain SQL files"
        agent_task(
            ctx,
            f"Create a database migration for: {description}. Tool: {tool}. Follow the project's existing migration naming and style. Include a safe downgrade where the tool supports it. Do NOT run the migration against any database; show how to run it and what it changes.",
        )
        return
    if detected is None:
        c.heading("migrations")
        if sql_dir:
            files = sorted(p.name for p in sql_dir.iterdir() if p.is_file())
            c.info(f"no migration tool detected; {len(files)} file(s) in {rel(root, sql_dir)}")
            c.lines(f"  {f}" for f in files[:30])
            c.dim(
                "Apply them with your database client. WorkFlaskCode will not run raw SQL against a database for you."
            )
        else:
            raise CommandError(
                "no migration setup detected (alembic, django, prisma, knex, flyway, or a migrations/ directory)",
                hint="@migrate create <description> can start one with the agent",
            )
        return
    name, cmds = detected
    if sub not in cmds:
        raise CommandError("usage: @migrate [status|plan|run|create <description>]")
    argv = cmds[sub]
    if shutil.which(argv[0]) is None:
        raise CommandError(
            f"{name} migrations detected but `{argv[0]}` is not installed",
            hint="install it in your project environment first",
        )
    if sub == "run" and not ctx.interaction.confirm(
        f"{name} will apply pending migrations to the database your configuration points at. Take a backup first. Run `{shlex.join(argv)}`?",
        False,
    ):
        return
    result = run_user_command(ctx, argv, confirm=sub != "run")
    ctx.console.line(result.output.strip()[-4000:] or "(no output)")
    (c.ok if result.ok else c.warn)(f"{name} {sub} finished with exit code {result.exit_code}")


def _route_docs(root: Path) -> list[dict]:
    routes = []
    flask = re.compile(r"@\w+\.route\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?")
    fast = re.compile(r"@\w+\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
    express = re.compile(r"\b(?:app|router)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]")
    for path in iter_files(root, {".py", ".js", ".ts", ".mjs", ".php"}):
        text = read_text_safely(path, 400_000)
        if text is None:
            continue
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            doc = ""
            m = flask.search(line)
            if m:
                methods = re.findall(r"[A-Za-z]+", m.group(2) or "GET")
                entries = [(meth.upper(), m.group(1)) for meth in methods]
            else:
                m = fast.search(line) or express.search(line)
                entries = [(m.group(1).upper(), m.group(2))] if m else []
            if not entries:
                continue
            if path.suffix == ".py":
                for follow in lines[number : number + 6]:
                    d = re.search(r'"""(.+?)"""|\'\'\'(.+?)\'\'\'', follow)
                    if d:
                        doc = (d.group(1) or d.group(2)).strip()
                        break
            for method, route in entries:
                routes.append(
                    {"method": method, "path": route, "file": rel(root, path), "line": number, "summary": doc}
                )
    return sorted(routes, key=lambda r: (r["path"], r["method"]))


def cmd_api_docs(ctx: CommandContext) -> None:
    c = ctx.console
    root = ctx.project()
    spec = next(
        (
            root / n
            for n in ("openapi.json", "openapi.yaml", "openapi.yml", "swagger.json")
            if (root / n).is_file()
        ),
        None,
    )
    routes = _route_docs(root)
    if not routes and spec is None:
        raise CommandError(
            "no HTTP routes or OpenAPI file found",
            hint="supported: Flask, FastAPI, Express and Laravel-style route declarations, openapi.json/yaml",
        )
    c.heading("api routes", f"{len(routes)} detected" + (f"; spec file {spec.name}" if spec else ""))
    c.table(
        ["method", "path", "source", "summary"],
        [[r["method"], r["path"], f"{r['file']}:{r['line']}", r["summary"][:50]] for r in routes[:60]],
    )
    if not ctx.flag("write") and not ctx.flag("agent"):
        c.dim(
            "--write creates docs/api.md from these routes; --agent has the model enrich it with parameters and examples from the handlers"
        )
        return
    lines = [
        "# API reference",
        "",
        f"Generated by WorkFlaskCode on {time.strftime('%Y-%m-%d')} from route declarations. Parameters, bodies and responses are not inferred.",
        "",
    ]
    for r in routes:
        lines += [f"## `{r['method']} {r['path']}`", "", f"Handler: `{r['file']}:{r['line']}`", ""] + (
            [r["summary"], ""] if r["summary"] else []
        )
    target = root / "docs" / "api.md"
    old = read_text_safely(target) or ""
    new = "\n".join(lines)
    if old == new:
        c.ok("docs/api.md is already up to date")
    else:
        c.diff(
            "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    "a/docs/api.md",
                    "b/docs/api.md",
                )
            ),
            max_lines=80,
        )
        if ctx.flag("write") and ctx.interaction.confirm("write docs/api.md?", False):
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, new)
            c.ok("docs/api.md written")
    if ctx.flag("agent"):
        agent_task(
            ctx,
            "Improve docs/api.md: for each route read its handler and document parameters, request bodies, response shapes, status codes and auth, with a curl example. Document only what the code does. Keep the route list accurate.",
        )


def register(reg: CommandRegistry) -> None:
    add = lambda *a, **k: reg.register(Command(*a, **k))
    g = "project"
    add(
        "milestones",
        cmd_milestones,
        "milestones linked to tasks, with verified progress",
        "[list|add <title> [--due date]|done n|reopen n|remove n|link n <task>...]",
        g,
        needs_project=True,
    )
    add(
        "todo",
        cmd_todo,
        "open tasks plus TODO/FIXME markers found in code",
        "[add <text>|import]",
        g,
        needs_project=True,
    )
    add(
        "release",
        cmd_release,
        "release readiness checks; optional local tag (never pushes)",
        "[--tag v1.2.3] [--force] [--skip-tests]",
        g,
        needs_project=True,
    )
    add(
        "pr",
        cmd_pr,
        "pull request description from real commits and diff",
        "[--base branch] [--title t] [--out file] [--create]",
        g,
        needs_project=True,
    )
    add(
        "changelog",
        cmd_changelog,
        "changelog entry from conventional commits",
        "[--since tag] [--version v] [--write]",
        g,
        needs_project=True,
    )
    add(
        "review-commit",
        cmd_review_commit,
        "review one commit (read-only)",
        "[rev]",
        g,
        ("rc",),
        needs_project=True,
    )
    add(
        "worktree",
        cmd_worktree,
        "list, add or remove git worktrees",
        "[list|add <branch> [path]|remove <path>]",
        g,
        needs_project=True,
    )
    add(
        "migrate",
        cmd_migrate,
        "database migration status, plan, run (confirmed) or create",
        "[status|plan|run|create <description>]",
        g,
        needs_project=True,
    )
    add(
        "api-docs",
        cmd_api_docs,
        "detect HTTP routes and generate API docs",
        "[--write] [--agent]",
        g,
        needs_project=True,
    )
