import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflaskcode.commands.registry import build_registry  # noqa: E402

ORDER = ["general", "models", "planning", "code", "project", "design", "extensions"]
TITLES = {
    "general": "General",
    "models": "Models and providers",
    "planning": "Planning, safety and recovery",
    "code": "Code intelligence",
    "project": "Project and release",
    "design": "Design",
    "extensions": "Extensions and data",
}


def render() -> str:
    registry = build_registry()
    groups: dict[str, list] = {}
    for command in registry.all():
        groups.setdefault(command.group, []).append(command)
    lines = [
        "# Command reference",
        "",
        "Generated from the command registry (`python3 docs/gen_commands.py`). Every command has a real "
        "implementation; commands that need an optional tool say what to install when it is missing.",
        "",
        "Commands start with `@`. Anything else you type is sent to the agent. Flags use `--name value`. "
        "`@help <command>` shows usage inside a session, and every command also works as a subcommand: "
        "`python3 workflaskcode.py audit --json`.",
        "",
    ]
    for group in ORDER + [g for g in groups if g not in ORDER]:
        if group not in groups:
            continue
        lines += [
            f"## {TITLES.get(group, group.title())}",
            "",
            "| Command | What it does | Usage |",
            "|---|---|---|",
        ]
        for c in sorted(groups[group], key=lambda c: c.name):
            usage = f"`@{c.name} {c.usage}`".replace("|", "\\|") if c.usage else f"`@{c.name}`"
            alias = f" (alias: {', '.join('@' + a for a in c.aliases)})" if c.aliases else ""
            lines.append(f"| `@{c.name}` | {c.help}{alias} | {usage} |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    Path(__file__).with_name("commands.md").write_text(render(), encoding="utf-8")
