from __future__ import annotations

import ast
import math
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from workflaskcode.audit.models import Finding
from workflaskcode.tools.files import IGNORED_DIRS
from workflaskcode.utils.fs import read_text_safely

SOURCE_EXTS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".php",
    ".java",
    ".rb",
    ".sh",
}
WEB_EXTS = {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".php"}
TEXT_EXTS = (
    SOURCE_EXTS
    | WEB_EXTS
    | {
        ".json",
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".cfg",
        ".env",
        ".md",
        ".txt",
        ".css",
        ".scss",
        ".sql",
        ".xml",
        ".conf",
        "",
    }
)
MAX_FILE_BYTES = 1_000_000


def iter_files(root: Path, exts: set[str] | None = None) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for name in sorted(files):
            path = Path(current) / name
            if exts is not None and path.suffix.lower() not in exts:
                continue
            yield path


def rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {c: value.count(c) for c in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "high"),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "critical"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "high"),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "high"),
    ("openai-style-key", re.compile(r"\bsk-(?:or-v1-|ant-)?[A-Za-z0-9_\-]{24,}\b"), "high"),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high"),
    ("stripe-live-key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}\b"), "critical"),
]
GENERIC_SECRET = re.compile(
    r"""(?ix)\b(api[_-]?key|secret|passwd|password|token|auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*["']([^"'\s]{8,})["']"""
)
PLACEHOLDER = re.compile(
    r"(?i)(example|placeholder|changeme|your[_-]|xxx|<.*>|\$\{|%\(|\{\{|dummy|test|fake|sample|redacted|\*{3,})"
)


def _is_test_path(root: Path, path: Path) -> bool:
    parts = path.relative_to(root).parts
    return any(p in ("tests", "test", "fixtures", "__tests__") for p in parts) or path.name.startswith(
        "test_"
    )


def _tone_down(f: Finding) -> Finding:
    f.severity = "low"
    f.confidence = "low"
    f.explanation += (
        " It is in test code, so it is probably a dummy value; confirm it is not a real credential."
    )
    return f


def check_secrets(root: Path) -> Iterator[Finding]:
    for finding in _check_secrets(root):
        yield _tone_down(finding) if finding.file and _is_test_path(root, root / finding.file) else finding


