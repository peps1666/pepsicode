from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pepsicode.llm.providers import configure_providers

PEPSI_CODE_DIR = Path.home() / ".pepsi-code"
PEPSI_CODE_SETTINGS_PATH = PEPSI_CODE_DIR / "settings.json"
PEPSI_CODE_HISTORY_PATH = PEPSI_CODE_DIR / "history.json"
PEPSI_CODE_PERMISSIONS_PATH = PEPSI_CODE_DIR / "permissions.json"
PEPSI_CODE_MCP_PATH = PEPSI_CODE_DIR / "mcp.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _truthy(value: Any) -> bool:
    """Interpret a config/env value as a boolean.

    Accepts genuine bools, the strings ``"true"/"1"/"yes"/"on"`` (case
    insensitive), and numbers. Anything else (including ``None`` and ``""``)
    is falsy.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def project_mcp_path(cwd: str | Path | None = None) -> Path:
    return Path(cwd or Path.cwd()) / ".mcp.json"


def _read_json_file(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        return {}
    return json.loads(file_path.read_text(encoding="utf-8"))


def read_settings_file(file_path: Path) -> dict[str, Any]:
    return _read_json_file(file_path)


def read_mcp_config_file(file_path: Path) -> dict[str, Any]:
    parsed = _read_json_file(file_path)
    if not isinstance(parsed, dict):
        return {}
    mcp_servers = parsed.get("mcpServers", {})
    return mcp_servers if isinstance(mcp_servers, dict) else {}


def merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged_mcp = dict(base.get("mcpServers", {}))
    for name, server in override.get("mcpServers", {}).items():
        current = dict(merged_mcp.get(name, {}))
        next_server = dict(server)
        current.update(next_server)
        current["env"] = {
            **dict(merged_mcp.get(name, {}).get("env", {})),
            **dict(next_server.get("env", {})),
        }
        merged_mcp[name] = current

    return {
        **base,
        **override,
        "env": {
            **dict(base.get("env", {})),
            **dict(override.get("env", {})),
        },
        "mcpServers": merged_mcp,
    }


def load_effective_settings(cwd: str | Path | None = None) -> dict[str, Any]:
    claude_settings = read_settings_file(CLAUDE_SETTINGS_PATH)
    global_mcp = read_mcp_config_file(PEPSI_CODE_MCP_PATH)
    project_mcp = read_mcp_config_file(project_mcp_path(cwd))
    mini_code_settings = read_settings_file(PEPSI_CODE_SETTINGS_PATH)

    effective = merge_settings(
        merge_settings(
            merge_settings(claude_settings, {"mcpServers": global_mcp}),
            {"mcpServers": project_mcp},
        ),
        mini_code_settings,
    )
    if mini_code_settings.get("model"):
        effective["_modelSource"] = str(PEPSI_CODE_SETTINGS_PATH)
    elif claude_settings.get("model"):
        effective["_modelSource"] = str(CLAUDE_SETTINGS_PATH)
    if "providers" in mini_code_settings:
        # A Claude alias / thinking default must never leak into a domestic provider profile.
        for key in ("model", "reasoningEffort", "fallbackModel", "models", "modelsUrl", "maxOutputTokens"):
            if key not in mini_code_settings:
                effective.pop(key, None)
    return effective


def save_mini_code_settings(updates: dict[str, Any]) -> None:
    PEPSI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_settings_file(PEPSI_CODE_SETTINGS_PATH)
    next_settings = merge_settings(existing, updates)
    # Setup may save credentials: a failed write must not truncate the existing configuration.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=PEPSI_CODE_SETTINGS_PATH.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(next_settings, indent=2) + "\n")
        os.replace(temporary, PEPSI_CODE_SETTINGS_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_runtime_config(cwd: str | Path | None = None) -> dict[str, Any]:
    effective = load_effective_settings(cwd)
    env = {**dict(effective.get("env", {})), **os.environ}
    model = os.environ.get("PEPSI_CODE_MODEL") or effective.get("model") or str(env.get("ANTHROPIC_MODEL", "")).strip()
    base_url = str(env.get("ANTHROPIC_BASE_URL", "")).strip() or "https://api.anthropic.com"
    auth_token = str(env.get("ANTHROPIC_AUTH_TOKEN", "")).strip() or None
    api_key = str(env.get("ANTHROPIC_API_KEY", "")).strip() or None
    raw_max_output_tokens = (
        os.environ.get("PEPSI_CODE_MAX_OUTPUT_TOKENS")
        or effective.get("maxOutputTokens")
        or env.get("PEPSI_CODE_MAX_OUTPUT_TOKENS")
    )
    max_output_tokens = None
    if raw_max_output_tokens is not None:
        try:
            parsed = int(raw_max_output_tokens)
            if parsed > 0:
                max_output_tokens = parsed
        except (TypeError, ValueError):
            max_output_tokens = None

    fallback_model = (
        os.environ.get("PEPSI_CODE_FALLBACK_MODEL")
        or effective.get("fallbackModel")
        or str(env.get("ANTHROPIC_FALLBACK_MODEL", "")).strip()
        or None
    )

    # Loop-engineering knobs (opt-in). Parsed here so future wiring can read
    # them from the runtime config; not yet consumed by the run path.
    raw_budget_limit = os.environ.get("PEPSI_CODE_BUDGET_LIMIT") or effective.get("budgetLimitUsd")
    budget_limit_usd: float | None = None
    if raw_budget_limit is not None:
        try:
            budget_limit_usd = float(raw_budget_limit)
        except (TypeError, ValueError):
            budget_limit_usd = None

    raw_max_iterations = os.environ.get("PEPSI_CODE_MAX_ITERATIONS") or effective.get("maxIterations")
    max_iterations = 3
    if raw_max_iterations is not None:
        try:
            parsed_iterations = int(raw_max_iterations)
            if parsed_iterations > 0:
                max_iterations = parsed_iterations
        except (TypeError, ValueError):
            pass

    verifier_enabled = _truthy(os.environ.get("PEPSI_CODE_VERIFIER_ENABLED") or effective.get("verifierEnabled"))

    reasoning_effort = os.environ.get("PEPSI_CODE_REASONING_EFFORT") or effective.get("reasoningEffort", "default")
    models = effective.get("models", [])
    if not isinstance(models, list) or any(
        not isinstance(item, (str, dict)) or (isinstance(item, dict) and not isinstance(item.get("id"), str))
        for item in models
    ):
        raise ValueError("models must be a list of model IDs or objects with an id")
    # Capability validation happens after discovery. Providers may add new IDs or effort levels.
    if not isinstance(reasoning_effort, str) or not reasoning_effort or any(c.isspace() for c in reasoning_effort):
        raise ValueError("reasoningEffort must be a non-empty API effort level")

    runtime = {
        "model": model,
        "modelSource": (
            "环境变量 PEPSI_CODE_MODEL"
            if os.environ.get("PEPSI_CODE_MODEL")
            else effective.get("_modelSource", "settings.json")
            if effective.get("model")
            else "ANTHROPIC_MODEL"
        ),
        "reasoningEffort": reasoning_effort,
        "models": models,
        "modelsUrl": effective.get("modelsUrl"),
        "fallbackModel": fallback_model,
        "baseUrl": base_url,
        "authToken": auth_token,
        "apiKey": api_key,
        "maxOutputTokens": max_output_tokens,
        "governance": bool(effective.get("governance", False)),
        "mcpServers": effective.get("mcpServers", {}),
        "budgetLimitUsd": budget_limit_usd,
        "maxIterations": max_iterations,
        "verifierEnabled": verifier_enabled,
        "sourceSummary": f"config: {PEPSI_CODE_SETTINGS_PATH} > {CLAUDE_SETTINGS_PATH} > process.env",
    }
    configure_providers(runtime, effective, env)
    if not runtime["model"] and not runtime.get("_providerRuntimes"):
        raise RuntimeError("No model configured. Set ~/.pepsi-code/settings.json or ANTHROPIC_MODEL.")
    if not runtime.get("authToken") and not runtime.get("apiKey"):
        raise RuntimeError("No auth configured. Configure the selected provider's API Key.")
    return runtime


def get_mcp_config_path(scope: str, cwd: str | Path | None = None) -> Path:
    return project_mcp_path(cwd) if scope == "project" else PEPSI_CODE_MCP_PATH


def load_scoped_mcp_servers(scope: str, cwd: str | Path | None = None) -> dict[str, Any]:
    return read_mcp_config_file(get_mcp_config_path(scope, cwd))


def save_scoped_mcp_servers(scope: str, servers: dict[str, Any], cwd: str | Path | None = None) -> None:
    target = get_mcp_config_path(scope, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")


# MCP Token 存储（用于 HTTP 传输的 Bearer Token 认证）
PEPSI_CODE_MCP_TOKENS_PATH = PEPSI_CODE_DIR / "mcp-tokens.json"


def read_mcp_tokens() -> dict[str, str]:
    """读取 MCP 服务器的 Bearer Token 存储。"""
    if not PEPSI_CODE_MCP_TOKENS_PATH.exists():
        return {}
    try:
        parsed = json.loads(PEPSI_CODE_MCP_TOKENS_PATH.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_mcp_tokens(tokens: dict[str, str]) -> None:
    """保存 MCP 服务器的 Bearer Token 存储。"""
    PEPSI_CODE_MCP_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PEPSI_CODE_MCP_TOKENS_PATH.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")


def validate_config(cwd: str | Path | None = None) -> tuple[bool, list[str]]:
    """验证配置完整性，返回 (是否有效，错误列表)

    检查项：
    1. 模型名称是否配置
    2. API key 是否配置
    3. MCP 配置文件是否合法

    模型是否可用由运行时读取的 API 列表判断，不维护静态模型名称表。
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        config = load_runtime_config(cwd)

        # 检查 MCP 配置
        mcp_servers = config.get("mcpServers", {})
        for name, server in mcp_servers.items():
            if not server.get("command") and not server.get("url"):
                errors.append(f"MCP server '{name}' has neither command nor url configured")

        return len(errors) == 0, errors + warnings

    except RuntimeError as e:
        error_msg = str(e)

        # 提供友好的错误消息
        if "No model configured" in error_msg:
            help_msg = (
                f"Error: {error_msg}\n\n"
                "How to fix:\n"
                "  1. Set a model ID returned by your provider's /models endpoint\n"
                "  2. Or edit ~/.pepsi-code/settings.json:\n"
                '     {"model": "<provider-model-id>"}\n'
            )
            errors.append(help_msg)

        elif "No auth configured" in error_msg:
            help_msg = (
                f"Error: {error_msg}\n\n"
                "How to fix:\n"
                "  1. Set API key: export ANTHROPIC_API_KEY=<your-provider-key>\n"
                "  2. Or edit ~/.pepsi-code/settings.json:\n"
                '     {"env": {"ANTHROPIC_API_KEY": "<your-provider-key>"}}\n'
            )
            errors.append(help_msg)
        else:
            errors.append(str(e))

        return False, errors
    except Exception as e:
        return False, [f"Unexpected error: {e}"]


