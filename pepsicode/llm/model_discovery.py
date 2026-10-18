"""Discover model IDs, display names and capabilities from the configured API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pepsicode.llm.providers import auth_headers

CACHE_SECONDS = 300


class ModelDiscoveryError(ValueError):
    """A controlled, credential-free diagnostic suitable for the UI."""


def _supported(value: Any) -> bool:
    return value is True or (isinstance(value, dict) and value.get("supported") is True)


def _levels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item
            for item in value
            if isinstance(item, str) and item and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in item)
        )
    )


def parse_model_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize both Messages API and compatible /models metadata without inspecting IDs."""
    identifier = item.get("id")
    if (
        not isinstance(identifier, str)
        or not identifier
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in identifier)
    ):
        raise ModelDiscoveryError("模型列表包含无效 id")
    capabilities = item.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    thinking = capabilities.get("thinking") or {}
    types = thinking.get("types", {}) if isinstance(thinking, dict) else {}
    types = types if isinstance(types, dict) else {}
    effort = item.get("effort") or {}
    effort = effort if isinstance(effort, dict) else {}
    levels = _levels(effort.get("supported_levels"))
    capability_effort = capabilities.get("effort") or {}
    if not levels and isinstance(capability_effort, dict):
        levels = _levels([key for key, value in capability_effort.items() if key != "supported" and _supported(value)])
    adaptive = _supported(types.get("adaptive"))
    enabled = _supported(types.get("enabled"))
    mode = (
        "adaptive"
        if adaptive
        else "budget_effort"
        if enabled and levels and not effort
        else "effort"
        if levels
        else "none"
    )
    # A thinking budget is a client policy, not a provider-declared effort level.
    profile: dict[str, Any] = {
        "id": identifier,
        "name": identifier,
        "reasoningMode": mode,
        "reasoningLevels": levels,
        "supportsThinkingOff": _supported(types.get("disabled")) or effort.get("supports_disabled") is True,
        "capabilitySource": "api",
    }
    if isinstance(effort.get("default_level"), str) and effort["default_level"] in levels:
        profile["defaultEffort"] = effort["default_level"]
    display_name = item.get("name") or item.get("display_name")
    if isinstance(display_name, str) and display_name.strip():
        profile["name"] = "".join(c for c in display_name if ord(c) >= 32 and ord(c) != 127)
    for target, source in (
        ("contextWindow", "context_window"),
        ("contextWindow", "context_length"),
        ("contextWindow", "max_input_tokens"),
        ("maxOutputTokens", "max_output_tokens"),
        ("maxOutputTokens", "max_tokens"),
    ):
        value = item.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            profile[target] = value
    return profile


def _models_urls(runtime: dict) -> list[str]:
    base = str(runtime["baseUrl"]).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ModelDiscoveryError("API 地址必须为不含内嵌凭据的 HTTP(S) 地址")
    explicit = runtime.get("modelsUrl")
    if explicit is False:
        raise ModelDiscoveryError("当前配置未启用模型列表接口，请显式配置 models 或设置可用的 modelsUrl")
    if explicit:
        target = urllib.parse.urljoin(base + "/", explicit)
        destination = urllib.parse.urlsplit(target)
        if (destination.scheme, destination.netloc) != (parsed.scheme, parsed.netloc):
            raise ModelDiscoveryError("modelsUrl 必须与 API 地址同源，避免把凭据发送给其他服务")
        return [target]
    if parsed.path.rstrip("/").endswith("/anthropic"):
        parent = base[: -len("/anthropic")]
        return [base + "/v1/models", parent + "/models", parent + "/v1/models"]
    return [base + "/models"] if parsed.path.rstrip("/").endswith("/v1") else [base + "/v1/models", base + "/models"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward API credentials through provider redirects.
        return None


def refresh_model_catalog(runtime: dict[str, Any], *, force: bool = False) -> None:
    """Fetch metadata only. Never invoke a model or substitute built-in candidates."""
    if not force and "_catalogCheckedAt" in runtime and runtime["_catalogCheckedAt"] + CACHE_SECONDS > time.monotonic():
        return
    runtime["_catalogCheckedAt"] = time.monotonic()
    try:
        urls = _models_urls(runtime)
        headers = {"Accept": "application/json", "anthropic-version": "2023-06-01"}
        if runtime.get("provider"):
            headers.update(auth_headers(runtime))
        elif runtime.get("apiKey"):
            headers.update({"x-api-key": runtime["apiKey"], "Authorization": f"Bearer {runtime['apiKey']}"})
        elif runtime.get("authToken"):
            headers["Authorization"] = f"Bearer {runtime['authToken']}"
        opener = urllib.request.build_opener(_NoRedirect())
        collected: list[dict] = []
        selected_url = ""
        for index, url in enumerate(urls):
            try:
                for _page in range(50):
                    request = urllib.request.Request(url, headers=headers, method="GET")
                    with opener.open(request, timeout=5) as response:
                        body = response.read(4 * 1024 * 1024 + 1)
                    if len(body) > 4 * 1024 * 1024:
                        raise ModelDiscoveryError("模型列表响应过大")
                    data = json.loads(body)
                    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                        raise ModelDiscoveryError("模型列表响应缺少 data 数组")
                    collected.extend(parse_model_metadata(item) for item in data["data"] if isinstance(item, dict))
                    selected_url = url
                    if not data.get("has_more"):
                        break
                    after = data.get("last_id")
                    if not isinstance(after, str) or not after:
                        raise ModelDiscoveryError("分页响应缺少 last_id")
                    parts = urllib.parse.urlsplit(url)
                    query = dict(urllib.parse.parse_qsl(parts.query))
                    if query.get("after_id") == after:
                        raise ModelDiscoveryError("模型列表分页没有前进")
                    query["after_id"] = after
                    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
                else:
                    raise ModelDiscoveryError("模型列表分页超过限制")
                break
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                if status in {404, 405} and not collected and index + 1 < len(urls):
                    continue
                raise ModelDiscoveryError(f"模型列表接口返回 HTTP {status}") from None
        runtime["_discoveredModels"] = list({item["id"]: item for item in collected}.values())
        runtime["_catalogAuthoritative"] = True
        runtime["catalogUrl"] = selected_url
        runtime["catalogError"] = ""
    except (ValueError, OSError, urllib.error.URLError, TimeoutError) as error:
        # Error bodies may echo credentials. Expose only controlled diagnostics.
        detail = str(error) if isinstance(error, ModelDiscoveryError) else "网络连接失败或响应不是有效 JSON"
        fallback = (
            "正在使用上次成功读取的列表。"
            if runtime.get("_catalogAuthoritative")
            else "仅显示本地配置（未验证可用性）。"
        )
        runtime["catalogError"] = f"无法读取模型列表：{detail}。可刷新重试；{fallback}"