def _check_secrets(root: Path) -> Iterator[Finding]:
    for path in iter_files(root):
        if path.suffix.lower() not in TEXT_EXTS and path.name not in (".env",):
            continue
        name = path.name.lower()
        if name.endswith((".lock", ".min.js", ".map")) or name in (
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
        ):
            continue
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None:
            continue
        r = rel(root, path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 600:
                continue
            for rule, rx, sev in SECRET_PATTERNS:
                m = rx.search(line)
                if m:
                    yield Finding(
                        "security",
                        sev,
                        f"secret/{rule}",
                        r,
                        lineno,
                        _mask(line.strip(), m.group(0)),
                        f"A string matching a {rule.replace('-', ' ')} is committed in the source tree.",
                        "Remove it from the repository, rotate the credential, and load it from the environment or a secret store.",
                        "high",
                        False,
                        "native:secrets",
                    )
            g = GENERIC_SECRET.search(line)
            if g and not PLACEHOLDER.search(g.group(2)) and entropy(g.group(2)) >= 3.0:
                yield Finding(
                    "security",
                    "medium",
                    "secret/hardcoded-credential",
                    r,
                    lineno,
                    _mask(line.strip(), g.group(2)),
                    f"'{g.group(1)}' is assigned a literal value with high entropy.",
                    "Read it from an environment variable or secret manager instead of the source.",
                    "medium",
                    False,
                    "native:secrets",
                )


def _mask(line: str, secret: str) -> str:
    masked = secret[:4] + "…" + secret[-2:] if len(secret) > 8 else "…"
    return line.replace(secret, masked)[:200]


def check_env_files(root: Path, gitignore_text: str) -> Iterator[Finding]:
    for path in iter_files(root):
        if path.name == ".env" or (
            path.name.startswith(".env.") and not path.name.endswith((".example", ".sample", ".template"))
        ):
            if not re.search(r"(^|/)\.env(\.\*)?\s*$|^\.env", gitignore_text, re.M):
                yield Finding(
                    "security",
                    "high",
                    "config/env-not-ignored",
                    rel(root, path),
                    None,
                    f"{path.name} exists and .gitignore does not list it",
                    "Environment files usually hold secrets and are easily committed by accident.",
                    "Add '.env' to .gitignore and commit a .env.example with placeholder values.",
                    "high",
                    True,
                    "native:config",
                )


DANGEROUS_CALLS: dict[str, tuple[str, str, str, str]] = {
    "eval": ("security", "high", "py/eval", "eval() executes arbitrary code from a string."),
    "exec": ("security", "high", "py/exec", "exec() executes arbitrary code from a string."),
    "os.system": (
        "security",
        "medium",
        "py/os-system",
        "os.system() runs through a shell and is injection-prone.",
    ),
    "pickle.loads": (
        "security",
        "high",
        "py/pickle",
        "pickle can execute arbitrary code when loading untrusted data.",
    ),
    "pickle.load": (
        "security",
        "high",
        "py/pickle",
        "pickle can execute arbitrary code when loading untrusted data.",
    ),
    "marshal.loads": (
        "security",
        "high",
        "py/marshal",
        "marshal can execute arbitrary code when loading untrusted data.",
    ),
    "tempfile.mktemp": (
        "security",
        "medium",
        "py/mktemp",
        "mktemp is race-prone; use mkstemp or NamedTemporaryFile.",
    ),
    "yaml.load": (
        "security",
        "medium",
        "py/yaml-load",
        "yaml.load without SafeLoader can construct arbitrary objects.",
    ),
    "hashlib.md5": (
        "security",
        "low",
        "py/weak-hash",
        "MD5 is broken for security purposes (fine for non-security checksums).",
    ),
    "hashlib.sha1": (
        "security",
        "low",
        "py/weak-hash",
        "SHA-1 is weak for security purposes (fine for non-security checksums).",
    ),
    "random.random": (
        "security",
        "info",
        "py/insecure-random",
        "random is not cryptographically secure; use secrets for tokens.",
    ),
}
SHELL_CALLS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _complexity(fn: ast.AST) -> int:
    score = 1
    for node in ast.walk(fn):
        if isinstance(
            node, (ast.If, ast.For, ast.While, ast.AsyncFor, ast.IfExp, ast.With, ast.AsyncWith, ast.Assert)
        ):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.ExceptHandler):
            score += 1
        elif isinstance(node, ast.comprehension):
            score += 1 + len(node.ifs)
    return score


