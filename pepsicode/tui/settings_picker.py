"""Keyboard model / effort pickers. Rendering is independent of terminal IO."""

from __future__ import annotations

from dataclasses import dataclass

from pepsicode.llm.model_settings import model_capabilities, model_catalog, model_display_name
from pepsicode.llm.providers import PROVIDERS, provider_catalog
from pepsicode.tui.chrome import (
    ACCENT,
    BOLD,
    BORDER,
    BRAND_LIME,
    BRIGHT_RED,
    BRIGHT_WHITE,
    HIGHLIGHT_BG,
    RESET,
    SUBTLE,
    TEXT,
    TEXT_FAINT,
    YELLOW,
    center_box_frame,
    paint_row,
    render_box,
    render_divider,
    truncate_plain,
    wrap_text,
)

EFFORT_LABELS = {
    "default": ("默认", "由服务端决定思考方式与强度"),
    "off": ("关闭思考", "不启用额外思考"),
    "low": ("低", "速度优先，适合简单任务"),
    "medium": ("中", "兼顾响应速度与推理深度"),
    "high": ("高", "适合复杂分析与编程任务"),
    "xhigh": ("超高", "更深入地处理复杂任务"),
    "max": ("最高", "使用最高支持强度，可能增加耗时和费用"),
}

PERMISSION_OPTIONS = [
    ("ask", "每次询问", "编辑文件、执行本地命令、项目外访问和外部工具均需确认"),
    ("auto", "自动审批", "项目内编辑与常规命令自动放行；高风险命令、项目外访问和外部工具需确认"),
    ("full_access", "完全信任（高风险）", "文件、命令及外部工具均免权限确认"),
]


def create_permissions_picker(current: str) -> SettingsPicker:
    options = [SettingOption(*option) for option in PERMISSION_OPTIONS]
    options.append(SettingOption("__workspace_default__", "设置工作区默认", "选择新会话默认权限，不改变当前会话"))
    return SettingsPicker(
        kind="permissions",
        model="",
        options=options,
        current=current,
        selected=next((i for i, option in enumerate(options) if option.value == current), 0),
    )


@dataclass(frozen=True)
class SettingOption:
    value: str
    label: str
    description: str


@dataclass
class SettingsPicker:
    kind: str
    model: str
    options: list[SettingOption]
    current: str
    selected: int = 0
    persist: bool = False
    notice: str = ""
    error: str = ""
    return_input: str = ""
    provider: str | None = None
    provider_name: str = ""


def create_settings_picker(runtime: dict, kind: str, *, notice: str = "", return_input: str = "") -> SettingsPicker:
    current = (
        runtime.get("provider", "")
        if kind == "provider"
        else runtime["model"]
        if kind == "model"
        else runtime.get("reasoningEffort", "default")
    )
    if kind == "provider":
        options = [
            SettingOption(
                entry["id"],
                entry["name"],
                "独立 API Key 已配置" if entry["configured"] else "未配置，选择后填写 API Key",
            )
            for entry in provider_catalog(runtime)
        ]
        options.append(SettingOption("__add__", "+ 自定义厂商", "逐步配置其他 Anthropic 兼容服务商"))
    elif kind == "model":
        options = [
            SettingOption(
                item["id"],
                item["name"],
                f"上下文：{item.get('contextWindow', '未提供')}({item.get('limitSources', {}).get('contextWindow', '未知')}) · 最大输出：{item.get('maxOutputTokens', '未提供')}({item.get('limitSources', {}).get('maxOutputTokens', '未知')}) · API ID：{item['id']}",
            )
            for item in model_catalog(runtime)
        ]
    else:
        capability = model_capabilities(runtime, runtime["model"])
        options = [
            SettingOption(
                level,
                EFFORT_LABELS.get(level, (level, ""))[0],
                f"接口默认强度：{capability['defaultEffort']}"
                if level == "default" and capability.get("defaultEffort")
                else EFFORT_LABELS.get(level, (level, "接口提供的思考强度"))[1],
            )
            for level in capability["reasoningLevels"]
        ]
    return SettingsPicker(
        kind=kind,
        model=model_display_name(runtime),
        options=options,
        current=current,
        selected=next((i for i, option in enumerate(options) if option.value == current), 0),
        notice=notice
        or runtime.get("catalogError", "")
        or ("接口未提供思考档位元数据，保留默认设置。" if kind == "effort" and len(options) <= 1 else ""),
        return_input=return_input,
        provider=runtime.get("provider"),
        provider_name=runtime.get("providerName", ""),
    )


