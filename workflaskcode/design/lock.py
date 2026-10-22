from __future__ import annotations

import ast
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from workflaskcode.audit.checks import iter_files, rel
from workflaskcode.audit.models import Finding
from workflaskcode.utils.fs import read_text_safely
from workflaskcode.workspace.metadata import ProjectMetadata
from workflaskcode.workspace.project import ProjectInfo

STYLE_EXTS = {".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".jsx", ".tsx"}
MAX_BYTES = 600_000
HEX = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
CSS_VAR = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;}\n]+)")
FONT = re.compile(r"font-family\s*:\s*([^;}\n]+)", re.I)
BREAKPOINT = re.compile(r"@media[^{]*\(\s*(?:min|max)-width\s*:\s*(\d+(?:\.\d+)?)(px|em|rem)\s*\)", re.I)
SPACING = re.compile(r"(?:margin|padding|gap)(?:-[a-z]+)?\s*:\s*([^;}\n]+)", re.I)
ROUTE_PY = re.compile(r"@\w+\.(get|post|put|patch|delete|route)\(\s*[\"']([^\"']+)[\"']")
ROUTE_JS = re.compile(r"\b(?:app|router)\.(get|post|put|patch|delete)\(\s*[\"'`]([^\"'`]+)[\"'`]")
SQL_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?", re.I)
SNAKE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
CAMEL = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$")


@dataclass
class DesignScan:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)
    colors: list[str] = field(default_factory=list)
    fonts: list[str] = field(default_factory=list)
    breakpoints: list[str] = field(default_factory=list)
    spacing_values: list[str] = field(default_factory=list)
    component_dirs: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    naming: dict[str, int] = field(default_factory=dict)
    test_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DesignScan:
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def has_ui(self) -> bool:
        return bool(self.tokens or self.colors or self.fonts or self.breakpoints or self.component_dirs)


def _normalise_hex(value: str) -> str:
    v = value.lower()
    if len(v) == 4:
        v = "#" + "".join(ch * 2 for ch in v[1:])
    return v


def scan_project(root: Path, info: ProjectInfo | None = None) -> DesignScan:
    scan = DesignScan()
    if info:
        scan.languages = sorted(info.languages)
        scan.frameworks = sorted(info.frameworks)
        scan.test_commands = [" ".join(c) for c in info.test_commands]
    tokens: dict[str, str] = {}
    colors: Counter[str] = Counter()
    fonts: Counter[str] = Counter()
    breakpoints: set[str] = set()
    spacing: Counter[str] = Counter()
    routes: set[str] = set()
    tables: set[str] = set()
    naming: Counter[str] = Counter()
    comp_dirs: set[str] = set()
    for path in iter_files(root):
        suffix = path.suffix.lower()
        parts = path.relative_to(root).parts
        for marker in ("components", "pages", "views", "layouts", "templates"):
            if marker in parts[:-1]:
                comp_dirs.add("/".join(parts[: parts.index(marker) + 1]))
        text = (
            read_text_safely(path, MAX_BYTES)
            if suffix in STYLE_EXTS | {".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".php"}
            else None
        )
        if text is None:
            continue
        if suffix in STYLE_EXTS:
            for name, value in CSS_VAR.findall(text):
                tokens.setdefault(name, value.strip())
            for m in HEX.findall(text):
                colors[_normalise_hex(m)] += 1
            for m in FONT.findall(text):
                fonts[m.strip().strip(";")[:80]] += 1
            for amount, unit in BREAKPOINT.findall(text):
                breakpoints.add(f"{amount}{unit}")
            for m in SPACING.findall(text):
                if len(m) < 30 and "var(" not in m:
                    spacing[m.strip()] += 1
        if suffix == ".py":
            for method, route in ROUTE_PY.findall(text):
                routes.add(f"{method.upper()} {route}" if method != "route" else f"ANY {route}")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
                    "__"
                ):
                    naming[
                        "snake_case"
                        if SNAKE.match(node.name) or node.name.islower()
                        else "camelCase"
                        if CAMEL.match(node.name)
                        else "other"
                    ] += 1
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id == "__tablename__"
                            and isinstance(node.value, ast.Constant)
                        ):
                            tables.add(str(node.value.value))
        elif suffix in {".js", ".ts", ".tsx", ".jsx"}:
            for method, route in ROUTE_JS.findall(text):
                routes.add(f"{method.upper()} {route}")
            for name in re.findall(
                r"\bfunction\s+([A-Za-z_]\w*)|const\s+([a-z]\w*)\s*=\s*(?:async\s*)?\(", text
            ):
                ident = name[0] or name[1]
                naming[
                    "camelCase" if CAMEL.match(ident) else "snake_case" if SNAKE.match(ident) else "other"
                ] += 1
        elif suffix == ".sql":
            tables.update(SQL_TABLE.findall(text))
    scan.tokens = dict(sorted(tokens.items()))
    scan.colors = [c for c, _ in colors.most_common(40)]
    scan.fonts = [f for f, _ in fonts.most_common(12)]
    scan.breakpoints = sorted(breakpoints, key=lambda b: float(re.match(r"[\d.]+", b).group(0)))
    scan.spacing_values = [s for s, _ in spacing.most_common(20)]
    scan.component_dirs = sorted(comp_dirs)
    scan.routes = sorted(routes)
    scan.tables = sorted(tables)
    scan.naming = dict(naming)
    return scan