def check_python(root: Path) -> Iterator[Finding]:
    for path in iter_files(root, {".py"}):
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None:
            continue
        r = rel(root, path)
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            yield Finding(
                "code-quality",
                "high",
                "py/syntax-error",
                r,
                exc.lineno,
                str(exc.msg),
                "The file does not parse.",
                "Fix the syntax error.",
                "high",
                True,
                "native:ast",
            )
            continue
        if len(lines) > 1000:
            yield Finding(
                "maintainability",
                "low",
                "py/large-file",
                r,
                None,
                f"{len(lines)} lines",
                "Very large modules are hard to navigate and test.",
                "Split by responsibility.",
                "medium",
                True,
                "native:ast",
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name in DANGEROUS_CALLS:
                    if name == "yaml.load" and any(
                        kw.arg == "Loader" and "Safe" in _dotted(kw.value) for kw in node.keywords
                    ):
                        continue
                    if name in ("eval", "exec") and node.args and isinstance(node.args[0], ast.Constant):
                        continue
                    cat, sev, rule, why = DANGEROUS_CALLS[name]
                    yield Finding(
                        cat,
                        sev,
                        rule,
                        r,
                        node.lineno,
                        lines[node.lineno - 1].strip()[:200],
                        why,
                        "Use a safer alternative or validate the input rigorously.",
                        "medium",
                        False,
                        "native:ast",
                    )
                if name in SHELL_CALLS:
                    for kw in node.keywords:
                        if (
                            kw.arg == "shell"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ):
                            yield Finding(
                                "security",
                                "high",
                                "py/shell-true",
                                r,
                                node.lineno,
                                lines[node.lineno - 1].strip()[:200],
                                "shell=True passes the command through a shell; any interpolated input becomes command injection.",
                                "Pass an argument list and drop shell=True.",
                                "medium",
                                False,
                                "native:ast",
                            )
                for kw in node.keywords:
                    if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                        yield Finding(
                            "security",
                            "high",
                            "py/tls-verify-off",
                            r,
                            node.lineno,
                            lines[node.lineno - 1].strip()[:200],
                            "TLS certificate verification is disabled, enabling man-in-the-middle attacks.",
                            "Remove verify=False or point it at a CA bundle.",
                            "high",
                            False,
                            "native:ast",
                        )
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany")
                    and node.args
                ):
                    first = node.args[0]
                    if (
                        isinstance(first, ast.JoinedStr)
                        or (isinstance(first, ast.BinOp) and isinstance(first.op, (ast.Mod, ast.Add)))
                        or (
                            isinstance(first, ast.Call)
                            and isinstance(first.func, ast.Attribute)
                            and first.func.attr == "format"
                        )
                    ):
                        dynamic = isinstance(first, ast.JoinedStr)
                        yield Finding(
                            "security",
                            "medium" if dynamic else "high",
                            "py/sql-injection",
                            r,
                            node.lineno,
                            lines[node.lineno - 1].strip()[:200],
                            "A SQL statement is built by string formatting instead of bound parameters."
                            + (
                                " If only trusted identifiers (table/column names) are interpolated and values are bound, this is safe."
                                if dynamic
                                else ""
                            ),
                            "Use placeholders (?, %s) and pass values as parameters.",
                            "low" if dynamic else "medium",
                            False,
                            "native:ast",
                        )
            elif isinstance(node, ast.ExceptHandler):
                swallowed = len(node.body) == 1 and isinstance(node.body[0], (ast.Pass, ast.Continue))
                if node.type is None:
                    yield Finding(
                        "error-handling",
                        "medium",
                        "py/bare-except",
                        r,
                        node.lineno,
                        lines[node.lineno - 1].strip(),
                        "A bare except also catches KeyboardInterrupt/SystemExit and hides bugs.",
                        "Catch specific exception types.",
                        "high",
                        True,
                        "native:ast",
                    )
                elif swallowed and _dotted(node.type) in ("Exception", "BaseException"):
                    yield Finding(
                        "error-handling",
                        "medium",
                        "py/swallowed-exception",
                        r,
                        node.lineno,
                        lines[node.lineno - 1].strip(),
                        "A broad exception is caught and silently ignored, hiding failures.",
                        "Handle, log, or narrow the exception.",
                        "medium",
                        True,
                        "native:ast",
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                length = end - node.lineno + 1
                if length > 80:
                    yield Finding(
                        "maintainability",
                        "low",
                        "py/long-function",
                        r,
                        node.lineno,
                        f"{node.name}() is {length} lines",
                        "Long functions are hard to read and test.",
                        "Extract cohesive steps into helpers.",
                        "high",
                        True,
                        "native:ast",
                    )
                score = _complexity(node)
                if score > 15:
                    yield Finding(
                        "code-quality",
                        "medium" if score > 25 else "low",
                        "py/complexity",
                        r,
                        node.lineno,
                        f"{node.name}() cyclomatic complexity ~{score}",
                        "High branching makes the function error-prone.",
                        "Split it or replace conditionals with data/dispatch.",
                        "medium",
                        True,
                        "native:ast",
                    )
                nargs = len(node.args.args) + len(node.args.kwonlyargs)
                if nargs > 7:
                    yield Finding(
                        "maintainability",
                        "low",
                        "py/many-params",
                        r,
                        node.lineno,
                        f"{node.name}() takes {nargs} parameters",
                        "Many parameters signal a missing abstraction.",
                        "Group related values into a dataclass.",
                        "medium",
                        True,
                        "native:ast",
                    )
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        yield Finding(
                            "code-quality",
                            "medium",
                            "py/mutable-default",
                            r,
                            node.lineno,
                            f"{node.name}() has a mutable default argument",
                            "Mutable defaults are shared across calls.",
                            "Default to None and create the object inside.",
                            "high",
                            True,
                            "native:ast",
                        )
                        break
                if isinstance(node, ast.AsyncFunctionDef):
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Call) and _dotted(inner.func) in (
                            "time.sleep",
                            "requests.get",
                            "requests.post",
                            "subprocess.run",
                        ):
                            yield Finding(
                                "performance",
                                "medium",
                                "py/blocking-in-async",
                                r,
                                inner.lineno,
                                lines[inner.lineno - 1].strip()[:200],
                                "A blocking call inside an async function stalls the event loop.",
                                "Use the async equivalent or run it in an executor.",
                                "medium",
                                True,
                                "native:ast",
                            )
                            break


