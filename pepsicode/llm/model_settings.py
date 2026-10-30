"""Model capabilities and translation of UI reasoning choices to Messages API fields."""

from __future__ import annotations

from typing import Any

from pepsicode.llm.providers import commit_provider, provider_runtime

BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}
AUTO_OUTPUT_TOKENS = 65536
UNKNOWN_OUTPUT_TOKENS = 8192


def request_output_tokens(runtime: dict[str, Any], model: str) -> int:
    """Choose a bounded request budget, respecting explicit and advertised limits."""
    maximum = model_capabilities(runtime, model).get("maxOutputTokens")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        maximum = None
    configured = runtime.get("maxOutputTokens")
    if configured is not None:
        if not isinstance(configured, int) or isinstance(configured, bool) or configured <= 0:
            raise ValueError("maxOutputTokens must be a positive integer")
        if maximum is not None and configured > maximum:
            raise ValueError(f"maxOutputTokens {configured} exceeds the model's advertised maximum {maximum}")
        return configured
    return min(AUTO_OUTPUT_TOKENS, maximum) if maximum is not None else UNKNOWN_OUTPUT_TOKENS


def update_context_window(runtime: dict[str, Any], manager: Any) -> None:
    """Apply known limits without resetting live token usage."""
    if manager is None:
        return
    try:
        requested = request_output_tokens(runtime, runtime["model"])
        manager.output_reserve = reasoning_request_fields(runtime, runtime["model"]).get("max_tokens", requested)
    except ValueError:
        # An invalid selection still needs its picker so the user can fix it.
        manager.output_reserve = 0
    capabilities = model_capabilities(runtime, runtime["model"])
    window = capabilities.get("contextWindow")
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        manager.context_window = window
        manager.context_window_is_estimate = capabilities["limitSources"].get("contextWindow") in {"cache", "last-api"}
    else:
        from pepsicode.context.context_manager import DEFAULT_CONTEXT_WINDOWS

        model = runtime["model"]
        manager.context_window = DEFAULT_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOWS["default"])
        manager.context_window_is_estimate = model not in DEFAULT_CONTEXT_WINDOWS


def model_selection_error(runtime: dict[str, Any], model: str | None = None) -> str | None:
    model = model if model is not None else runtime.get("model", "")
    if not runtime.get("_catalogAuthoritative") or any(
        item["id"] == model for item in runtime.get("_discoveredModels", [])
    ):
        return None
    source = runtime.get("modelSource", "当前配置")
    return (
        f"当前接口返回的模型列表中没有 {model!r}（来源：{source}）。请使用 /model 选择接口实际返回的模型，或刷新列表。"
    )


def model_capabilities(runtime: dict[str, Any], model: str) -> dict[str, Any]:
    """Use API metadata first; configuration may explicitly fill missing metadata."""
    configured = next(
        (item for item in runtime.get("models", []) if isinstance(item, dict) and item.get("id") == model), {}
    )
    discovered = next((item for item in runtime.get("_discoveredModels", []) if item["id"] == model), {})
    cached = next((item for item in runtime.get("_cachedModelCapabilities", []) if item.get("id") == model), {})
    profile = {**configured, **discovered}
    sources = {}
    for field in ("contextWindow", "maxOutputTokens"):
        for source, data in (("config", configured), ("api", discovered), ("cache", cached)):
            if field not in data:
                continue
            value = data[field]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                if source == "config":
                    raise ValueError(f"{field} must be a positive integer for {model}")
                continue
            profile[field] = value
            sources[field] = "last-api" if source == "api" and runtime.get("catalogError") else source
            break
    profile["limitSources"] = sources
    if discovered.get("reasoningMode") == "none" and configured.get("reasoningMode"):
        profile.update(
            {
                key: value
                for key, value in configured.items()
                if key in {"reasoningMode", "reasoningLevels", "supportsThinkingOff"}
            }
        )
        profile["capabilitySource"] = "config"
    mode = profile.get("reasoningMode", "none")
    if mode not in {"none", "budget", "budget_effort", "adaptive", "effort", "output_effort"}:
        raise ValueError(f"Invalid reasoningMode for {model}: {mode}")
    levels = profile.get("reasoningLevels", list(BUDGETS) if mode == "budget" else [])
    if not isinstance(levels, list) or any(
        not isinstance(level, str) or not level or any(c.isspace() or ord(c) < 32 for c in level) for level in levels
    ):
        raise ValueError(f"Invalid reasoningLevels for {model}")
    if mode == "budget" and any(level not in BUDGETS for level in levels):
        raise ValueError(f"Invalid budget levels for {model}")
    choices = ["default"]
    if profile.get("supportsThinkingOff", False):
        choices.append("off")
    if mode != "none":
        choices += levels
    return {
        **profile,
        "id": model,
        "name": profile.get("name", model),
        "reasoningMode": mode,
        "reasoningLevels": list(dict.fromkeys(choices)),
    }


