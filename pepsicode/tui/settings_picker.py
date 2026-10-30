"""Keyboard model / effort pickers. Rendering is independent of terminal IO."""

from __future__ import annotations

from dataclasses import dataclass

from pepsicode.llm.model_settings import model_capabilities, model_catalog, model_display_name
from pepsicode.llm.providers import PROVIDERS, provider_catalog
from pepsicode.tui.chrome import ACCENT, BOLD, HIGHLIGHT_BG, RESET, SUBTLE, truncate_plain, wrap_text

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
    ("read_only", "只读", "查看文件和搜索，禁止编辑及命令执行"),
    ("auto", "自动", "项目内编辑免确认；本地命令、项目外访问和外部工具需确认"),
    (
        "trust_commands",
        "命令免确认（高风险）",
        "所有程序以当前用户权限运行，可访问项目外文件和网络；内置工具访问项目外及外部工具仍需确认",
    ),
    ("full_access", "完全访问", "文件、命令及外部工具均免权限确认"),
]


def create_permissions_picker(current: str) -> SettingsPicker:
    options = [SettingOption(*option) for option in PERMISSION_OPTIONS]
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
    width, height = max(8, cols - 2), max(5, rows)
    title = {"provider": "选择服务商", "model": "选择模型", "effort": "选择思考强度", "permissions": "选择权限模式"}[
        picker.kind
    ]
    header = [f"  {BOLD}{title}{RESET}"]
    if picker.kind != "permissions":
        header.append(f"  {SUBTLE}当前模型：{picker.model}{RESET}")
    if picker.provider:
        header.insert(
            1, f"  服务商：{picker.provider_name or PROVIDERS.get(picker.provider, {}).get('name', picker.provider)}"
        )
    if picker.notice or picker.error:
        header += [f"  {line}" for line in wrap_text(picker.error or picker.notice, max(4, width - 4)).splitlines()[:2]]
    available = max(1, (height - len(header) - 4) // 2)
    start = max(0, min(picker.selected - available // 2, len(picker.options) - available))
    body = []
    for index in range(start, min(len(picker.options), start + available)):
        option = picker.options[index]
        current = "  √ 当前" if option.value == picker.current else ""
        value = f"（{option.value}）" if picker.kind == "effort" else ""
        line = truncate_plain(f"  {'>' if index == picker.selected else ' '} {option.label}{value}{current}", width)
        body += [
            f"{HIGHLIGHT_BG}{ACCENT}{line}{RESET}" if index == picker.selected else line,
            f"    {SUBTLE}{option.description}{RESET}",
        ]
    if not picker.options:
        body = [
            "  请在 settings.json 的 providers 中配置服务商。"
            if picker.kind == "provider"
            else "  接口没有返回可选模型。按 r 重新读取，Esc 返回。"
        ]
    footer = [
        f"  保存为新会话默认：{'开' if picker.persist else '关'}  [s 切换]",
        "  ↑/↓ 选择 · Enter 应用 · r 刷新 · Esc 返回",
        f"  {picker.selected + 1 if picker.options else 0}/{len(picker.options)} · 从下一轮对话生效",
    ]
    if picker.kind == "permissions":
        footer = ["  ↑/↓ 选择 · Enter 应用 · Esc 返回", "  √ 当前模式 · 选择后随当前会话保存"]
    content = (header + body)[: max(1, height - len(footer))]
    content += [""] * max(0, height - len(content) - len(footer))
    return [truncate_plain(line, width) for line in (content + footer)[:height]]
