"""Bounded, passive Agent status view for the terminal's right-hand pane."""

from pepsicode.agents.status import assignment_summary, display_text, report_summary, stage_label
from pepsicode.tui.chrome import (
    ACCENT,
    BOLD,
    BRAND_LIME,
    BRIGHT_RED,
    BRIGHT_WHITE,
    GREEN,
    RESET,
    SUBTLE,
    TEXT,
    TEXT_FAINT,
    YELLOW,
    render_divider,
    truncate_plain,
    wrap_text,
)


def panel_width(cols: int, height: int) -> int:
    # Keep at least 70 columns for the conversation, including the separator.
    return min(44, cols // 3) if cols >= 110 and height >= 10 else 0


def _agent_status_badge(row: dict) -> tuple[str, str]:
    stage = row["stage"]
    if stage == "failed":
        return BRIGHT_RED, "✖"
    if stage in {"budget_exhausted", "waiting_user", "waiting_approval"}:
        return YELLOW, "●"
    if row["ended_at"] is None and not row["historical"] and stage not in {"queued", "recovery_pending"}:
        return BRAND_LIME, "●"
    if stage == "completed":
        return GREEN, "✓"
    return SUBTLE, "·"


def render_agent_panel(view: dict, width: int, height: int, offset: int = 0) -> tuple[list[str], dict]:
    records = view["records"]
    offset = min(max(0, offset), max(0, len(records) - 1))
    active = sum(
        row["ended_at"] is None and not row["historical"] and row["stage"] not in {"queued", "recovery_pending"}
        for row in records
    )
    active_text = f"{BRAND_LIME}● {active} 个执行中{RESET}" if active else f"{SUBTLE}0 个执行中{RESET}"
    lines = [
        f"{BRAND_LIME}◆{RESET} {BRIGHT_WHITE}{BOLD}子 Agent{RESET}  {active_text}",
        f"{TEXT_FAINT}自动更新 · 点击名称查看详情{RESET}",
    ]
    if height >= 10:
        lines.append(render_divider(width))
    if view["persistence_error"]:
        lines.append(f"{YELLOW}保存失败：{display_text(view['persistence_error'])}{RESET}")
    actions = {}
    shown = 0
    for row in records[offset:]:
        available = height - 2 - len(lines)
        if available < 4:
            break
        actions[len(lines)] = ("detail", row["trace_id"])
        title = f"{row['name']} · {row['trace_id'][:8]}"
        color, icon = _agent_status_badge(row)
        block = [
            f"{color}{icon}{RESET} {BRIGHT_WHITE}{BOLD}{display_text(title)}{RESET}",
            f"  {color}{stage_label(row)}{RESET} {TEXT_FAINT}·{RESET} {SUBTLE}{round(row['elapsed_seconds'])}秒{RESET}"
            + (f" {TEXT_FAINT}· 历史{RESET}" if row["historical"] else ""),
            *[
                f"  {TEXT}{line}{RESET}"
                for line in wrap_text(assignment_summary(row["task"]), max(4, width - 2)).splitlines()[:2]
            ],
        ]
        questions = [q for q in row["questions"] if q["actionable"]]
        if questions:
            block.append(f"  {YELLOW}待回复：{display_text(questions[0]['question'])}{RESET}")
        if row["activities"]:
            block.append(f"  {SUBTLE}当前：{display_text(row['activities'][-1]['summary'])}{RESET}")
        summary = report_summary(row)
        if summary:
            block.append(f"  {TEXT}报告：{summary}{RESET}")
        elif row["end_reason"] and row["stage"] not in {"completed", "budget_exhausted"}:
            block.append(f"  {YELLOW}原因：{display_text(row['end_reason'])}{RESET}")
        proof = row["acceptance"]
        if proof["total"]:
            block.append(f"  {SUBTLE}父任务验收 {proof['passed']}/{proof['total']}{RESET}")
        lines.extend(block[:available])
        if len(lines) < height - 2:
            lines.append(render_divider(width))
        else:
            lines.append("")
        shown += 1
    lines = lines[: height - 1]
    lines += [""] * max(0, height - 1 - len(lines))
    if offset + shown < len(records):
        lines.append(f"{ACCENT}下一页 ›{RESET}  {SUBTLE}· {offset + 1}–{offset + shown}/{len(records)}{RESET}")
        actions[height - 1] = ("page", offset + shown)
    elif offset:
        lines.append(f"{ACCENT}回到最新执行 ↑{RESET}")
        actions[height - 1] = ("page", 0)
    else:
        lines.append(f"{TEXT_FAINT}保留本会话最近的执行记录{RESET}")
    return [truncate_plain(line, width) for line in lines[:height]], actions


def render_agent_strip(view: dict, width: int) -> list[str]:
    """Narrow terminals still show updates without a slash command."""
    records = view["records"]
    parts = [f"{TEXT}{row['name']}{RESET} {SUBTLE}{stage_label(row)}{RESET}" for row in records[:2]]
    lines = [
        f"{BRAND_LIME}◆{RESET} {BOLD}子 Agent{RESET} {TEXT_FAINT}·{RESET} " + f" {TEXT_FAINT}/{RESET} ".join(parts)
    ]
    if view["persistence_error"]:
        lines.append(f"{YELLOW}状态保存失败：{display_text(view['persistence_error'])}{RESET}")
    elif records:
        row = records[0]
        questions = [q for q in row["questions"] if q["actionable"]]
        detail = (
            f"{YELLOW}待回复：{display_text(questions[0]['question'])}{RESET}"
            if questions
            else f"{SUBTLE}{display_text(row['activities'][-1]['summary'])}{RESET}"
            if row["activities"]
            else f"{SUBTLE}{report_summary(row) or assignment_summary(row['task'])}{RESET}"
        )
        lines.append(detail)
    return ["  " + truncate_plain(line, max(1, width - 4)) for line in lines]
