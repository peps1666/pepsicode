"""Connection profiles for supported providers; never a model-name registry."""

from __future__ import annotations

import os
from typing import Any

# DeepSeek is listed first and is the default when no provider is chosen explicitly.
PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "baseUrl": "https://api.deepseek.com/anthropic", "keyEnv": "DEEPSEEK_API_KEY"},
    "kimi": {"name": "Kimi", "baseUrl": "https://api.moonshot.cn/anthropic", "keyEnv": "MOONSHOT_API_KEY"},
    "glm": {"name": "GLM（智谱）", "baseUrl": "https://open.bigmodel.cn/api/anthropic", "keyEnv": "ZHIPU_API_KEY"},
    "qwen": {
        "name": "千问（百炼）",
        "baseUrl": "https://dashscope.aliyuncs.com/apps/anthropic",
        "keyEnv": "DASHSCOPE_API_KEY",
    },
    "custom": {"name": "自定义厂商", "baseUrl": "", "keyEnv": ""},
}
DEFAULT_PROVIDER = "deepseek"

CONNECTION_KEYS = {
    "provider",
    "providerName",
    "authType",
    "baseUrl",
    "apiKey",
    "authToken",
    "modelsUrl",
    "models",
    "model",
    "modelSource",
    "reasoningEffort",
    "fallbackModel",
    "maxOutputTokens",
    "_catalogCheckedAt",
    "_catalogAuthoritative",
    "_discoveredModels",
    "_cachedModelCapabilities",
    "catalogUrl",
    "catalogError",
    "promptCaching",
}


def configure_providers(runtime: dict, settings: dict, env: dict) -> None:
    entries = settings.get("providers")
    if entries is None:
        if os.environ.get("PEPSI_CODE_PROVIDER") or settings.get("provider"):
            raise ValueError("请先在 settings.json 的 providers 中配置服务商及独立凭据")
        return  # Preserve existing single-endpoint installations.
    if not isinstance(entries, dict) or not entries:
        raise ValueError("providers 必须是包含服务商配置的对象")
    override = env.get("PEPSI_CODE_MAX_OUTPUT_TOKENS")
    if override:
        try:
            override = int(override)
        except (TypeError, ValueError):
            raise ValueError("PEPSI_CODE_MAX_OUTPUT_TOKENS 必须为正整数") from None
        if override <= 0:
            raise ValueError("PEPSI_CODE_MAX_OUTPUT_TOKENS 必须为正整数")
    profiles = {}
    for identifier, entry in entries.items():
        if identifier not in PROVIDERS or not isinstance(entry, dict):
            raise ValueError("支持的服务商为 deepseek、kimi、glm、qwen、custom；每项配置必须为对象")
        preset = PROVIDERS[identifier]
        key_env = entry.get("apiKeyEnv", preset["keyEnv"])
        if not isinstance(key_env, str):
            raise ValueError(f"{identifier}.apiKeyEnv 必须为环境变量名")
        token_env = entry.get("authTokenEnv", "")
        if not isinstance(token_env, str):
            raise ValueError(f"{identifier}.authTokenEnv 必须为环境变量名")
        key = env.get(key_env) or entry.get("apiKey")
        token = env.get(token_env) or entry.get("authToken")
        if any(value is not None and not isinstance(value, str) for value in (key, token)):
            raise ValueError(f"{identifier} 的凭据必须为字符串")
        profile = {
            "provider": identifier,
            "providerName": entry.get("name") or preset["name"],
            "authType": entry.get("authType", "bearer"),
            "baseUrl": entry.get("baseUrl") or preset["baseUrl"],
            "apiKey": key or None,
            "authToken": token or None,
            "modelsUrl": entry.get("modelsUrl"),
            "models": entry.get("models", []),
            "model": entry.get("model", ""),
            "reasoningEffort": entry.get("reasoningEffort", "default"),
            "maxOutputTokens": override or entry.get("maxOutputTokens"),
            "fallbackModel": entry.get("fallbackModel"),
            "promptCaching": entry.get("promptCaching", False),
            "modelSource": f"settings.json providers.{identifier}",
        }
        if not isinstance(profile["model"], str) or not isinstance(profile["models"], list):
            raise ValueError(f"{identifier} 的 model 必须为字符串，models 必须为数组")
        if not isinstance(profile["baseUrl"], str) or not isinstance(profile["reasoningEffort"], str):
            raise ValueError(f"{identifier} 的 baseUrl 与 reasoningEffort 必须为字符串")
        if not profile["baseUrl"] or profile["authType"] not in {"bearer", "api_key"}:
            raise ValueError(f"{identifier} 需要 API 地址，authType 应为 bearer 或 api_key")
        if (
            profile["modelsUrl"] is not None
            and profile["modelsUrl"] is not False
            and not isinstance(profile["modelsUrl"], str)
        ):
            raise ValueError(f"{identifier}.modelsUrl 必须为同源地址或 false")
        maximum = profile["maxOutputTokens"]
        if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0):
            raise ValueError(f"{identifier}.maxOutputTokens 必须为正整数")
        profiles[identifier] = profile
    fallback = DEFAULT_PROVIDER if DEFAULT_PROVIDER in profiles else next(iter(profiles))
    active = os.environ.get("PEPSI_CODE_PROVIDER") or settings.get("provider") or fallback
    if active not in profiles:
        raise ValueError(f"服务商 {active!r} 尚未配置")
    selected = dict(profiles[active])
    # Top-level model settings are saved by the shared picker with an explicit provider.
    if settings.get("provider") == active:
        for key in ("model", "reasoningEffort"):
            if key in settings:
                selected[key] = settings[key]
    if os.environ.get("PEPSI_CODE_MODEL"):
        selected["model"] = os.environ["PEPSI_CODE_MODEL"]
    if os.environ.get("PEPSI_CODE_REASONING_EFFORT"):
        selected["reasoningEffort"] = os.environ["PEPSI_CODE_REASONING_EFFORT"]
    runtime.update(selected)
    runtime["_providerRuntimes"] = profiles


