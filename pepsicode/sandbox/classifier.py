"""Dangerous-command and Codex-style auto-approval classification.

Migrated verbatim from the old ``permissions.py`` so the classifier can be
reused by both the approval layer (to decide whether to prompt) and future
sandbox backends (to refuse certain commands outright).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath


def format_command_signature(command: str, args: list[str]) -> str:
    return " ".join([command, *args]).strip()


def _command_stem(command: str) -> str:
    """Return a normalized lowercase executable stem on both Windows and POSIX."""
    cleaned = command.strip().strip('"').strip("'")
    if not cleaned:
        return ""
    if "\\" in cleaned or (len(cleaned) >= 2 and cleaned[1] == ":"):
        name = PureWindowsPath(cleaned).name.lower()
    else:
        name = PurePosixPath(cleaned).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tokenize_segment(segment: str) -> list[str]:
    """Quote-aware tokenizer that preserves Windows backslashes."""
    text = segment.strip()
    if not text:
        return []
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in {"'", '"'}:
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _split_shell_segments(snippet: str) -> list[str]:
    """Split a shell command string on control operators while respecting quotes."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    length = len(snippet)
    while i < length:
        ch = snippet[i]
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            i += 1
            continue
        if snippet[i : i + 2] in {"&&", "||"}:
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 2
            continue
        if ch in {";", "|", "&", "\n", "\r"}:
            seg = "".join(current).strip()
            if seg:
                segments.append(seg)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        segments.append(tail)
    return segments


def _unwrap_cmd_payload(payload: str) -> str:
    """Unwrap the outer quotes added by Windows ``cmd.exe /s /c`` shims."""
    text = payload.strip()
    if len(text) >= 4 and text.startswith('""') and text.endswith('""'):
        return text[1:-1].strip()
    return text


def _extract_shell_payload(stem: str, args: list[str]) -> tuple[str | None, str | None]:
    """Return ``(payload, immediate_risk)`` for a shell invocation."""
    lowered = [arg.lower() for arg in args]
    if stem == "cmd":
        for idx, flag in enumerate(lowered):
            if flag in {"/c", "/k"}:
                raw = " ".join(args[idx + 1 :]).strip()
                return (_unwrap_cmd_payload(raw) if raw else None), None
        return None, None
    if stem in {"bash", "sh", "zsh", "fish"}:
        for idx, flag in enumerate(lowered):
            if flag in {"-c", "-lc", "-ec", "-xc"} or (
                flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]
            ):
                return (args[idx + 1] if idx + 1 < len(args) else None), None
        return None, None
    if stem in {"powershell", "pwsh"}:
        for idx, flag in enumerate(lowered):
            if flag in {"-encodedcommand", "-enc", "-ec"}:
                return None, "encoded PowerShell command cannot be auto-approved"
            if flag in {"-command", "-c"}:
                raw = " ".join(args[idx + 1 :]).strip()
                return (raw or None), None
        return None, None
    return None, None


_GIT_OPTIONS_WITH_VALUE = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
)
_GIT_FLAG_OPTIONS = frozenset(
    {
        "--bare",
        "--no-pager",
        "-p",
        "--paginate",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
    }
)


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    """Extract the git subcommand and its arguments, skipping global git flags."""
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token in _GIT_OPTIONS_WITH_VALUE:
            idx += 2
            continue
        if token in _GIT_FLAG_OPTIONS or any(
            token.startswith(prefix)
            for prefix in ("--git-dir=", "--work-tree=", "--namespace=", "--config-env=", "--super-prefix=")
        ):
            idx += 1
            continue
        if token.startswith("-c") and len(token) > 2:
            idx += 1
            continue
        if token.startswith("-C") and len(token) > 2:
            idx += 1
            continue
        return token.lower(), args[idx + 1 :]
    return "", []


