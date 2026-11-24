"""Small, keyboard-driven menus for slash commands."""

from __future__ import annotations

from dataclasses import dataclass, field

from pepsicode.tui.chrome import (
    ACCENT,
    BOLD,
    BORDER,
    BRAND_LIME,
    BRIGHT_WHITE,
    HIGHLIGHT_BG,
    RESET,
    SUBTLE,
    TEXT,
    center_box_frame,
    paint_row,
    render_box,
    render_divider,
    truncate_plain,
)


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
    cols, rows = max(10, cols), max(5, rows)
    box_w = min(max(8, cols - 2), 82)
    content_w = max(4, box_w - 4) if box_w >= 10 else box_w
    compact = rows < 11 or box_w < 30

    def clean(text: str) -> str:
        return "".join(char if char.isprintable() else " " for char in text)

    header: list[str] = []
    if picker.note:
        header.append(f"{SUBTLE}{clean(picker.note)}{RESET}")
        if not compact:
            header.append(render_divider(content_w))

    border_overhead = 2 if box_w >= 10 else 0
    available = max(1, rows - border_overhead - len(header))
    selected = min(picker.selected, max(0, len(picker.options) - 1))
    start = max(0, min(selected - available // 2, len(picker.options) - available))
    lines: list[str] = []
    for index in range(start, min(len(picker.options), start + available)):
        option = picker.options[index]
        label = clean(option.label)
        desc = f"  ·  {clean(option.description)}" if option.description else ""
        if index == selected:
            row_txt = f"{ACCENT}{BOLD}>{RESET} {BRIGHT_WHITE}{BOLD}{label}{RESET}{SUBTLE}{desc}{RESET}"
            lines.append(paint_row(truncate_plain(row_txt, content_w), content_w, HIGHLIGHT_BG))
        else:
            lines.append(truncate_plain(f"  {TEXT}{label}{RESET}{SUBTLE}{desc}{RESET}", content_w))
    if not lines:
        lines = [f"{SUBTLE}没有可选择的项目{RESET}"]

    counter = f"{selected + 1 if picker.options else 0}/{len(picker.options)}"
    boxed = render_box(
        header + lines,
        box_w,
        title=f"{BRAND_LIME}◆ {BOLD}{clean(picker.title)}{RESET}",
        right_title=f"{SUBTLE}{counter}{RESET}",
        footer=f"{SUBTLE}↑/↓ 选择 · Enter 确认 · Esc 取消{RESET}",
        border_color=BORDER,
    )
    if len(boxed) > rows:
        return [truncate_plain(line, max(1, cols - 1)) for line in boxed[:rows]]
    return center_box_frame(boxed, cols, rows)