def provider_catalog(runtime: dict) -> list[dict[str, Any]]:
    """Every built-in preset, plus the custom provider once it has been set up."""
    profiles = runtime.get("_providerRuntimes", {})
    entries = []
    for identifier, preset in PROVIDERS.items():
        profile = profiles.get(identifier)
        if identifier == "custom" and profile is None:
            continue
        profile = profile or {}
        entries.append(
            {
                "id": identifier,
                "name": profile.get("providerName") or preset["name"],
                "configured": bool(profile.get("apiKey") or profile.get("authToken")),
                "baseUrl": preset["baseUrl"],
            }
        )
    return entries


def provider_runtime(runtime: dict, provider: str | None = None) -> dict:
    if provider is not None and not isinstance(provider, str):
        raise ValueError("provider 必须为服务商 ID 字符串")
    if provider is None or provider == runtime.get("provider"):
        return runtime
    profile = runtime.get("_providerRuntimes", {}).get(provider)
    if profile is None and provider not in PROVIDERS:
        raise ValueError(f"服务商 {provider!r} 尚未配置")
    if profile is None or (not profile.get("apiKey") and not profile.get("authToken")):
        raise ValueError(f"请先配置 {PROVIDERS[provider]['name']} 的独立 API Key")
    return profile


def commit_provider(runtime: dict, candidate: dict) -> None:
    """Keep the adapter's runtime identity and replace every connection field atomically."""
    if candidate is runtime:
        return
    profiles = runtime.get("_providerRuntimes", {})
    previous = runtime.get("provider")
    if previous in profiles:
        profiles[previous] = {key: runtime[key] for key in CONNECTION_KEYS if key in runtime}
    for key in CONNECTION_KEYS:
        runtime.pop(key, None)
    runtime.update({key: candidate[key] for key in CONNECTION_KEYS if key in candidate})


def model_settings_snapshot(runtime: dict) -> dict[str, str]:
    result = {"model": runtime["model"], "reasoningEffort": runtime.get("reasoningEffort", "default")}
    if runtime.get("provider"):
        result["provider"] = runtime["provider"]
    return result


def messages_url(runtime: dict) -> str:
    base = runtime["baseUrl"].rstrip("/")
    return base + ("/messages" if base.endswith("/v1") else "/v1/messages")


def auth_headers(runtime: dict) -> dict[str, str]:
    if runtime.get("authType") == "api_key":
        return {"x-api-key": runtime.get("apiKey") or runtime.get("authToken")}
    if runtime.get("provider") or not runtime.get("apiKey"):
        return {"Authorization": f"Bearer {runtime.get('authToken') or runtime.get('apiKey')}"}
    return {"x-api-key": runtime["apiKey"]}