def model_catalog(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    if runtime.get("_catalogAuthoritative"):
        return [model_capabilities(runtime, item["id"]) for item in runtime.get("_discoveredModels", [])]
    names = [runtime["model"], runtime.get("fallbackModel")]
    names += [item.get("id") if isinstance(item, dict) else item for item in runtime.get("models", [])]
    return [
        model_capabilities(runtime, name)
        for name in dict.fromkeys(names)
        if isinstance(name, str) and name and not model_selection_error(runtime, name)
    ]


def model_display_name(runtime: dict[str, Any], model: str | None = None) -> str:
    return model_capabilities(runtime, model or runtime["model"])["name"]


def validate_model_settings(runtime: dict[str, Any], model: Any, effort: Any) -> None:
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model):
        raise ValueError("Model must be a non-empty ID without whitespace (up to 200 characters)")
    if effort not in model_capabilities(runtime, model)["reasoningLevels"]:
        raise ValueError(f"Reasoning effort {effort!r} is not supported for {model}")
    # Validate output budget before accepting a setting in the UI.
    request_output_tokens(runtime, model)
    reasoning_request_fields({**runtime, "reasoningEffort": effort}, model)


def apply_model_settings(
    runtime: dict[str, Any],
    model: str,
    effort: str,
    *,
    messages: list | None = None,
    context_manager: Any = None,
    provider: str | None = None,
) -> None:
    """Update shared runtime in place so the adapter and Task tools see the next-turn settings."""
    candidate = provider_runtime(runtime, provider)
    validate_model_settings(candidate, model, effort)
    problem = model_selection_error(candidate, model)
    if problem:
        raise ValueError(problem)
    if (model != runtime["model"] or candidate is not runtime) and messages is not None:
        messages[:] = [message for message in messages if message.get("role") != "assistant_thinking"]
    commit_provider(runtime, candidate)
    runtime.update({"model": model, "reasoningEffort": effort})
    if context_manager is not None:
        context_manager.update_model(model)
        update_context_window(runtime, context_manager)


def reasoning_request_fields(runtime: dict[str, Any], model: str) -> dict[str, Any]:
    effort = runtime.get("reasoningEffort", "default")
    capabilities = model_capabilities(runtime, model)
    if effort not in capabilities["reasoningLevels"] and model == runtime.get("model"):
        raise ValueError(f"当前接口没有声明思考档位 {effort!r}，请使用 /effort 选择接口返回的档位或默认设置")
    # A fallback / sub-agent may use a different model: don't forward incompatible fields.
    if effort == "default" or effort not in capabilities["reasoningLevels"]:
        return {}
    if effort == "off":
        return {"thinking": {"type": "disabled"}}
    mode = capabilities["reasoningMode"]
    if mode == "output_effort":
        return {"output_config": {"effort": effort}}
    if mode in {"budget", "budget_effort"}:
        budget = BUDGETS[effort] if mode == "budget" else 1024
        maximum = request_output_tokens(runtime, model)
        if runtime.get("maxOutputTokens") is None and model_capabilities(runtime, model).get("maxOutputTokens") is None:
            maximum = max(maximum, budget + 4096)
        if maximum <= budget:
            raise ValueError(f"maxOutputTokens must exceed the {budget}-token thinking budget for {effort}")
        return {
            "thinking": {"type": "enabled", "budget_tokens": budget},
            "max_tokens": maximum,
            **({"output_config": {"effort": effort}} if mode == "budget_effort" else {}),
        }
    return {
        # Newer adaptive models omit thinking text unless asked; summaries make it viewable.
        "thinking": {"type": "adaptive", "display": "summarized"} if mode == "adaptive" else {"type": "enabled"},
        "output_config": {"effort": effort},
        "max_tokens": request_output_tokens(runtime, model),
    }