def dominant_naming(naming: dict[str, int]) -> str | None:
    pool = {k: v for k, v in naming.items() if k != "other" and v}
    if not pool:
        return None
    return max(pool, key=pool.get)


class DesignLock:
    FILE = "design.json"

    def __init__(self, root: Path, metadata: ProjectMetadata) -> None:
        self.root = root
        self.metadata = metadata

    def load(self) -> dict[str, Any] | None:
        raw = self.metadata.read_json(self.FILE)
        return raw if isinstance(raw, dict) and "scan" in raw else None

    @property
    def locked(self) -> bool:
        return self.load() is not None

    def lock(
        self,
        info: ProjectInfo | None,
        *,
        rules: list[str] | None = None,
        prohibited: list[str] | None = None,
        decisions: list[str] | None = None,
    ) -> dict[str, Any]:
        previous = self.load() or {}
        scan = scan_project(self.root, info)
        data = {
            "version": 1,
            "locked_at": time.time(),
            "scan": scan.to_dict(),
            "rules": rules if rules is not None else previous.get("rules", []),
            "prohibited": prohibited if prohibited is not None else previous.get("prohibited", []),
            "decisions": decisions if decisions is not None else previous.get("decisions", []),
        }
        self.metadata.ensure()
        self.metadata.write_json(self.FILE, data)
        self.metadata.update_project_md(self.render_markdown(data))
        return data

    def add_rule(self, kind: str, text: str) -> dict[str, Any]:
        data = self.load()
        if data is None:
            raise ValueError("no design lock yet; run @design-lock first")
        if kind not in ("rules", "prohibited", "decisions"):
            raise ValueError("kind must be rules, prohibited or decisions")
        if text not in data[kind]:
            data[kind].append(text)
        self.metadata.write_json(self.FILE, data)
        self.metadata.update_project_md(self.render_markdown(data))
        return data

    def unlock(self) -> bool:
        path = self.metadata.directory / self.FILE
        if path.exists():
            path.replace(path.with_name(f"{self.FILE}.unlocked-{int(time.time())}"))
            return True
        return False

    @staticmethod
    def render_markdown(data: dict[str, Any]) -> str:
        scan = DesignScan.from_dict(data["scan"])
        lines = [
            "## Design lock",
            "",
            "Preserve these conventions unless the user explicitly asks for a redesign.",
        ]
        if scan.frameworks or scan.languages:
            lines.append(
                f"- Stack: {', '.join(scan.languages)} / {', '.join(scan.frameworks) or 'no framework detected'}"
            )
        if scan.tokens:
            lines.append(
                "- Design tokens: " + ", ".join(f"{k}={v}" for k, v in list(scan.tokens.items())[:14])
            )
        if scan.colors:
            lines.append("- Palette: " + ", ".join(scan.colors[:12]))
        if scan.fonts:
            lines.append("- Typography: " + "; ".join(scan.fonts[:4]))
        if scan.breakpoints:
            lines.append("- Breakpoints: " + ", ".join(scan.breakpoints))
        if scan.component_dirs:
            lines.append("- Component structure: " + ", ".join(scan.component_dirs))
        if scan.routes:
            lines.append("- API/route contract: " + ", ".join(scan.routes[:25]))
        if scan.tables:
            lines.append("- Database tables: " + ", ".join(scan.tables))
        naming = dominant_naming(scan.naming)
        if naming:
            lines.append(f"- Naming convention: {naming}")
        if scan.test_commands:
            lines.append("- Testing: " + "; ".join(scan.test_commands))
        for title, key in (
            ("Rules", "rules"),
            ("Approved decisions", "decisions"),
            ("Prohibited changes", "prohibited"),
        ):
            if data.get(key):
                lines.append(f"- {title}:")
                lines += [f"  - {item}" for item in data[key]]
        return "\n".join(lines)

    def rules_text(self) -> str | None:
        data = self.load()
        return self.render_markdown(data) if data else None

    def diff(self, info: ProjectInfo | None) -> list[str]:
        data = self.load()
        if data is None:
            raise ValueError("no design lock yet; run @design-lock first")
        old = DesignScan.from_dict(data["scan"])
        new = scan_project(self.root, info)
        out: list[str] = []

        def sets(label: str, a: list[str], b: list[str]) -> None:
            added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
            for item in added:
                out.append(f"+ {label}: {item}")
            for item in removed:
                out.append(f"- {label}: {item}")

        sets("framework", old.frameworks, new.frameworks)
        sets("language", old.languages, new.languages)
        for name in sorted(set(old.tokens) | set(new.tokens)):
            if name not in new.tokens:
                out.append(f"- token: {name}: {old.tokens[name]}")
            elif name not in old.tokens:
                out.append(f"+ token: {name}: {new.tokens[name]}")
            elif old.tokens[name] != new.tokens[name]:
                out.append(f"~ token: {name}: {old.tokens[name]} -> {new.tokens[name]}")
        sets("color", old.colors, new.colors)
        sets("font", old.fonts, new.fonts)
        sets("breakpoint", old.breakpoints, new.breakpoints)
        sets("component dir", old.component_dirs, new.component_dirs)
        sets("route", old.routes, new.routes)
        sets("table", old.tables, new.tables)
        old_naming, new_naming = dominant_naming(old.naming), dominant_naming(new.naming)
        if old_naming and new_naming and old_naming != new_naming:
            out.append(f"~ naming convention: {old_naming} -> {new_naming}")
        return out

    def audit(self, info: ProjectInfo | None) -> list[Finding]:
        data = self.load()
        if data is None:
            raise ValueError("no design lock yet; run @design-lock first")
        old = DesignScan.from_dict(data["scan"])
        new = scan_project(self.root, info)
        findings: list[Finding] = []

        def add(severity: str, rule: str, evidence: str, why: str, fix: str) -> None:
            findings.append(
                Finding(
                    "design",
                    severity,
                    rule,
                    None,
                    None,
                    evidence,
                    why,
                    fix,
                    "medium",
                    True,
                    "native:design-lock",
                )
            )

        for route in sorted(set(old.routes) - set(new.routes)):
            add(
                "high",
                "design/route-removed",
                route,
                "A locked API route no longer exists; clients may break.",
                "Restore it, or update the lock deliberately (@design-lock --update).",
            )
        for table in sorted(set(old.tables) - set(new.tables)):
            add(
                "high",
                "design/table-removed",
                table,
                "A locked database table is no longer defined.",
                "Restore it or add a migration and update the lock.",
            )
        if old.frameworks and set(old.frameworks) != set(new.frameworks):
            add(
                "high",
                "design/framework-changed",
                f"{old.frameworks} -> {new.frameworks}",
                "The detected framework set changed.",
                "Confirm the migration was intended and update the lock.",
            )
        if old.tokens:
            for name, value in old.tokens.items():
                if name in new.tokens and new.tokens[name] != value:
                    add(
                        "medium",
                        "design/token-changed",
                        f"{name}: {value} -> {new.tokens[name]}",
                        "A design token's value changed.",
                        "Revert it, or update the lock if this is a deliberate redesign.",
                    )
                if name not in new.tokens:
                    add(
                        "medium",
                        "design/token-removed",
                        name,
                        "A locked design token was removed.",
                        "Restore it or update the lock.",
                    )
        known_colors = set(old.colors) | {
            _normalise_hex(v) for v in old.tokens.values() if HEX.fullmatch(v.strip())
        }
        if known_colors:
            fresh = [c for c in new.colors if c not in known_colors]
            if len(fresh) >= 3:
                add(
                    "low",
                    "design/new-colors",
                    ", ".join(fresh[:8]),
                    f"{len(fresh)} colours appear that were not in the locked palette.",
                    "Use existing tokens, or extend the palette deliberately.",
                )
        fresh_fonts = sorted(set(new.fonts) - set(old.fonts))
        if old.fonts and fresh_fonts:
            add(
                "low",
                "design/new-fonts",
                "; ".join(fresh_fonts[:3]),
                "New font stacks were introduced.",
                "Reuse the locked typography or update the lock.",
            )
        fresh_bp = sorted(set(new.breakpoints) - set(old.breakpoints))
        if old.breakpoints and fresh_bp:
            add(
                "low",
                "design/new-breakpoints",
                ", ".join(fresh_bp),
                "Responsive breakpoints differ from the locked set.",
                "Align with the existing breakpoints.",
            )
        old_naming, new_naming = dominant_naming(old.naming), dominant_naming(new.naming)
        if old_naming and new_naming and old_naming != new_naming:
            add(
                "medium",
                "design/naming-drift",
                f"{old_naming} -> {new_naming}",
                "The dominant naming convention changed.",
                "Follow the original convention.",
            )
        for text in data.get("prohibited", []):
            keyword = text.strip().lower()
            if keyword and len(keyword) > 3:
                for path in iter_files(self.root):
                    body = read_text_safely(path, MAX_BYTES)
                    if (
                        body
                        and keyword in body.lower()
                        and path.suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json"}
                    ):
                        findings.append(
                            Finding(
                                "design",
                                "medium",
                                "design/prohibited-term",
                                rel(self.root, path),
                                None,
                                text,
                                "The file mentions a term listed under prohibited changes (keyword match; review manually).",
                                "Confirm this is not a prohibited change.",
                                "low",
                                False,
                                "native:design-lock",
                            )
                        )
                        break
        return findings