_DOCKER_READ_ONLY_SUBCOMMANDS = frozenset({"ps", "images", "logs", "inspect", "version", "info", "stats"})
_GH_MUTATING_SUBCOMMANDS = frozenset(
    {"create", "merge", "close", "delete", "edit", "comment", "review", "release", "api"}
)
_NETWORK_OR_REMOTE_COMMANDS = frozenset(
    {"curl", "wget", "ssh", "scp", "sftp", "rsync", "ftp", "nc", "ncat", "netcat", "telnet"}
)
_PRIVILEGE_OR_SYSTEM_COMMANDS = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "runas",
        "gsudo",
        "pkexec",
        "shutdown",
        "reboot",
        "poweroff",
        "halt",
        "csrutil",
        "dscl",
        "bcdedit",
        "icacls",
        "takeown",
        "taskkill",
        "killall",
        "pkill",
    }
)
_DISK_DESTRUCTIVE_COMMANDS = frozenset({"dd", "mkfs", "fdisk", "format", "diskpart", "diskutil", "shred", "wipefs"})
_POWERSHELL_RISKY_CMDLETS = frozenset(
    {
        "invoke-expression",
        "iex",
        "invoke-webrequest",
        "iwr",
        "invoke-restmethod",
        "irm",
        "set-executionpolicy",
        "stop-computer",
        "restart-computer",
        "clear-disk",
        "format-volume",
        "stop-process",
    }
)
_EXTERNAL_REDIRECT_RE = re.compile(
    r"(?:^|\s)[0-9]*>{1,2}\s*(?:/etc/|/usr/|/bin/|/sbin/|/System/|C:\\Windows\\|\.\.[/\\])", re.I
)


