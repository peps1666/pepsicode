"""Validate, discover and save a guided provider setup without invoking a model."""

from __future__ import annotations

from urllib.parse import urlsplit

from pepsicode.config import read_settings_file, save_mini_code_settings
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import apply_model_settings, model_catalog, validate_model_settings
from pepsicode.llm.providers import PROVIDERS, commit_provider


def validate_setup_field(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("请输入文字")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("输入不能包含换行或控制字符")
    if field == "name" and (not value or len(value) > 60):
        raise ValueError("请输入 1–60 个字符的厂商名称")
    if field in {"baseUrl", "modelsUrl"} and value:
        parts = urlsplit(value)
        if field == "baseUrl" or parts.netloc or parts.scheme:
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("请输入完整的 http:// 或 https:// 地址，地址中不要包含 Key 或密码")
        if parts.query or parts.fragment:
            raise ValueError("地址中不要包含查询参数或片段；API Key 在下一步单独填写")
        if field == "baseUrl" and parts.path.rstrip("/").endswith(("/messages", "/chat/completions", "/responses")):
            raise ValueError("请填写服务商提供的 Anthropic 兼容 Base URL，不要包含 messages 等调用路径")
    if field == "baseUrl" and not value:
        raise ValueError("请填写 API Base URL")
    if field == "apiKey" and (not value or any(char.isspace() for char in value)):
        raise ValueError("请填写 API Key，不能包含空格或换行")
    if field == "authType" and value not in {"bearer", "api_key"}:
        raise ValueError("请选择 Bearer Token 或 x-api-key")
    if field == "model" and (not value or len(value) > 200 or any(char.isspace() for char in value)):
        raise ValueError("请填写服务商提供的完整模型 ID，不能包含空格")
    return value.rstrip("/") if field == "baseUrl" else value


def build_setup_candidate(draft: dict) -> dict:
    identifier = draft.get("provider", "custom")
    if identifier not in PROVIDERS:
        raise ValueError("请选择支持的服务商或自定义厂商")
    fields = {field: validate_setup_field(field, draft.get(field, "")) for field in ("name", "baseUrl", "apiKey")}
    auth = validate_setup_field("authType", draft.get("authType", "bearer"))
    models_url = draft.get("modelsUrl", "")
    if models_url is not False:
        models_url = validate_setup_field("modelsUrl", models_url)
        if models_url:
            from urllib.parse import urljoin

            base = urlsplit(fields["baseUrl"])
            target = urlsplit(urljoin(fields["baseUrl"] + "/", models_url))
            if (base.scheme, base.netloc) != (target.scheme, target.netloc):
                raise ValueError("模型列表地址必须与 API 地址同源")
    return {
        "provider": identifier,
        "providerName": fields["name"],
        "baseUrl": fields["baseUrl"],
        "authType": auth,
        "apiKey": fields["apiKey"],
        "authToken": None,
        "modelsUrl": models_url or (False if models_url is False else None),
        "models": [],
        "model": "",
        "reasoningEffort": "default",
        "maxOutputTokens": None,
        "fallbackModel": None,
        "modelSource": "配置引导",
    }


def probe_provider_setup(draft: dict) -> dict:
    candidate = build_setup_candidate(draft)
    refresh_model_catalog(candidate, force=True)
    return candidate


def public_setup_result(candidate: dict) -> dict:
    return {
        "models": model_catalog(candidate),
        "catalogError": candidate.get("catalogError", ""),
        "verified": bool(candidate.get("_catalogAuthoritative")),
    }


def save_provider_setup(runtime: dict, draft: dict, candidate: dict) -> dict:
    """Save only on the final confirmation; a failed write cannot change the active runtime."""
    from pepsicode import config

    model = validate_setup_field("model", draft.get("model", ""))
    if candidate.get("_catalogAuthoritative") and model not in {item["id"] for item in model_catalog(candidate)}:
        raise ValueError("请选择接口返回的模型；若列表有变化，请重新读取")
    validate_model_settings(candidate, model, "default")
    # Revalidate the connection fields, never trust a stale candidate after editing the draft.
    clean = build_setup_candidate(draft)
    for key in ("provider", "providerName", "baseUrl", "apiKey", "authType", "modelsUrl"):
        if clean[key] != candidate[key]:
            raise ValueError("连接配置已变化，请先重新读取模型列表")
    identifier = candidate["provider"]
    existing = read_settings_file(config.PEPSI_CODE_SETTINGS_PATH)
    entry = {
        "name": candidate["providerName"],
        "baseUrl": candidate["baseUrl"],
        "authType": candidate["authType"],
        "apiKey": candidate["apiKey"],
        "modelsUrl": candidate.get("modelsUrl"),
        "model": model,
    }
    if not candidate.get("_catalogAuthoritative"):
        entry["models"] = [model]
    entries = {**existing.get("providers", {}), identifier: entry}
    save_mini_code_settings(
        {"providers": entries, "provider": identifier, "model": model, "reasoningEffort": "default"}
    )
    # The connection and discovered metadata are already validated; keep the shared runtime identity.
    profiles = runtime.setdefault("_providerRuntimes", {})
    candidate = {**candidate, "models": entry.get("models", [])}
    commit_provider(runtime, candidate)
    profiles[identifier] = candidate
    apply_model_settings(runtime, model, "default")
    return runtime
