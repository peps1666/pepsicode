"""Markdown prompt commands; expansion never evaluates shell or Python code."""

from __future__ import annotations

import re
import shlex
import threading
import time
from pathlib import Path

from pepsicode.cli.poly_commands import CommandMetadata, PromptCommand
from pepsicode.context.skills import split_frontmatter
from pepsicode.input_limits import MAX_EXPANDED_INPUT_CHARACTERS, MAX_INPUT_CHARACTERS
from pepsicode.types import ChatMessage

MAX_TEMPLATE_BYTES = 65_536
MAX_COMMANDS = 256
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_PARAMETER = re.compile(r"\$\$|\$\{([1-9][0-9]*|@|ARGUMENTS):-([^}]*)\}|\$(ARGUMENTS\b|@|[1-9][0-9]*)")


def expand_arguments(body: str, arguments: str) -> str:
    if len(arguments) > MAX_INPUT_CHARACTERS:
        raise ValueError(f"Command arguments exceed {MAX_INPUT_CHARACTERS} characters")
    lexer = shlex.shlex(arguments, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    # Preserve Windows paths. Quotes group arguments; backslashes are literal.
    lexer.escape = ""
    try:
        values = list(lexer)
    except ValueError as error:
        raise ValueError(f"Invalid command arguments: {error}") from error

    def replace(match: re.Match) -> str:
        if match.group(0) == "$$":
            return "$"
        key = match.group(1) or match.group(3)
        if key in {"@", "ARGUMENTS"}:
            value = " ".join(values)
        else:
            index = int(key) - 1 if len(key) < 10 else len(values)
            value = values[index] if index < len(values) else ""
        return value or match.group(2) or ""

    expanded = _PARAMETER.sub(replace, body)
    if not expanded.strip():
        raise ValueError("Expanded command is empty; supply arguments or a default value")
    if len(expanded) > MAX_EXPANDED_INPUT_CHARACTERS:
        raise ValueError(f"Expanded command exceeds {MAX_EXPANDED_INPUT_CHARACTERS} characters")
    return expanded


class MarkdownPromptCommand(PromptCommand):
    def __init__(self, name: str, description: str, hint: str, body: str, path: Path, source: str):
        self.body = body
        self.path = str(path)
        self.source = source
        self.argument_hint = hint
        super().__init__(
            CommandMetadata(name=name, description=description, usage=f"{name} {hint}".rstrip(), paths=[str(path)]),
            self.expand,
        )

    def expand(self, arguments: str, context: dict | None = None) -> str:
        return expand_arguments(self.body, arguments)

    def message(self, raw_input: str, arguments: str) -> ChatMessage:
        return {
            "role": "user",
            "content": self.expand(arguments),
            "displayContent": raw_input,
            "commandPath": self.path,
        }


class PromptCommandCatalog:
    def __init__(self, cwd: str, home: str, reserved: set[str]):
        self.cwd = Path(cwd)
        self.home = Path(home)
        self.reserved = {name.casefold() for name in reserved}
        self.commands: dict[str, MarkdownPromptCommand] = {}
        self.errors: dict[str, str] = {}
        self.diagnostics: list[str] = []
        self._loaded_at = float("-inf")
        self._lock = threading.RLock()

    def refresh(self, *, force: bool = False) -> PromptCommandCatalog:
        with self._lock:
            if not force and time.monotonic() - self._loaded_at < 1:
                return self
            self.commands, self.errors, self.diagnostics = {}, {}, []
            claimed = set()
            roots = [
                (self.cwd / ".pepsi-code" / "commands", "project", self.cwd),
                (self.home / ".pepsi-code" / "commands", "user", self.home),
            ]
            count = 0
            for root, source, anchor in roots:
                try:
                    if not root.exists():
                        continue
                    resolved_root = root.resolve()
                    if not resolved_root.is_relative_to(anchor.resolve()):
                        raise ValueError("command directory resolves outside its project or user directory")
                    paths = sorted(root.glob("*.md"), key=lambda path: path.name)
                    for path in paths:
                        count += 1
                        if count > MAX_COMMANDS:
                            self.diagnostics.append(f"Command scan limit ({MAX_COMMANDS}) reached at {root}")
                            break
                        name = f"/{path.stem}"
                        if name in claimed:
                            continue
                        claimed.add(name)
                        try:
                            if not _NAME.fullmatch(path.stem):
                                raise ValueError("filename must use 1–64 letters, digits, underscores or hyphens")
                            if name.casefold() in self.reserved:
                                raise ValueError("reserved built-in command name")
                            if path.resolve().parent != resolved_root:
                                raise ValueError("command file resolves outside its commands directory")
                            with path.open("rb") as stream:
                                raw = stream.read(MAX_TEMPLATE_BYTES + 1)
                            if len(raw) > MAX_TEMPLATE_BYTES:
                                raise ValueError(f"template exceeds {MAX_TEMPLATE_BYTES} bytes")
                            markdown = raw.decode("utf-8-sig").replace("\r\n", "\n")
                            fields, body = {}, markdown
                            if markdown.startswith("---\n"):
                                lines = markdown.splitlines()
                                end = next((index for index in range(1, len(lines)) if lines[index] == "---"), None)
                                if end is None:
                                    raise ValueError("frontmatter has no closing --- line")
                                fields, _ = split_frontmatter("\n".join(lines[: end + 1]))
                                body = "\n".join(lines[end + 1 :])
                            body = body.strip()
                            if not body:
                                raise ValueError("template body is empty")
                            description = fields.get("description") or next(
                                (
                                    line.strip()
                                    for line in body.splitlines()
                                    if line.strip() and not line.startswith("#")
                                ),
                                path.stem,
                            )
                            self.commands[name] = MarkdownPromptCommand(
                                name,
                                description[:240],
                                fields.get("argument-hint", "")[:120],
                                body,
                                path.resolve(),
                                source,
                            )
                        except (OSError, ValueError) as error:
                            self.errors[name] = f"{path}: {error}"
                            self.diagnostics.append(self.errors[name])
                except (OSError, ValueError) as error:
                    self.diagnostics.append(f"{root}: {error}")
                if count > MAX_COMMANDS:
                    break
            self._loaded_at = time.monotonic()
            return self

    def expand(self, raw_input: str) -> ChatMessage | None:
        parts = raw_input.strip().split(maxsplit=1)
        if not parts or not parts[0].startswith("/") or parts[0].casefold() in self.reserved:
            return None
        with self._lock:
            self.refresh(force=True)
            name = parts[0]
            if name in self.errors:
                raise ValueError(f"Cannot load command {name}: {self.errors[name]}")
            command = self.commands.get(name)
            return command.message(raw_input, parts[1] if len(parts) > 1 else "") if command else None
