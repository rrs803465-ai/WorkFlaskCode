from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from workflaskcode.audit.checks import iter_files, rel
from workflaskcode.audit.models import Finding
from workflaskcode.utils.fs import read_text_safely

MAX_BYTES = 2_000_000
ENTRY_NAMES = {"main", "setup", "teardown", "register", "app", "create_app", "handler", "lambda_handler"}
DUNDER = ("__", "__")


def _parse(path: Path) -> tuple[ast.Module | None, str]:
    text = read_text_safely(path, MAX_BYTES) or ""
    try:
        return ast.parse(text), text
    except SyntaxError:
        return None, text


def _exported(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                names.update(
                    e.value
                    for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    return names


def _used_names(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
            used.add(node.value)
    return used


def _unused_imports(tree: ast.Module, path: Path, root: Path) -> list[Finding]:
    if path.name == "__init__.py":
        return []
    used = _used_names(tree) | _exported(tree)
    out: list[Finding] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            pairs = [((a.asname or a.name.split(".")[0]), a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            pairs = [
                ((a.asname or a.name), f"{node.module or '.'}.{a.name}") for a in node.names if a.name != "*"
            ]
        else:
            continue
        for bound, full in pairs:
            if bound not in used:
                out.append(
                    Finding(
                        "maintainability",
                        "low",
                        "dead-code.unused-import",
                        rel(root, path),
                        node.lineno,
                        f"import of {full} is never referenced in this file",
                        "The name is imported but no reference, annotation string or __all__ entry uses it.",
                        f"Remove `{bound}` from the import, or re-export it via __all__ if it is public API.",
                        "high",
                        True,
                        "native:deadcode",
                    )
                )
    return out


def _definitions(tree: ast.Module) -> list[tuple[str, int, str]]:
    defs: list[tuple[str, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append((node.name, node.lineno, "function"))
        elif isinstance(node, ast.ClassDef):
            defs.append((node.name, node.lineno, "class"))
    return defs


def _decorated(tree: ast.Module, name: str) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return bool(node.decorator_list)
    return False


def find_dead_code(root: Path) -> list[Finding]:
    trees: dict[Path, tuple[ast.Module, str]] = {}
    for path in iter_files(root, {".py"}):
        tree, text = _parse(path)
        if tree is not None:
            trees[path] = (tree, text)
    references: Counter[str] = Counter()
    for tree, _ in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                references[node.id] += 1
            elif isinstance(node, ast.Attribute):
                references[node.attr] += 1
            elif isinstance(node, ast.alias):
                references[node.name.split(".")[-1]] += 1
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                references[node.value] += 1
    findings: list[Finding] = []
    for path, (tree, text) in trees.items():
        findings.extend(_unused_imports(tree, path, root))
        is_test = "tests" in path.parts or path.name.startswith("test_") or path.name == "conftest.py"
        if is_test:
            continue
        exported = _exported(tree)
        for name, line, kind in _definitions(tree):
            if name.startswith(DUNDER[0]) and name.endswith(DUNDER[1]):
                continue
            if name in ENTRY_NAMES or name in exported or _decorated(tree, name):
                continue
            if references[name] > 0:
                continue
            if f"{name}(" in text.replace(f"def {name}(", "").replace(f"class {name}(", ""):
                continue
            findings.append(
                Finding(
                    "maintainability",
                    "info",
                    f"dead-code.unreferenced-{kind}",
                    rel(root, path),
                    line,
                    f"{kind} {name} has no references anywhere in the project's Python files",
                    "No import, call, attribute access or string reference to this name exists. Dynamic use "
                    "(plugins, getattr, entry points in packaging metadata, external callers) cannot be ruled out statically.",
                    f"If nothing outside the project calls `{name}`, delete it; otherwise export it in __all__ or document it as public API.",
                    "medium",
                    False,
                    "native:deadcode",
                )
            )
    return sorted(findings, key=lambda f: (f.file or "", f.line or 0))
