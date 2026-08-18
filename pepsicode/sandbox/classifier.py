"""Dangerous-command classification.

Migrated verbatim from the old ``permissions.py`` so the classifier can be
reused by both the approval layer (to decide whether to prompt) and future
sandbox backends (to refuse certain commands outright).
"""

from __future__ import annotations


def format_command_signature(command: str, args: list[str]) -> str:
    return " ".join([command, *args]).strip()


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
