"""Step-by-step provider form; credentials stay out of the transcript and history."""

from dataclasses import dataclass, field

from pepsicode.llm.provider_setup import validate_setup_field
from pepsicode.llm.providers import DEFAULT_PROVIDER, PROVIDERS
from pepsicode.tui.chrome import (
    ACCENT,
    BOLD,
    BORDER,
    BRAND_LIME,
    BRIGHT_RED,
    BRIGHT_WHITE,
    CYAN,
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
from pepsicode.tui.input import render_input_prompt
from pepsicode.tui.input_parser import KeyEvent, TextEvent

FIELDS = ["name", "baseUrl", "authType", "apiKey", "modelsUrl", "model", "review"]
LABELS = {
    "name": ("厂商名称", "给这家服务商起个容易识别的名字。"),
    "baseUrl": ("API 地址", "填写 Anthropic Messages 兼容 Base URL，例如 https://gateway.example/anthropic。"),
    "authType": ("鉴权方式", "按服务商文档选择；多数兼容服务支持 Bearer Token。"),
    "apiKey": ("API Key", "粘贴这家厂商的 Key。输入已隐藏，不写入聊天记录或输入历史。"),
    "modelsUrl": ("模型列表地址（可选）", "直接回车自动查找；可填同源路径。接口没有列表时选择“无列表”。"),
    "model": ("选择模型", "使用接口返回的名称，实际请求提交对应 ID。"),
    "review": ("确认配置", "保存并使用：写入本机 ~/.pepsi-code/settings.json，并设为新会话默认。"),
}


@dataclass
class ProviderWizard:
    draft: dict = field(default_factory=dict)
    step: int = 0
    text: str = ""
    cursor: int = 0
    selected: int = 0
    candidate: dict | None = None
    error: str = ""
    no_catalog: bool = False

    @property
    def current(self):
        return FIELDS[self.step]

    def move(self, step: int):
        self.step = step
        value = self.draft.get(self.current, "")
        self.text = value if isinstance(value, str) else ""
        self.cursor = len(self.text)
        self.selected = 0
        self.error = ""


def create_provider_wizard(provider: str = DEFAULT_PROVIDER) -> ProviderWizard:
    preset = PROVIDERS[provider]
    wizard = ProviderWizard(
        draft={
            "provider": provider,
            "name": "" if provider == "custom" else preset["name"],
            "baseUrl": preset["baseUrl"],
            "authType": "bearer",
            "modelsUrl": "",
        }
    )
    # Presets already know their name, address and auth; start at the API Key (Alt+← still edits them).
    wizard.move(0 if provider == "custom" else FIELDS.index("apiKey"))
    return wizard


def wizard_event(wizard: ProviderWizard, event) -> str | None:
    """Return probe/save/cancel for the app to perform IO; all editing is local."""
    current = wizard.current
    from pepsicode.llm.model_settings import model_catalog

    models = model_catalog(wizard.candidate) if wizard.candidate else []
    menu = current == "authType" or (current == "model" and bool(models))
    if isinstance(event, KeyEvent):
        if event.name == "escape" or (event.ctrl and event.name == "c"):
            wizard.draft.clear()
            wizard.candidate = None
            wizard.text, wizard.cursor = "", 0
            return "cancel"
        if event.meta and event.name == "left":
            wizard.move(max(0, wizard.step - 1))
            return None
        if event.name == "tab" and current == "modelsUrl":
            wizard.no_catalog = not wizard.no_catalog
            return None
        if menu and event.name in {"up", "down"}:
            wizard.selected = (wizard.selected + (-1 if event.name == "up" else 1)) % (
                2 if current == "authType" else len(models)
            )
        elif event.name == "return":
            if current == "review":
                return "save"
            if current == "model" and not models and wizard.candidate and wizard.candidate.get("_catalogAuthoritative"):
                wizard.error = "接口返回了空列表，请返回检查 API 地址或账号权限。"
                return None
            if current == "authType":
                value = ["bearer", "api_key"][wizard.selected]
            elif current == "model" and models:
                value = models[wizard.selected]["id"]
            elif current == "modelsUrl" and wizard.no_catalog:
                value = False
            else:
                try:
                    value = validate_setup_field(current, wizard.text)
                except ValueError as error:
                    wizard.error = str(error)
                    return None
            wizard.draft[current] = value
            if current == "modelsUrl":
                return "probe"
            wizard.move(wizard.step + 1)
        elif not menu and current != "review":
            if event.name == "backspace" and wizard.cursor:
                wizard.text = wizard.text[: wizard.cursor - 1] + wizard.text[wizard.cursor :]
                wizard.cursor -= 1
            elif event.name == "delete":
                wizard.text = wizard.text[: wizard.cursor] + wizard.text[wizard.cursor + 1 :]
            elif event.name in {"left", "right"}:
                wizard.cursor = max(0, min(len(wizard.text), wizard.cursor + (-1 if event.name == "left" else 1)))
            elif event.name in {"home", "end"}:
                wizard.cursor = 0 if event.name == "home" else len(wizard.text)
            elif event.ctrl and event.name == "u":
                wizard.text, wizard.cursor = "", 0
    elif isinstance(event, TextEvent) and not event.ctrl and not event.meta and not menu and current != "review":
        if any(ord(char) < 32 or ord(char) == 127 for char in event.text):
            wizard.error = "请粘贴单行内容，不包含控制字符。"
            return None
        if len(wizard.text) + len(event.text) <= 4096:
            wizard.text = wizard.text[: wizard.cursor] + event.text + wizard.text[wizard.cursor :]
            wizard.cursor += len(event.text)
    return None


def render_provider_wizard(wizard: ProviderWizard, cols: int, rows: int) -> list[str]:
    from pepsicode.llm.model_settings import model_catalog

    cols, rows = max(10, cols), max(8, rows)
    box_w = min(max(8, cols - 2), 84)
    content_w = max(4, box_w - 4) if box_w >= 10 else box_w
    compact = rows < 14 or box_w < 36
    current = wizard.current
    label, hint = LABELS[current]
    title_name = "添加厂商" if wizard.draft.get("provider") == "custom" else f"配置 {wizard.draft.get('name', '')}"

    dots = " ".join(f"{BRAND_LIME}●{RESET}" if i <= wizard.step else f"{BORDER}○{RESET}" for i in range(len(FIELDS)))
    lines: list[str] = [f"{dots}  {SUBTLE}Anthropic Messages 兼容{RESET}" if not compact else dots]
    if not compact:
        lines += [f"{SUBTLE}{line}{RESET}" for line in wrap_text(hint, content_w).splitlines()]
    if wizard.error:
        lines += [f"{BRIGHT_RED}{line}{RESET}" for line in wrap_text(wizard.error, content_w).splitlines()[:2]]
    if not compact:
        lines.append(render_divider(content_w))

    models = model_catalog(wizard.candidate) if wizard.candidate else []
    if current == "authType":
        for i, name in enumerate(["Bearer Token（Authorization）", "API Key（x-api-key）"]):
            if wizard.selected == i:
                lines.append(
                    paint_row(
                        truncate_plain(f"{ACCENT}{BOLD}>{RESET} {BRIGHT_WHITE}{BOLD}{name}{RESET}", content_w),
                        content_w,
                        HIGHLIGHT_BG,
                    )
                )
            else:
                lines.append(f"  {TEXT}{name}{RESET}")
    elif current == "model" and models:
        footer_overhead = 2 if not compact else 0
        border_overhead = 2 if box_w >= 10 else 0
        capacity = max(1, rows - border_overhead - len(lines) - footer_overhead)
        start = max(0, min(wizard.selected - capacity // 2, len(models) - capacity))
        for i in range(start, min(len(models), start + capacity)):
            item = models[i]
            label_txt = f"{item['name']} ({item['id']})"
            if wizard.selected == i:
                row_txt = f"{ACCENT}{BOLD}>{RESET} {BRIGHT_WHITE}{BOLD}{label_txt}{RESET}"
                lines.append(paint_row(truncate_plain(row_txt, content_w), content_w, HIGHLIGHT_BG))
            else:
                lines.append(truncate_plain(f"  {TEXT}{label_txt}{RESET}", content_w))
    elif current == "review":
        lines += [
            f"{TEXT_FAINT}厂商{RESET}  {BRIGHT_WHITE}{wizard.draft['name']}{RESET}",
            f"{TEXT_FAINT}地址{RESET}  {CYAN}{wizard.draft['baseUrl']}{RESET}",
            f"{TEXT_FAINT}鉴权{RESET}  {TEXT}{wizard.draft['authType']}{RESET} {SUBTLE}· Key 已填写（本机配置文件保存）{RESET}",
            f"{TEXT_FAINT}模型{RESET}  {BRAND_LIME}{wizard.draft['model']}{RESET}",
            "",
            f"{BRAND_LIME}▸{RESET} {BRIGHT_WHITE}{BOLD}Enter 保存并使用{RESET}",
        ]
    else:
        if current == "model":
            if wizard.candidate and wizard.candidate.get("_catalogAuthoritative") and not models:
                lines += [f"{YELLOW}接口返回了空列表，请返回检查 API 地址或账号权限。{RESET}"]
            notice = wizard.candidate.get("catalogError", "接口未返回模型") if wizard.candidate else ""
            if notice:
                lines += [f"{YELLOW}{line}{RESET}" for line in wrap_text(notice, content_w).splitlines()[:2]]
            if not wizard.candidate or not wizard.candidate.get("_catalogAuthoritative"):
                lines += [f"{SUBTLE}请填服务商提供的模型 ID（未验证可用性）：{RESET}"]
        value = "*" * len(wizard.text) if current == "apiKey" else wizard.text
        lines += [render_input_prompt(value, wizard.cursor, width=content_w, hint="请输入…")]
        if current == "modelsUrl":
            mode_label = (
                f"{YELLOW}无列表，手动填写模型{RESET}"
                if wizard.no_catalog
                else f"{BRAND_LIME}自动发现 / 自定义路径{RESET}"
            )
            lines += [f" {TEXT_FAINT}列表：{RESET}{mode_label} {SUBTLE}（Tab 切换）{RESET}"]

    if not compact:
        lines += [render_divider(content_w), f"{SUBTLE}完成前不会保存；读取列表只发送 GET 请求。{RESET}"]

    boxed = render_box(
        lines,
        box_w,
        title=f"{BRAND_LIME}◆ {BOLD}{title_name}{RESET}",
        right_title=f"{SUBTLE}{wizard.step + 1}/{len(FIELDS)} · {label}{RESET}",
        footer=f"{SUBTLE}Enter 下一步 · Alt+← 上一步 · Esc 取消{RESET}",
        border_color=BORDER,
    )
    if len(boxed) > rows:
        return [truncate_plain(line, max(1, cols - 1)) for line in boxed[:rows]]
    return center_box_frame(boxed, cols, rows)
