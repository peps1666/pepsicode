"""Small, keyboard-driven menus for slash commands."""

from __future__ import annotations

from dataclasses import dataclass, field

from pepsicode.tui.chrome import ACCENT, BOLD, RESET, SUBTLE, truncate_plain


@dataclass(frozen=True)
class CommandOption:
    label: str
    value: str
    description: str = ""
    draft: bool = False


@dataclass
class CommandPicker:
    title: str
    options: list[CommandOption] = field(default_factory=list)
    selected: int = 0
    note: str = ""


def render_command_picker(picker: CommandPicker, cols: int, rows: int) -> list[str]:
    width, height = max(8, cols - 2), max(5, rows)

    def clean(text: str) -> str:
        return "".join(char if char.isprintable() else " " for char in text)

    header = [f"  {BOLD}{clean(picker.title)}{RESET}"]
    if picker.note:
        header.append(f"  {SUBTLE}{clean(picker.note)}{RESET}")
    available = max(1, height - len(header) - 2)
    selected = min(picker.selected, max(0, len(picker.options) - 1))
    start = max(0, min(selected - available // 2, len(picker.options) - available))
    lines = []
    for index in range(start, min(len(picker.options), start + available)):
        option = picker.options[index]
        line = f"  {'>' if index == selected else ' '} {clean(option.label)}"
        if option.description:
            line += f"  ·  {clean(option.description)}"
        lines.append(f"{ACCENT if index == selected else SUBTLE}{line}{RESET}")
    if not lines:
        lines = [f"  {SUBTLE}没有可选择的项目{RESET}"]
    result = header + lines
    result += [""] * max(0, height - len(result) - 1)
    result.append(f"  {SUBTLE}↑/↓ 选择 · Enter 确认 · Esc 取消{RESET}")
    return [truncate_plain(line, width) for line in result[:height]]
