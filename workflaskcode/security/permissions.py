from __future__ import annotations

import fnmatch
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from workflaskcode.config.models import PermissionMode, PermissionSettings


class Risk(IntEnum):
    READ = 0
    PROJECT = 1
    MODIFY = 2
    NETWORK = 3
    DESTRUCTIVE = 4
    FORBIDDEN = 5


@dataclass
class Decision:
    action: str
    risk: Risk
    reason: str
    effects: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


@dataclass
class ApprovalRequest:
    kind: str
    summary: str
    detail: str = ""
    cwd: str | None = None
    effects: list[str] = field(default_factory=list)
    risk: Risk = Risk.MODIFY
    destructive: bool = False


@dataclass
class ApprovalResult:
    approved: bool
    remember: bool = False
    note: str = ""


Approver = Callable[[ApprovalRequest], ApprovalResult]


def deny_all_approver(_: ApprovalRequest) -> ApprovalResult:
    return ApprovalResult(False, note="no interactive approver available")


READ_PROGRAMS = {
    "ls",
    "dir",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "rg",
    "pwd",
    "echo",
    "which",
    "where",
    "whoami",
    "date",
    "tree",
    "stat",
    "file",
    "diff",
    "sort",
    "uniq",
    "type",
    "du",
    "df",
    "basename",
    "dirname",
    "realpath",
    "printf",
    "true",
    "false",
    "env",
    "printenv",
    "uname",
    "hostname",
    "id",
    "cut",
    "tr",
    "awk",
    "jq",
    "less",
    "more",
    "readlink",
    "sha256sum",
    "md5sum",
}
READ_GIT = {
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "ls-files",
    "blame",
    "describe",
    "shortlog",
    "ls-tree",
    "cat-file",
    "rev-list",
    "reflog",
    "grep",
    "config",
}
PROJECT_PROGRAMS = {
    "pytest",
    "ruff",
    "mypy",
    "flake8",
    "black",
    "isort",
    "pylint",
    "bandit",
    "semgrep",
    "tox",
    "nox",
    "eslint",
    "prettier",
    "tsc",
    "vitest",
    "jest",
    "gofmt",
    "rustfmt",
    "cargo",
    "go",
    "make",
    "pyright",
    "coverage",
}
PROJECT_PM_SUBCOMMANDS = {"test", "run", "build", "lint", "check", "typecheck", "fmt", "vet", "t"}
PM_PROGRAMS = {"npm", "pnpm", "yarn", "bun", "npx"}
NETWORK_PROGRAMS = {
    "curl",
    "wget",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "ftp",
    "nc",
    "ncat",
    "telnet",
    "nmap",
    "ping",
    "dig",
    "nslookup",
    "gh",
    "aws",
    "gcloud",
    "az",
    "kubectl",
    "docker",
    "podman",
}
INSTALL_SUBCOMMANDS = {"install", "add", "i", "ci", "update", "upgrade", "publish", "download", "get"}
MODIFY_PROGRAMS = {"mkdir", "touch", "cp", "mv", "ln", "tee", "patch", "sed", "install", "chmod", "chown"}
DESTRUCTIVE_PROGRAMS = {"rm", "rmdir", "del", "erase", "unlink", "shred", "truncate", "rd", "remove-item"}
FORBIDDEN_PROGRAMS = {
    "sudo",
    "su",
    "doas",
    "pkexec",
    "mkfs",
    "fdisk",
    "parted",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init",
    "mount",
    "umount",
    "crontab",
    "iptables",
    "ufw",
    "passwd",
    "useradd",
    "userdel",
    "visudo",
    "format",
    "diskpart",
    "reg",
    "bcdedit",
}
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "cmd", "powershell", "pwsh"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php"}

DESTRUCTIVE_GIT = {
    ("reset", "--hard"),
    ("clean", "-f"),
    ("clean", "-fd"),
    ("clean", "-fdx"),
    ("clean", "-df"),
    ("checkout", "--"),
    ("restore",),
    ("push",),
    ("branch", "-D"),
    ("branch", "-d"),
    ("stash", "drop"),
    ("stash", "clear"),
    ("rebase",),
    ("filter-branch",),
    ("reflog", "expire"),
    ("gc", "--prune"),
    ("worktree", "remove"),
    ("tag", "-d"),
}
MODIFY_GIT = {
    "add",
    "commit",
    "switch",
    "checkout",
    "stash",
    "branch",
    "tag",
    "mv",
    "merge",
    "cherry-pick",
    "apply",
    "worktree",
    "init",
    "revert",
    "am",
}
NETWORK_GIT = {"clone", "fetch", "pull", "remote", "submodule", "ls-remote"}