def format_config_diagnostic(cwd: str | Path | None = None) -> str:
    """格式化配置诊断信息"""
    is_valid, messages = validate_config(cwd)

    lines = ["Configuration Diagnostics", "=" * 40, ""]

    if is_valid:
        lines.append("Status: OK")
        if messages:
            lines.append("")
            lines.append("Warnings:")
            for msg in messages:
                lines.append(f"  ⚠️  {msg}")
    else:
        lines.append("Status: ERRORS")
        lines.append("")
        lines.append("Errors:")
        for msg in messages:
            lines.append(f"  ❌ {msg}")

    # 显示当前配置摘要
    try:
        config = load_runtime_config(cwd)
        lines.append("")
        lines.append("Current Configuration")
        lines.append("-" * 40)
        lines.append(f"  Model: {config.get('model', 'not set')}")
        lines.append(f"  Base URL: {config.get('baseUrl', 'not set')}")
        auth_method = (
            "ANTHROPIC_AUTH_TOKEN"
            if config.get("authToken")
            else ("ANTHROPIC_API_KEY" if config.get("apiKey") else "not set")
        )
        lines.append(f"  Auth: {auth_method}")
        lines.append(f"  MCP Servers: {len(config.get('mcpServers', {}))}")
    except Exception:
        pass

    return "\n".join(lines)
