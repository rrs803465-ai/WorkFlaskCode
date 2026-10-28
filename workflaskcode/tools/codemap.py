from __future__ import annotations

import ast
import re
from pathlib import Path

from workflaskcode.errors import ToolError
from workflaskcode.tools.files import MAX_SEARCH_FILE_BYTES, FileTools
from workflaskcode.utils.fs import read_text_safely, sha256_bytes

MAP_CHAR_LIMIT = 14_000
SYMBOL_LINE_LIMIT = 220
SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".swift",
}
DEF_LINE = re.compile(
    r"^(?P<indent>\s*)(?:export\s+(?:default\s+)?)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+|pub\s+(?:async\s+)?)*"
    r"(?P<kind>def|class|function|func|fn|struct|interface|type|enum|trait|impl|module)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
CONST_LINE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)


def _py_outline(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    rows: list[str] = []

    def signature(node: ast.AST) -> str:
        args = ast.unparse(node.args)  # type: ignore[attr-defined]
        return args if len(args) <= 70 else args[:67] + "..."

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            rows.append(f"  L{node.lineno} class {node.name}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                    rows.append(f"    L{child.lineno} {prefix} {child.name}({signature(child)})")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            rows.append(f"  L{node.lineno} {prefix} {node.name}({signature(node)})")
    return rows


def _generic_outline(text: str) -> list[str]:
    rows: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = DEF_LINE.match(line)
        if match:
            depth = min(len(match.group("indent").expandtabs(4)) // 4, 2)
            rows.append(f"{'  ' * (depth + 1)}L{number} {match.group('kind')} {match.group('name')}")
            continue
        const = CONST_LINE.match(line)
        if const:
            rows.append(f"  L{number} const {const.group('name')}")
    return rows


class CodeMap:
    def __init__(self, files: FileTools) -> None:
        self.files = files

    def repo_map(self, path: str = ".", max_files: int = 80) -> str:
        base = self.files.guard.resolve(path)
        entries: list[str] = []
        total_files = 0
        used = 0
        truncated = False
        for file in self.files._walk_files(base):
            if file.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            total_files += 1
            if len(entries) >= max_files or used > MAP_CHAR_LIMIT:
                truncated = True
                continue
            text = read_text_safely(file, MAX_SEARCH_FILE_BYTES)
            if text is None:
                continue
            outline = _py_outline(text) if file.suffix == ".py" else _generic_outline(text)
            outline = outline[:40]
            block = f"{self.files.guard.relative(file)} ({text.count(chr(10)) + 1} lines)"
            if outline:
                block += "\n" + "\n".join(outline)
            entries.append(block)
            used += len(block)
        if not entries:
            return "no source files found under " + path
        footer = (
            f"\n[{len(entries)} of {total_files} source files shown; narrow `path` or raise max_files to see more]"
            if truncated
            else ""
        )
        return "\n".join(entries) + footer

    def read_symbol(self, name: str, path: str = ".", limit: int = 3) -> str:
        wanted = name.strip()
        if not wanted:
            raise ToolError("name is required")
        base = self.files.guard.resolve(path)
        results: list[str] = []
        for file in self.files._walk_files(base):
            if file.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            raw = read_text_safely(file, MAX_SEARCH_FILE_BYTES)
            if raw is None or wanted.split(".")[-1] not in raw:
                continue
            found = _python_symbol(raw, wanted) if file.suffix == ".py" else _generic_symbol(raw, wanted)
            for start, end in found:
                lines = raw.splitlines()
                window = lines[start - 1 : min(end, start - 1 + SYMBOL_LINE_LIMIT)]
                body = "\n".join(f"{i:>5}\t{text}" for i, text in enumerate(window, start))
                if end - start + 1 > SYMBOL_LINE_LIMIT:
                    body += f"\n[... {end - start + 1 - SYMBOL_LINE_LIMIT} more line(s); use read_file with start/end]"
                results.append(f"{self.files.guard.relative(file)}:{start}-{end}\n{body}")
                self._mark_seen(file)
                if len(results) >= limit:
                    return "\n\n".join(results)
        return "\n\n".join(results) or f"no definition of '{wanted}' found; try find_symbol or search"

    def _mark_seen(self, file: Path) -> None:
        try:
            self.files._seen[str(file)] = sha256_bytes(file.read_bytes())
        except OSError:
            pass


def _python_symbol(text: str, wanted: str) -> list[tuple[int, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _generic_symbol(text, wanted)
    parts = wanted.split(".")
    hits: list[tuple[int, int]] = []

    def visit(body: list[ast.stmt], remaining: list[str]) -> None:
        for node in body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == remaining[0]
            ):
                if len(remaining) == 1:
                    first = min([node.lineno, *[d.lineno for d in node.decorator_list]])
                    hits.append((first, node.end_lineno or node.lineno))
                elif isinstance(node, ast.ClassDef):
                    visit(node.body, remaining[1:])
            elif isinstance(node, ast.ClassDef) and len(remaining) == 1 and len(parts) == 1:
                visit(node.body, remaining)

    visit(tree.body, parts)
    return hits


def _generic_symbol(text: str, wanted: str) -> list[tuple[int, int]]:
    target = wanted.split(".")[-1]
    lines = text.splitlines()
    hits: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = DEF_LINE.match(line)
        const = CONST_LINE.match(line)
        name = match.group("name") if match else const.group("name") if const else None
        if name != target:
            continue
        indent = len(line) - len(line.lstrip())
        depth = 0
        opened = False
        end = index
        for j in range(index, min(len(lines), index + 800)):
            depth += lines[j].count("{") - lines[j].count("}")
            opened = opened or "{" in lines[j]
            end = j
            if opened and depth <= 0:
                break
            if (
                not opened
                and j > index
                and lines[j].strip()
                and len(lines[j]) - len(lines[j].lstrip()) <= indent
                and not lines[j].lstrip().startswith((")", "]", "}", "else", "elif"))
            ):
                end = j - 1
                break
        hits.append((index + 1, end + 1))
    return hits