_SENSITIVE_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE)")
_ENV_KEEP = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SHELL",
    "PWD",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "NODE_PATH",
    "GOPATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "TZ",
    "CI",
    "NO_COLOR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
}


def filtered_env(env: dict[str, str] | None = None, extra_keep: set[str] | None = None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    keep = _ENV_KEEP | (extra_keep or set())
    out: dict[str, str] = {}
    for key, value in source.items():
        if key in keep or (not _SENSITIVE_ENV.search(key) and not key.startswith(("AWS_", "GH_", "GITHUB_"))):
            if key.lower().endswith("_proxy"):
                value = re.sub(r"//[^/@]*@", "//", value)
            out[key] = value
    return out


_OPERATORS = {";", "&&", "||", "|", "&", "\n"}


def split_shell(command: str) -> tuple[list[list[str]], list[str]]:
    notes: list[str] = []
    if "$(" in command or "`" in command:
        notes.append("command substitution")
    if re.search(r"(?<![<>])[<>]{1,2}(?![<>&=])\s*\S", command) or ">" in command:
        notes.append("redirection")
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    tokens = list(lex)
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _OPERATORS or (set(tok) <= set(";&|") and tok):
            if segments[-1]:
                segments.append([])
            if tok == "|":
                notes.append("pipe")
            continue
        if tok in (">", ">>", "<", "2>", "&>", ">&") or set(tok) <= set("<>&"):
            notes.append("redirection")
            continue
        segments[-1].append(tok)
    return [s for s in segments if s], list(dict.fromkeys(notes))


def _prog(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/")).lower()
    return name[:-4] if name.endswith(".exe") else name


class PermissionPolicy:
    def __init__(self, settings: PermissionSettings, workspace_root: Path | None = None) -> None:
        self.settings = settings
        self.workspace_root = workspace_root
        self._session_allowed: set[str] = set()

    @property
    def mode(self) -> PermissionMode:
        return self.settings.mode

    def can_write_files(self) -> bool:
        return self.mode is not PermissionMode.READ_ONLY

    def can_execute(self) -> bool:
        return self.mode is not PermissionMode.READ_ONLY

    def remember(self, command_key: str) -> None:
        self._session_allowed.add(command_key)

    def is_remembered(self, key: str) -> bool:
        return key in self._session_allowed

    def classify_argv(self, argv: list[str]) -> tuple[Risk, str, list[str]]:
        if not argv:
            return Risk.FORBIDDEN, "empty command", []
        prog = _prog(argv[0])
        args = argv[1:]
        low = [a.lower() for a in args]
        if prog in FORBIDDEN_PROGRAMS:
            return Risk.FORBIDDEN, f"'{prog}' is never run by the agent", []
        if prog in SHELLS:
            if any(a in ("-c", "/c", "-command", "-encodedcommand") for a in low):
                return Risk.MODIFY, f"nested {prog} invocation", ["runs an opaque nested shell command"]
            return Risk.MODIFY, f"interactive/opaque {prog}", ["starts a shell"]
        if prog in ("eval", "exec", "source", ".", "xargs", "nohup", "setsid", "watch", "timeout"):
            if prog in ("timeout", "nohup", "setsid") and len(argv) > 1:
                inner = [
                    a for a in argv[1:] if not a.startswith("-") and not re.fullmatch(r"[\d.]+[smhd]?", a)
                ]
                if inner:
                    return self.classify_argv(inner)
            return Risk.MODIFY, f"'{prog}' runs other commands indirectly", ["indirect execution"]
        if prog == "rm":
            return self._classify_rm(args)
        if prog in DESTRUCTIVE_PROGRAMS:
            return Risk.DESTRUCTIVE, f"'{prog}' deletes data", ["deletes files"]
        if prog == "dd":
            if any(a.lower().startswith("of=/dev/") for a in args):
                return Risk.FORBIDDEN, "dd to a device", []
            return Risk.DESTRUCTIVE, "dd overwrites data", ["overwrites data"]
        if prog == "git":
            return self._classify_git(args)
        if prog in (
            "pip",
            "pip3",
            "uv",
            "pipx",
            "poetry",
            "conda",
            "mamba",
            "brew",
            "apt",
            "apt-get",
            "dnf",
            "yum",
            "pacman",
            "apk",
            "choco",
            "winget",
            "scoop",
            "gem",
            "bundle",
            "composer",
        ):
            sub = next((a for a in low if not a.startswith("-")), "")
            if sub in INSTALL_SUBCOMMANDS or sub in ("sync", "lock", "remove", "uninstall", "tool", "run"):
                if sub in ("run",):
                    return Risk.PROJECT, f"{prog} run executes project code", ["runs project code"]
                return (
                    Risk.NETWORK,
                    f"{prog} {sub} installs or changes packages",
                    ["network access", "installs packages"],
                )
            return Risk.READ, f"{prog} {sub}".strip(), []
        if prog in PM_PROGRAMS:
            sub = next((a for a in low if not a.startswith("-")), "")
            if sub in INSTALL_SUBCOMMANDS:
                return (
                    Risk.NETWORK,
                    f"{prog} {sub} installs packages",
                    ["network access", "installs packages"],
                )
            if prog == "npx" or sub in PROJECT_PM_SUBCOMMANDS or sub == "":
                return Risk.PROJECT, f"{prog} {sub}".strip(), ["runs project scripts"]
            return Risk.MODIFY, f"{prog} {sub}", []
        if prog in ("python", "python3", "py"):
            if len(low) >= 2 and low[0] == "-m":
                mod = low[1]
                if mod in ("pip",):
                    sub = next((a for a in low[2:] if not a.startswith("-")), "")
                    if sub in INSTALL_SUBCOMMANDS:
                        return Risk.NETWORK, "pip install", ["network access", "installs packages"]
                    return Risk.READ, "pip", []
                if mod in (
                    "pytest",
                    "unittest",
                    "ruff",
                    "mypy",
                    "black",
                    "isort",
                    "coverage",
                    "flake8",
                    "pylint",
                    "bandit",
                    "build",
                    "compileall",
                ):
                    return Risk.PROJECT, f"python -m {mod}", ["runs project code"]
                if mod == "venv":
                    return Risk.MODIFY, "creates a virtual environment", ["creates files"]
            if "--version" in low or "-V" in args:
                return Risk.READ, "python version", []
            return Risk.MODIFY, "runs a Python script", ["executes arbitrary code"]
        if prog in INTERPRETERS:
            if "--version" in low or "-v" == (low[0] if low else ""):
                return Risk.READ, f"{prog} version", []
            return Risk.MODIFY, f"runs {prog} code", ["executes arbitrary code"]
        if prog in NETWORK_PROGRAMS:
            if prog == "docker" and low[:1] in (["ps"], ["images"], ["version"], ["info"]):
                return Risk.READ, "docker inspection", []
            return Risk.NETWORK, f"'{prog}' uses the network", ["network access"]
        if prog == "find":
            if any(a in ("-delete", "-exec", "-execdir", "-ok") for a in low):
                return Risk.DESTRUCTIVE, "find with -delete/-exec", ["can delete or run commands"]
            return Risk.READ, "find", []
        if prog in ("sed",) and any(a.startswith("-i") for a in low):
            return Risk.MODIFY, "sed -i edits files in place", ["modifies files"]
        if prog in ("sed", "awk", "sort") and not any(a.startswith("-i") for a in low):
            return Risk.READ, prog, []
        if prog in ("env", "printenv"):
            return Risk.READ, prog, ["may print environment values"]
        if prog in MODIFY_PROGRAMS:
            return Risk.MODIFY, f"'{prog}' changes files", ["modifies files"]
        if prog in PROJECT_PROGRAMS:
            if prog == "cargo" or prog == "go":
                sub = low[0] if low else ""
                if sub in ("install", "get", "add", "mod", "publish"):
                    return Risk.NETWORK, f"{prog} {sub}", ["network access"]
            return Risk.PROJECT, f"{prog} runs project tooling", ["runs project code"]
        if prog in READ_PROGRAMS:
            return Risk.READ, f"'{prog}' inspects only", []
        return Risk.MODIFY, f"unrecognised program '{prog}'", ["unknown effects"]

    def _classify_rm(self, args: list[str]) -> tuple[Risk, str, list[str]]:
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
        long_flags = [a for a in args if a.startswith("--")]
        recursive = "r" in flags.lower() or "--recursive" in long_flags
        targets = [a for a in args if not a.startswith("-")]
        for t in targets:
            clean = t.rstrip("/\\") or t
            if clean in ("/", "~", "$HOME", "${HOME}", "*", ".", "..", "/*", "~/*") and recursive:
                return Risk.FORBIDDEN, "recursive delete of a root/home/wildcard target", []
            if t in ("/", "/*") or clean in ("/",):
                return Risk.FORBIDDEN, "deleting the filesystem root", []
        return Risk.DESTRUCTIVE, "rm deletes files", ["deletes files" + (" recursively" if recursive else "")]

    def _classify_git(self, args: list[str]) -> tuple[Risk, str, list[str]]:
        rest = list(args)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        if not rest:
            return Risk.READ, "git", []
        sub, tail = rest[0].lower(), [a.lower() for a in rest[1:]]
        if sub == "push":
            force = any(
                a in ("-f", "--force", "--force-with-lease", "--mirror", "--delete") or a.startswith("+")
                for a in tail
            )
            return (
                Risk.DESTRUCTIVE,
                "git push publishes commits" + (" (FORCE)" if force else ""),
                ["network access", "publishes to a remote" + (" and rewrites history" if force else "")],
            )
        for pattern in DESTRUCTIVE_GIT:
            if sub == pattern[0] and all(p in tail or p == pattern[0] for p in pattern[1:]):
                if sub == "branch" and not any(t in ("-d", "-D", "--delete") for t in [a for a in rest[1:]]):
                    continue
                return (
                    Risk.DESTRUCTIVE,
                    f"git {' '.join(pattern)} can discard work",
                    ["may discard uncommitted work or history"],
                )
        if sub in NETWORK_GIT:
            if sub == "remote" and (not tail or tail[0] in ("-v", "show", "get-url")):
                return Risk.READ, "git remote listing", []
            return Risk.NETWORK, f"git {sub} uses the network", ["network access"]
        if sub in READ_GIT:
            if sub == "config" and any(a for a in tail if not a.startswith("-") and "=" in a):
                return Risk.MODIFY, "git config changes settings", ["modifies git config"]
            return Risk.READ, f"git {sub}", []
        if sub == "branch" and (
            not tail
            or all(
                a.startswith("-") and a in ("-a", "-r", "-v", "-vv", "--list", "--show-current") for a in tail
            )
        ):
            return Risk.READ, "git branch listing", []
        if sub == "stash" and tail[:1] in (["list"], ["show"]):
            return Risk.READ, "git stash listing", []
        if sub == "worktree" and tail[:1] == ["list"]:
            return Risk.READ, "git worktree list", []
        if sub in MODIFY_GIT:
            return Risk.MODIFY, f"git {sub} changes the repository", ["modifies repository state"]
        return Risk.MODIFY, f"git {sub}", ["modifies repository state"]

    def classify_shell(self, command: str) -> tuple[Risk, str, list[str]]:
        try:
            segments, notes = split_shell(command)
        except ValueError:
            return Risk.MODIFY, "could not parse the shell string safely", ["unparseable shell syntax"]
        if re.search(r":\(\)\s*\{.*:\s*\|\s*:", command):
            return Risk.FORBIDDEN, "fork bomb pattern", []
        worst = (Risk.READ, "empty", [])
        progs = [_prog(s[0]) for s in segments]
        if (
            "|" in command
            and any(p in ("curl", "wget", "iwr", "invoke-webrequest") for p in progs)
            and any(
                p in SHELLS or p in INTERPRETERS or p in ("iex", "invoke-expression", "source", "eval")
                for p in progs[1:]
            )
        ):
            return Risk.FORBIDDEN, "downloads and executes code (curl | sh)", []
        reasons: list[str] = []
        effects: list[str] = []
        for seg in segments:
            risk, reason, eff = self.classify_argv(seg)
            reasons.append(reason)
            effects += eff
            if risk > worst[0]:
                worst = (risk, reason, eff)
        if worst[0] is Risk.READ and notes:
            if "command substitution" in notes:
                worst = (Risk.MODIFY, "contains command substitution", [])
                effects.append("hidden commands via substitution")
            elif "redirection" in notes:
                worst = (Risk.MODIFY, "redirects output (can overwrite files)", [])
                effects.append("may write files via redirection")
        elif "redirection" in notes and worst[0] < Risk.MODIFY:
            worst = (Risk.MODIFY, worst[1], worst[2])
        reason = "; ".join(dict.fromkeys(reasons)) or worst[1]
        if notes:
            reason += f" [{', '.join(notes)}]"
        return worst[0], reason, list(dict.fromkeys(effects))

    def _matches(self, command_text: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(command_text, p) for p in patterns)

    def _outside_workspace(self, argv: list[str], cwd: Path | None) -> list[str]:
        if self.workspace_root is None:
            return []
        root = os.path.realpath(self.workspace_root)
        base = os.path.realpath(cwd) if cwd else root
        bad = []
        for arg in argv[1:]:
            if arg.startswith("-") and "=" not in arg:
                continue
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            looks_like_path = (
                value.startswith(("/", "~", "..", "\\"))
                or re.match(r"^[A-Za-z]:[\\/]", value)
                or "/../" in value
            )
            if not looks_like_path:
                continue
            real = os.path.realpath(os.path.join(base, os.path.expanduser(value)))
            try:
                inside = os.path.commonpath([real, root]) == root
            except ValueError:
                inside = False
            if not inside:
                bad.append(value)
        return bad

    def evaluate(
        self,
        *,
        argv: list[str] | None = None,
        shell: str | None = None,
        cwd: Path | None = None,
    ) -> Decision:
        if (argv is None) == (shell is None):
            raise ValueError("pass exactly one of argv / shell")
        text = shell if shell is not None else " ".join(shlex.quote(a) for a in (argv or []))
        if not self.can_execute():
            return Decision("deny", Risk.FORBIDDEN, "read-only mode: command execution is disabled")
        if self._matches(text, self.settings.deny):
            return Decision("deny", Risk.FORBIDDEN, "matches a configured deny rule")
        if shell is not None:
            if not self.settings.allow_shell_strings:
                return Decision("deny", Risk.FORBIDDEN, "shell strings are disabled; use an argument list")
            risk, reason, effects = self.classify_shell(shell)
            first = split_shell(shell)[0][0] if _safe_split(shell) else None
        else:
            risk, reason, effects = self.classify_argv(argv or [])
            first = (argv or [None])[0]
        if risk is Risk.FORBIDDEN:
            return Decision("deny", risk, reason, effects)
        outside = self._outside_workspace(argv, cwd) if argv else []
        if outside:
            effects = [*effects, f"touches paths outside the workspace: {', '.join(outside[:3])}"]
            if self.mode is PermissionMode.RESTRICTED_AUTONOMOUS:
                return Decision("deny", risk, "workspace restriction: " + effects[-1], effects)
            risk = max(risk, Risk.MODIFY)
        if risk is Risk.NETWORK and self.settings.network == "deny":
            return Decision("deny", risk, f"network access is disabled by policy ({reason})", effects)
        key = text.strip()
        if key in self._session_allowed and risk < Risk.DESTRUCTIVE:
            return Decision("allow", risk, "approved earlier this session", effects)
        mode = self.mode
        if mode is PermissionMode.APPROVE_ALL:
            return Decision("ask", risk, reason, effects)
        user_allowed = self._matches(text, self.settings.allow)
        if risk is Risk.DESTRUCTIVE:
            return Decision("ask", risk, reason, effects)
        if mode is PermissionMode.APPROVE_RISKY:
            if risk is Risk.READ and not outside:
                return Decision("allow", risk, reason, effects)
            if user_allowed and risk <= Risk.PROJECT:
                return Decision("allow", risk, f"allow-listed ({reason})", effects)
            return Decision("ask", risk, reason, effects)
        if risk <= Risk.PROJECT or (user_allowed and risk <= Risk.MODIFY):
            return Decision("allow", risk, reason, effects)
        if (
            risk is Risk.MODIFY
            and first
            and _prog(first) in {"mkdir", "touch", "cp", "mv", "git"}
            and shell is None
        ):
            return Decision("allow", risk, reason, effects)
        if risk is Risk.NETWORK and self.settings.network == "allow":
            return Decision("ask", risk, reason + " (network installs always need approval)", effects)
        return Decision("ask", risk, reason, effects)


def _safe_split(command: str) -> bool:
    try:
        return bool(split_shell(command)[0])
    except ValueError:
        return False