def _classify_auto_single(command: str, args: list[str], *, depth: int) -> str | None:
    if depth > 3:
        return f"deeply nested shell command requires approval ({format_command_signature(command, args)})"

    stem = _command_stem(command)
    if not stem:
        return None
    normalized_args = [arg.strip() for arg in args if arg.strip()]
    lower_args = [arg.lower() for arg in normalized_args]
    signature = format_command_signature(command, normalized_args)

    # Shell wrappers: inspect every sub-command inside the shell payload.
    if stem in {"cmd", "bash", "sh", "zsh", "fish", "powershell", "pwsh"}:
        payload, immediate_risk = _extract_shell_payload(stem, normalized_args)
        if immediate_risk:
            return f"{immediate_risk} ({signature})"
        if payload:
            if _EXTERNAL_REDIRECT_RE.search(payload):
                return f"shell redirection targets a system or external path ({signature})"
            for segment in _split_shell_segments(payload):
                tokens = _tokenize_segment(segment)
                if not tokens:
                    continue
                risk = _classify_auto_single(tokens[0], tokens[1:], depth=depth + 1)
                if risk:
                    return risk
            return None
        return None

    # PowerShell high-risk cmdlets (can appear directly or inside powershell -Command).
    if stem in _POWERSHELL_RISKY_CMDLETS:
        return f"{stem} performs a high-risk network, system, or arbitrary-evaluation action ({signature})"
    if stem in {"remove-item", "ri"}:
        if any(flag.startswith(("-r", "-fo")) or flag in {"-recurse", "-force"} for flag in lower_args):
            return f"recursive or forced Remove-Item can delete files irreversibly ({signature})"
    if stem == "start-process" and "runas" in lower_args:
        return f"Start-Process -Verb RunAs elevates privileges ({signature})"

    # Git: allow local workspace git operations; prompt on remote push or destructive history/tree actions.
    if stem == "git":
        sub, sub_args = _git_subcommand(normalized_args)
        sub_lower = [arg.lower() for arg in sub_args]
        if sub == "push":
            return f"git push modifies a remote repository ({signature})"
        if sub == "reset" and "--hard" in sub_lower:
            return f"git reset --hard can discard local changes ({signature})"
        if sub == "clean" and not any(flag in {"-n", "--dry-run"} for flag in sub_lower):
            return f"git clean can delete untracked files ({signature})"
        if sub == "checkout" and "--" in sub_args:
            return f"git checkout -- can overwrite working tree files ({signature})"
        if sub == "restore" and any(arg == "-s" or arg.startswith("--source") for arg in sub_lower):
            return f"git restore --source can overwrite local files ({signature})"
        if sub == "branch" and "-D" in sub_args:
            return f"git branch -D force-deletes a branch ({signature})"
        if sub == "config" and any(flag in {"--global", "--system"} for flag in sub_lower):
            return f"git config --global/--system modifies configuration outside the repository ({signature})"
        if sub == "remote" and any(action in {"set-url", "remove", "rm"} for action in sub_lower):
            return f"git remote {sub_args[0] if sub_args else ''} changes repository remote configuration ({signature})"
        return None

    # Destructive filesystem & disk operations.
    if stem == "rm":
        combined_flags = "".join(
            arg[1:] for arg in normalized_args if arg.startswith("-") and not arg.startswith("--")
        ).lower()
        has_recursive = "r" in combined_flags or "--recursive" in lower_args
        has_force = "f" in combined_flags or "--force" in lower_args
        targets = [arg for arg in normalized_args if not arg.startswith("-")]
        if (has_recursive and has_force) or any(
            t in {"/", "/*", "~", ".."} or t.startswith("../") or t.startswith("..\\") for t in targets
        ):
            return f"rm can irreversibly delete files ({signature})"
        return None
    if stem in {"rmdir", "rd"} and any(
        flag in {"/s", "-r", "--recursive"} or flag.startswith("/s") for flag in lower_args
    ):
        return f"{stem} /s recursively deletes directories ({signature})"
    if stem in {"del", "erase"} and any(flag in {"/s", "/f"} for flag in lower_args):
        return f"{stem} with /s or /f can delete files irreversibly ({signature})"
    if stem in _DISK_DESTRUCTIVE_COMMANDS or stem.startswith("mkfs."):
        return f"{stem} can modify or destroy disk data ({signature})"
    if stem == "chmod" and (
        "777" in normalized_args or any(arg.endswith("777") for arg in normalized_args) or "-R" in normalized_args
    ):
        return f"chmod {signature} makes broad permission changes"
    if stem == "chown":
        return f"chown changes file ownership ({signature})"

    # Privilege escalation and OS-level administration.
    if stem in _PRIVILEGE_OR_SYSTEM_COMMANDS:
        return f"{stem} performs privileged or system-level actions ({signature})"
    if stem == "kill" and any(flag in {"-9", "-kill", "-sigkill"} for flag in lower_args):
        return f"kill -9 forcefully terminates processes ({signature})"
    if stem == "defaults" and any(action in {"write", "delete"} for action in lower_args):
        return f"defaults modifies system preferences ({signature})"
    if stem == "launchctl" and any(action in {"unload", "bootout", "disable"} for action in lower_args):
        return f"launchctl can disable system services ({signature})"
    if stem == "systemctl" and any(action in {"stop", "disable", "mask", "restart"} for action in lower_args):
        return f"systemctl modifies system services ({signature})"
    if stem == "reg" and any(action in {"add", "delete", "import"} for action in lower_args):
        return f"reg modifies the Windows registry ({signature})"
    if stem == "sc" and any(action in {"stop", "delete", "config"} for action in lower_args):
        return f"sc modifies Windows services ({signature})"
    if stem == "net" and any(action in {"user", "localgroup"} for action in lower_args):
        return f"net modifies local accounts or groups ({signature})"

    # Network / remote transfer / publishing / external CLIs.
    if stem in _NETWORK_OR_REMOTE_COMMANDS:
        return f"{stem} accesses external network or remote hosts ({signature})"
    if stem in {"npm", "pnpm", "yarn", "bun"} and any(action in {"publish", "unpublish"} for action in lower_args):
        return f"{stem} publish/unpublish affects an external package registry ({signature})"
    if stem == "cargo" and any(action in {"publish", "yank"} for action in lower_args):
        return f"cargo publish/yank affects an external package registry ({signature})"
    if stem == "twine" and "upload" in lower_args:
        return f"twine upload publishes packages externally ({signature})"
    if stem in {"poetry", "uv"} and "publish" in lower_args:
        return f"{stem} publish affects an external package registry ({signature})"
    if stem == "gem" and "push" in lower_args:
        return f"gem push publishes packages externally ({signature})"
    if stem == "gh" and any(arg in _GH_MUTATING_SUBCOMMANDS for arg in lower_args):
        return f"gh modifies external GitHub resources ({signature})"
    if stem in {"docker", "podman"}:
        sub = next((arg for arg in lower_args if not arg.startswith("-")), "")
        if sub not in _DOCKER_READ_ONLY_SUBCOMMANDS:
            return f"{stem} {sub or 'command'} affects containers or host daemon state ({signature})"

    return None