def check_todos(root: Path) -> Iterator[Finding]:
    rx = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]?(.*)")
    hits: list[tuple[str, int, str]] = []
    for path in iter_files(root, SOURCE_EXTS):
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            m = rx.search(line)
            if m and ("#" in line or "//" in line or "/*" in line):
                hits.append((rel(root, path), lineno, f"{m.group(1)}: {m.group(2).strip()[:100]}"))
    if len(hits) >= 5:
        yield Finding(
            "maintainability",
            "info",
            "marker/todo-count",
            None,
            None,
            f"{len(hits)} TODO/FIXME/HACK markers",
            "Unresolved markers indicate unfinished work.",
            "Triage them with @todo.",
            "high",
            True,
            "native:markers",
        )


def check_web(root: Path) -> Iterator[Finding]:
    img = re.compile(r"<img\b(?![^>]*\balt\s*=)[^>]*>", re.I)
    inp = re.compile(
        r"<input\b(?![^>]*(?:\baria-label\s*=|\baria-labelledby\s*=|type\s*=\s*[\"'](?:hidden|submit|button)[\"']))[^>]*>",
        re.I,
    )
    btn = re.compile(r"<button\b[^>]*>\s*</button>", re.I)
    for path in iter_files(root, {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".php"}):
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None:
            continue
        r = rel(root, path)
        for m in img.finditer(text):
            yield Finding(
                "accessibility",
                "medium",
                "a11y/img-alt",
                r,
                text.count("\n", 0, m.start()) + 1,
                m.group(0)[:160],
                "Images without alt text are invisible to screen readers.",
                'Add a meaningful alt attribute (alt="" for decorative images).',
                "medium",
                False,
                "native:a11y",
            )
        for m in inp.finditer(text):
            tag = m.group(0)
            ident = re.search(r"\bid\s*=\s*[\"']([^\"']+)", tag)
            if ident and re.search(
                rf"<label\b[^>]*\bfor\s*=\s*[\"']{re.escape(ident.group(1))}[\"']", text, re.I
            ):
                continue
            if re.search(r"<label\b", text[: m.start()][-300:], re.I):
                continue
            yield Finding(
                "accessibility",
                "medium",
                "a11y/input-label",
                r,
                text.count("\n", 0, m.start()) + 1,
                tag[:160],
                "Form inputs need an associated label or aria-label.",
                "Add <label for=...> or aria-label.",
                "low",
                False,
                "native:a11y",
            )
        for m in btn.finditer(text):
            yield Finding(
                "accessibility",
                "medium",
                "a11y/empty-button",
                r,
                text.count("\n", 0, m.start()) + 1,
                m.group(0)[:160],
                "A button with no text or aria-label has no accessible name.",
                "Give it visible text or aria-label.",
                "medium",
                False,
                "native:a11y",
            )
        if path.suffix.lower() in (".html", ".htm") and "<html" in text.lower():
            if not re.search(r"<html[^>]*\blang\s*=", text, re.I):
                yield Finding(
                    "accessibility",
                    "low",
                    "a11y/html-lang",
                    r,
                    1,
                    "<html> has no lang attribute",
                    "Screen readers need the page language.",
                    'Add lang="en" (or the right code).',
                    "high",
                    True,
                    "native:a11y",
                )
            if "viewport" not in text.lower():
                yield Finding(
                    "accessibility",
                    "low",
                    "a11y/viewport",
                    r,
                    1,
                    "no viewport meta tag",
                    "Pages without a viewport meta tag render poorly on mobile.",
                    'Add <meta name="viewport" content="width=device-width, initial-scale=1">.',
                    "high",
                    True,
                    "native:a11y",
                )
    js = re.compile(r"(\.innerHTML\s*=|document\.write\s*\(|dangerouslySetInnerHTML|\beval\s*\()")
    for path in iter_files(root, {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html"}):
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None or path.name.endswith(".min.js"):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            m = js.search(line)
            if m:
                yield Finding(
                    "security",
                    "medium",
                    "js/dom-injection",
                    rel(root, path),
                    lineno,
                    line.strip()[:200],
                    f"{m.group(1)} can introduce cross-site scripting when fed untrusted data.",
                    "Use textContent / framework escaping, or sanitise with a vetted library.",
                    "low",
                    False,
                    "native:js",
                )


def check_docker(root: Path) -> Iterator[Finding]:
    for path in iter_files(root):
        if path.name != "Dockerfile" and not path.name.startswith("Dockerfile."):
            continue
        text = read_text_safely(path)
        if text is None:
            continue
        r = rel(root, path)
        if not re.search(r"^\s*USER\s+(?!root\b)\S+", text, re.M):
            yield Finding(
                "security",
                "medium",
                "docker/runs-as-root",
                r,
                None,
                "no non-root USER instruction",
                "The container process runs as root.",
                "Create an unprivileged user and add a USER instruction.",
                "medium",
                True,
                "native:docker",
            )
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.match(r"^\s*FROM\s+\S+:latest\b", line) or re.match(
                r"^\s*FROM\s+[^:\s@]+\s*(AS\s+\S+)?$", line, re.I
            ):
                yield Finding(
                    "configuration",
                    "low",
                    "docker/unpinned-base",
                    r,
                    lineno,
                    line.strip(),
                    "An unpinned base image makes builds irreproducible.",
                    "Pin a specific version tag or digest.",
                    "high",
                    True,
                    "native:docker",
                )


def check_settings(root: Path) -> Iterator[Finding]:
    for path in iter_files(root, {".py", ".env", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg"}):
        if path.name.endswith(".example"):
            continue
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None:
            continue
        r = rel(root, path)
        for lineno, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if re.match(r"^DEBUG\s*=\s*True\b", s) and "settings" in path.name.lower():
                yield Finding(
                    "configuration",
                    "medium",
                    "config/debug-enabled",
                    r,
                    lineno,
                    s,
                    "DEBUG is hard-coded on in a settings module.",
                    "Drive it from an environment variable defaulting to off.",
                    "medium",
                    True,
                    "native:config",
                )
            if re.search(r"(?i)(allow_origins|cors_origins|Access-Control-Allow-Origin)\W+.*[\"']\*[\"']", s):
                yield Finding(
                    "security",
                    "medium",
                    "config/cors-wildcard",
                    r,
                    lineno,
                    s[:200],
                    "CORS allows every origin.",
                    "Restrict it to the origins that need access.",
                    "medium",
                    False,
                    "native:config",
                )


def check_project_hygiene(root: Path, has_git: bool) -> Iterator[Finding]:
    names = {p.name.lower() for p in root.iterdir()} if root.is_dir() else set()
    if has_git and ".gitignore" not in names:
        yield Finding(
            "configuration",
            "medium",
            "hygiene/no-gitignore",
            None,
            None,
            "no .gitignore",
            "Without one, build output and secrets are easy to commit.",
            "Add a .gitignore for your stack.",
            "high",
            True,
            "native:config",
        )
    if not any(n.startswith("readme") for n in names):
        yield Finding(
            "configuration",
            "low",
            "hygiene/no-readme",
            None,
            None,
            "no README",
            "New contributors have no entry point.",
            "Add a README.",
            "high",
            True,
            "native:config",
        )
    ci = (
        (root / ".github" / "workflows").is_dir()
        or (root / ".gitlab-ci.yml").exists()
        or (root / ".circleci").is_dir()
    )
    if not ci and has_git:
        yield Finding(
            "configuration",
            "info",
            "hygiene/no-ci",
            None,
            None,
            "no CI configuration found",
            "Tests and linters are not enforced automatically.",
            "Add a CI workflow that runs the test suite.",
            "medium",
            True,
            "native:config",
        )


def check_dependencies(root: Path) -> Iterator[Finding]:
    req = root / "requirements.txt"
    text = read_text_safely(req)
    if text:
        for lineno, line in enumerate(text.splitlines(), 1):
            s = line.split("#")[0].strip()
            if not s or s.startswith(("-", "git+", "http")):
                continue
            if not re.search(r"(==|~=|>=|<=|<|>|!=|@)", s):
                yield Finding(
                    "dependencies",
                    "low",
                    "deps/unpinned",
                    "requirements.txt",
                    lineno,
                    s,
                    "No version constraint; builds can break or pull in vulnerable releases.",
                    "Pin or bound the version.",
                    "high",
                    True,
                    "native:deps",
                )
            elif "==" not in s and ">=" in s and "<" not in s and "~=" not in s:
                yield Finding(
                    "dependencies",
                    "info",
                    "deps/open-upper-bound",
                    "requirements.txt",
                    lineno,
                    s,
                    "No upper bound; a future major release may break the project.",
                    "Consider a compatible-release (~=) or upper bound.",
                    "medium",
                    True,
                    "native:deps",
                )
    pkg = root / "package.json"
    text = read_text_safely(pkg)
    if text:
        import json

        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        for section in ("dependencies", "devDependencies"):
            for name, ver in (data.get(section) or {}).items():
                if str(ver).strip() in ("*", "latest", ""):
                    yield Finding(
                        "dependencies",
                        "medium",
                        "deps/wildcard",
                        "package.json",
                        None,
                        f"{name}: {ver}",
                        "Wildcard versions make installs non-reproducible.",
                        "Pin a semver range.",
                        "high",
                        True,
                        "native:deps",
                    )
        has_lock = any(
            (root / n).exists() for n in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb")
        )
        if (data.get("dependencies") or data.get("devDependencies")) and not has_lock:
            yield Finding(
                "dependencies",
                "low",
                "deps/no-lockfile",
                "package.json",
                None,
                "no lockfile",
                "Without a lockfile installs are not reproducible.",
                "Commit your package manager's lockfile.",
                "high",
                True,
                "native:deps",
            )


def py_modules(root: Path) -> dict[str, Path]:
    mods: dict[str, Path] = {}
    for path in iter_files(root, {".py"}):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            mods[".".join(parts)] = path
    return mods


def import_graph(root: Path) -> dict[str, set[str]]:
    mods = py_modules(root)
    graph: dict[str, set[str]] = {m: set() for m in mods}
    for mod, path in mods.items():
        text = read_text_safely(path, MAX_FILE_BYTES)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        is_pkg = path.name == "__init__.py"
        pkg = mod if is_pkg else mod.rpartition(".")[0]
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".") if pkg else []
                    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                    prefix = ".".join(base)
                    targets = [f"{prefix}.{node.module}" if node.module else prefix]
                    if not node.module:
                        targets = [f"{prefix}.{a.name}" for a in node.names]
                elif node.module:
                    targets = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for t in targets:
                while t and t not in mods:
                    t = t.rpartition(".")[0]
                if t and t != mod:
                    graph[mod].add(t)
    return graph


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def strong(v: str) -> None:
        work = [(v, iter(sorted(graph.get(v, ()))))]
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))

    for v in sorted(graph):
        if v not in index:
            strong(v)
    return out


def check_architecture(root: Path) -> Iterator[Finding]:
    graph = import_graph(root)
    mods = py_modules(root)
    for cycle in find_cycles(graph):
        parent = min(cycle, key=len)
        if all(m == parent or m.startswith(parent + ".") for m in cycle):
            continue
        yield Finding(
            "architecture",
            "medium",
            "arch/import-cycle",
            rel(root, mods[cycle[0]]),
            None,
            " <-> ".join(cycle[:6]) + (" ..." if len(cycle) > 6 else ""),
            "Modules import each other in a cycle, which causes fragile initialisation and tight coupling.",
            "Extract the shared piece into a third module or invert a dependency.",
            "high",
            True,
            "native:imports",
        )
    dependents: dict[str, int] = {}
    for deps in graph.values():
        for d in deps:
            dependents[d] = dependents.get(d, 0) + 1
    for mod, count in sorted(dependents.items(), key=lambda kv: -kv[1])[:3]:
        if count >= 12:
            yield Finding(
                "architecture",
                "info",
                "arch/hub-module",
                rel(root, mods[mod]),
                None,
                f"{mod} is imported by {count} modules",
                "A module depended on this widely is expensive to change.",
                "Keep its interface small and stable.",
                "medium",
                True,
                "native:imports",
            )


def check_test_coverage(root: Path) -> Iterator[Finding]:
    tests = [
        p
        for p in iter_files(root, {".py", ".js", ".ts", ".tsx", ".jsx"})
        if re.search(r"(^|/)(tests?|__tests__)/|test_|_test\.|\.test\.|\.spec\.", rel(root, p))
    ]
    sources = [
        p
        for p in iter_files(root, {".py", ".js", ".ts", ".tsx", ".jsx"})
        if p not in set(tests) and "migrations" not in p.parts
    ]
    if sources and not tests:
        yield Finding(
            "test-coverage",
            "medium",
            "tests/none",
            None,
            None,
            f"{len(sources)} source files, 0 test files",
            "The project has no automated tests.",
            "Add tests for the core behaviour first (try @generate-tests).",
            "high",
            True,
            "native:tests",
        )
        return
    blob = "\n".join(read_text_safely(t, 400_000) or "" for t in tests)
    untested = [
        s
        for s in sources
        if s.stem not in ("__init__", "__main__", "cli", "index", "main", "setup", "conftest")
        and s.stem not in blob
    ]
    if sources and len(untested) / max(len(sources), 1) > 0.5 and len(untested) >= 3:
        yield Finding(
            "test-coverage",
            "low",
            "tests/low-reference",
            None,
            None,
            f"{len(untested)} of {len(sources)} source modules are never referenced by any test file",
            "Heuristic only (name matching): these modules may be untested.",
            "Measure real coverage with coverage.py / your runner.",
            "low",
            False,
            "native:tests",
        )
    cov = root / "coverage.xml"
    text = read_text_safely(cov)
    if text:
        m = re.search(r'line-rate="([0-9.]+)"', text)
        if m and float(m.group(1)) < 0.6:
            yield Finding(
                "test-coverage",
                "medium",
                "tests/coverage-low",
                "coverage.xml",
                None,
                f"line coverage {float(m.group(1)) * 100:.0f}%",
                "Reported line coverage is below 60%.",
                "Add tests for uncovered paths.",
                "high",
                True,
                "native:coverage-xml",
            )


ALL_NATIVE: list[tuple[str, str]] = [
    ("secrets", "secrets and credentials"),
    ("python", "Python AST security/quality/error-handling"),
    ("web", "web accessibility and DOM injection"),
    ("docker", "Dockerfile hardening"),
    ("config", "configuration and hygiene"),
    ("deps", "dependency declarations"),
    ("architecture", "import graph and cycles"),
    ("tests", "test presence"),
    ("markers", "TODO/FIXME markers"),
]


def run_native(root: Path, names: Iterable[str], has_git: bool) -> Iterator[tuple[str, Finding]]:
    gitignore = read_text_safely(root / ".gitignore") or ""
    chosen = set(names)
    runners = {
        "secrets": lambda: [*check_secrets(root), *check_env_files(root, gitignore)],
        "python": lambda: check_python(root),
        "web": lambda: check_web(root),
        "docker": lambda: check_docker(root),
        "config": lambda: [*check_settings(root), *check_project_hygiene(root, has_git)],
        "deps": lambda: check_dependencies(root),
        "architecture": lambda: check_architecture(root),
        "tests": lambda: check_test_coverage(root),
        "markers": lambda: check_todos(root),
    }
    for name, fn in runners.items():
        if name in chosen:
            for finding in fn():
                yield name, finding