def render_settings_picker(picker: SettingsPicker, cols: int, rows: int) -> list[str]:
    cols, rows = max(10, cols), max(5, rows)
    box_w = min(max(8, cols - 2), 86)
    content_w = max(4, box_w - 4) if box_w >= 10 else box_w
    compact = rows < 13 or box_w < 34
    title = {"provider": "选择服务商", "model": "选择模型", "effort": "选择思考强度", "permissions": "选择权限模式"}[
        picker.kind
    ]

    header: list[str] = []
    if not compact:
        if picker.provider:
            prov_name = picker.provider_name or PROVIDERS.get(picker.provider, {}).get("name", picker.provider)
            header.append(f"{TEXT_FAINT}服务商{RESET}  {TEXT}{prov_name}{RESET}")
        if picker.kind != "permissions":
            header.append(f"{TEXT_FAINT}当前模型{RESET}  {TEXT}{picker.model}{RESET}")
        if picker.error:
            header += [f"{BRIGHT_RED}{line}{RESET}" for line in wrap_text(picker.error, content_w).splitlines()[:2]]
        elif picker.notice:
            header += [f"{YELLOW}{line}{RESET}" for line in wrap_text(picker.notice, content_w).splitlines()[:2]]
        if header:
            header.append(render_divider(content_w))
    elif picker.error:
        header.append(f"{BRIGHT_RED}{truncate_plain(picker.error, content_w)}{RESET}")

    persist_state = f"{BRAND_LIME}{BOLD}开{RESET}" if picker.persist else f"{SUBTLE}关{RESET}"
    if picker.kind == "permissions":
        footer_rows = (
            [render_divider(content_w), f"{SUBTLE}√ 当前模式 · 选择后随当前会话保存{RESET}"] if not compact else []
        )
        box_footer = f"{SUBTLE}↑/↓ 选择 · Enter 应用 · Esc 返回{RESET}"
    else:
        if not compact:
            footer_rows = [
                render_divider(content_w),
                f"{TEXT_FAINT}保存为新会话默认：{RESET}{persist_state}  {SUBTLE}[s 切换] · 从下一轮对话生效{RESET}",
            ]
            box_footer = f"{SUBTLE}↑/↓ 选择 · Enter 应用 · r 刷新 · Esc 返回{RESET}"
        else:
            footer_rows = [f"{SUBTLE}默认:{RESET}{persist_state} {SUBTLE}[s] · Enter 应用 · Esc 返回{RESET}"]
            box_footer = f"{SUBTLE}↑/↓ · Enter 应用 · r 刷新{RESET}"

    border_overhead = 2 if box_w >= 10 else 0
    available = max(1, (rows - border_overhead - len(header) - len(footer_rows)) // 2)
    start = max(0, min(picker.selected - available // 2, len(picker.options) - available))
    body: list[str] = []
    for index in range(start, min(len(picker.options), start + available)):
        option = picker.options[index]
        is_sel = index == picker.selected
        is_cur = option.value == picker.current
        value_tag = f"（{option.value}）" if picker.kind == "effort" else ""
        if is_sel:
            badge = f"  {BRAND_LIME}{BOLD}√ 当前{RESET}" if is_cur else ""
            row_txt = (
                f"{ACCENT}{BOLD}>{RESET} {BRIGHT_WHITE}{BOLD}{option.label}{RESET}{SUBTLE}{value_tag}{RESET}{badge}"
            )
            body.append(paint_row(truncate_plain(row_txt, content_w), content_w, HIGHLIGHT_BG))
        else:
            badge = f"  {BRAND_LIME}√ 当前{RESET}" if is_cur else ""
            row_txt = f"  {TEXT}{option.label}{RESET}{SUBTLE}{value_tag}{RESET}{badge}"
            body.append(truncate_plain(row_txt, content_w))
        desc_txt = truncate_plain(option.description, max(1, content_w - 2))
        body.append(f"  {SUBTLE}{desc_txt}{RESET}")

    if not picker.options:
        empty_msg = (
            "请在 settings.json 的 providers 中配置服务商。"
            if picker.kind == "provider"
            else "接口没有返回可选模型。按 r 重新读取，Esc 返回。"
        )
        body = [f"{SUBTLE}{line}{RESET}" for line in wrap_text(empty_msg, content_w).splitlines()]

    counter = f"{picker.selected + 1 if picker.options else 0}/{len(picker.options)}"
    boxed = render_box(
        header + body + footer_rows,
        box_w,
        title=f"{BRAND_LIME}◆ {BOLD}{title}{RESET}",
        right_title=f"{SUBTLE}{counter}{RESET}",
        footer=box_footer,
        border_color=BORDER,
    )
    if len(boxed) > rows:
        return [truncate_plain(line, max(1, cols - 1)) for line in boxed[:rows]]
    return center_box_frame(boxed, cols, rows)