def classify_auto_command_risk(command: str, args: list[str]) -> str | None:
    """Return a prompt reason if a command is high-risk in Codex-style ``auto`` mode.

    Normal workspace development commands (tests, linters, builds, local git
    operations, and read/inspection commands) return ``None`` and are
    auto-approved in ``auto`` mode when their working directory is inside the
    workspace. Destructive, privileged, external-network, publishing, and
    remote-mutating commands return a human-readable reason and still require
    user confirmation.
    """
    return _classify_auto_single(command, args, depth=0)


def classify_dangerous_command(command: str, args: list[str]) -> str | None:
    """Return a human-readable reason if the command is dangerous, else ``None``.

    The logic is identical to the previous ``_classify_dangerous_command``
    helper; only the name changed (dropped the leading underscore) to mark
    it as a public, reusable function.
    """
    normalized_args = [arg.strip() for arg in args if arg.strip()]
    signature = format_command_signature(command, normalized_args)

    if command == "git":
        if "reset" in normalized_args and "--hard" in normalized_args:
            return f"git reset --hard can discard local changes ({signature})"
        if "clean" in normalized_args:
            return f"git clean can delete untracked files ({signature})"
        if "checkout" in normalized_args and "--" in normalized_args:
            return f"git checkout -- can overwrite working tree files ({signature})"
        if "push" in normalized_args and any(arg in {"--force", "-f"} for arg in normalized_args):
            return f"git push --force rewrites remote history ({signature})"
        if "restore" in normalized_args and any(arg.startswith("--source") for arg in normalized_args):
            return f"git restore --source can overwrite local files ({signature})"

    if command == "npm" and "publish" in normalized_args:
        return f"npm publish affects a registry outside this machine ({signature})"

    if command == "rm":
        # Combine all flags (supports -rf, -fr, -Rf, -r -f, etc.)
        combined_flags = "".join(arg for arg in normalized_args if arg.startswith("-")).lower()
        # Check whether both the recursive and force flags are present
        if "r" in combined_flags and "f" in combined_flags:
            # Check whether it targets the root directory or uses --no-preserve-root
            if any(arg in {"/", "/*"} for arg in normalized_args) or "--no-preserve-root" in normalized_args:
                return f"rm -rf can cause catastrophic data loss ({signature})"
            # Even when not targeting root, still flag it as dangerous
            return f"rm -rf can cause catastrophic data loss ({signature})"

    if command in {"dd", "mkfs", "mkfs.ext4", "mkfs.vfat", "fdisk", "format"}:
        return f"{command} can modify or destroy disk partitions ({signature})"

    if command == "chmod":
        if "777" in normalized_args or any(arg.endswith("777") for arg in normalized_args):
            return f"chmod 777 opens permissions to all users ({signature})"

    if command in {
        "node",
        "python",
        "python3",
        "pythonw",
        "bun",
        "bash",
        "sh",
        "zsh",
        "fish",
        "powershell",
        "pwsh",
    }:
        return f"{command} can execute arbitrary local code ({signature})"

    # macOS-specific dangerous commands
    if command == "diskutil":
        return f"diskutil can erase or partition disks ({signature})"
    if command == "csrutil":
        return f"csrutil modifies System Integrity Protection ({signature})"
    if command == "defaults" and "write" in normalized_args:
        return f"defaults write modifies system preferences ({signature})"
    if command == "launchctl" and any(arg in {"unload", "bootout", "disable"} for arg in normalized_args):
        return f"launchctl can disable system services ({signature})"
    if command == "dscl":
        return f"dscl can modify directory services and user accounts ({signature})"

    return None
