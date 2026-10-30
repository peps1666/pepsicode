"""Session-aware model commands shared by interactive and piped terminals."""

from __future__ import annotations

from typing import Any

from pepsicode.config import save_mini_code_settings
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import (
    apply_model_settings,
    model_capabilities,
    model_catalog,
    model_selection_error,
    reasoning_request_fields,
    request_output_tokens,
    update_context_window,
    validate_model_settings,
)
from pepsicode.llm.providers import model_settings_snapshot, provider_catalog, provider_runtime


def update_session_model_settings(
    *,
    runtime: dict,
    model_adapter: Any,
    model: str,
    effort: str,
    persist: bool = False,
    messages: list | None = None,
    context_manager: Any = None,
    provider: str | None = None,
) -> None:
    if model_adapter is None or isinstance(model_adapter, MockModelAdapter):
        raise ValueError("Mock 模式下无法修改模型设置")
    candidate = provider_runtime(runtime, provider)
    validate_model_settings(candidate, model, effort)
    problem = model_selection_error(candidate, model)
    if problem:
        raise ValueError(problem)
    if persist:
        updates = {"model": model, "reasoningEffort": effort}
        if candidate.get("provider"):
            updates["provider"] = candidate["provider"]
        save_mini_code_settings(updates)
    apply_model_settings(runtime, model, effort, messages=messages, context_manager=context_manager, provider=provider)
    runtime["modelSource"] = "pepsicode 默认设置" if persist else "当前会话选择"


def handle_model_command(
    user_input: str,
    *,
    runtime: dict | None,
    model_adapter: Any,
    messages: list | None = None,
    context_manager: Any = None,
) -> str | None:
    parts = user_input.split()
    if not parts or parts[0] not in {"/model", "/effort", "/provider"}:
        return None
    command, *arguments = parts
    if command == "/provider" and arguments == ["add"]:
        return "请直接运行 pepsicode，在交互终端输入 /provider add 打开逐步配置引导。API Key 不应放在命令参数中。"
    usage = (
        "usage: /model [list | refresh | <model-id> [--save]]"
        if command == "/model"
        else "usage: /effort [<level> [--save]]"
    )
    if runtime is None or model_adapter is None or isinstance(model_adapter, MockModelAdapter):
        return "Model settings unavailable in mock/unconfigured mode. Configure an API and restart pepsicode."
    if command == "/provider":
        if not arguments or arguments == ["list"]:
            entries = provider_catalog(runtime)
            return (
                "当前服务商："
                + runtime.get("provider", "单接口配置")
                + "\n"
                + (
                    "\n".join(
                        f"{entry['name']} ({entry['id']})：{'已配置' if entry['configured'] else '缺少 API Key'}"
                        for entry in entries
                    )
                    or "请在 settings.json 的 providers 中配置 DeepSeek、Kimi、GLM、千问。"
                )
            )
        persist = arguments[-1] == "--save"
        values = arguments[:-1] if persist else arguments
        if len(values) not in {1, 2}:
            return "usage: /provider <deepseek|kimi|glm|qwen|custom> [model-id] [--save]; /provider add 打开配置引导"
        try:
            candidate = provider_runtime(runtime, values[0])
            refresh_model_catalog(candidate)
            model = values[1] if len(values) == 2 else candidate.get("model", "")
            if not model or model_selection_error(candidate, model):
                return (
                    candidate.get("catalogError", "")
                    + "\n请指定模型：/provider "
                    + values[0]
                    + " <model-id>\n"
                    + "\n".join(f"{item['name']} ({item['id']})" for item in model_catalog(candidate))
                )
            update_session_model_settings(
                runtime=runtime,
                model_adapter=model_adapter,
                model=model,
                effort="default",
                provider=values[0],
                persist=persist,
                messages=messages,
                context_manager=context_manager,
            )
            return "已切换：" + str(model_settings_snapshot(runtime)) + "。下一轮生效。"
        except (ValueError, OSError) as error:
            return f"服务商设置未修改：{error}"
    if not arguments:
        if command == "/model":
            caps = model_capabilities(runtime, runtime["model"])
            context = caps.get("contextWindow", "未提供（使用估算值）")
            maximum = caps.get("maxOutputTokens", "未提供")
            sources = caps.get("limitSources", {})
            request_limit = reasoning_request_fields(runtime, runtime["model"]).get(
                "max_tokens", request_output_tokens(runtime, runtime["model"])
            )
            return (
                f"current model: {runtime['model']}\nreasoning effort: {runtime.get('reasoningEffort', 'default')}"
                f"\n上下文窗口: {context} ({sources.get('contextWindow', 'fallback')})"
                f"\n模型最大输出: {maximum} ({sources.get('maxOutputTokens', 'fallback')})"
                f"\n基础输出预算: {request_limit}（会按剩余上下文缩小）"
            )
        levels = model_capabilities(runtime, runtime["model"])["reasoningLevels"]
        return f"reasoning effort: {runtime.get('reasoningEffort', 'default')}\nallowed levels: {', '.join(levels)}"
    if command == "/model" and arguments in (["list"], ["refresh"]):
        refresh_model_catalog(runtime, force=arguments == ["refresh"])
        update_context_window(runtime, context_manager)
        source = "API 返回的模型" if runtime.get("_catalogAuthoritative") else "本地配置（未验证可用性）"
        return (
            runtime.get("catalogError", "")
            + f"\n{source}:\n"
            + "\n".join(
                f"{'*' if item['id'] == runtime['model'] else ' '} {item['name']} ({item['id']})  [{', '.join(item['reasoningLevels'])}]"
                for item in model_catalog(runtime)
            )
        )
    persist = arguments[-1] == "--save"
    values = arguments[:-1] if persist else arguments
    if len(values) != 1 or values[0].startswith("--") or (command == "/model" and values[0] == "list"):
        return usage
    model = values[0] if command == "/model" else runtime["model"]
    effort = (
        values[0]
        if command == "/effort"
        else (runtime.get("reasoningEffort", "default") if model == runtime["model"] else "default")
    )
    try:
        update_session_model_settings(
            runtime=runtime,
            model_adapter=model_adapter,
            model=model,
            effort=effort,
            persist=persist,
            messages=messages,
            context_manager=context_manager,
        )
    except (ValueError, OSError) as error:
        return f"Model settings unchanged: {error}"
    scope = (
        "Saved as defaults (environment variables still take precedence on restart)."
        if persist
        else "Current session only; add --save to save defaults."
    )
    return f"model: {model}\nreasoning effort: {effort}\nApplies to the next turn. {scope}"
