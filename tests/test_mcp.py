from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from pepsicode.mcp import (
    HttpMcpClient,
    StdioMcpClient,
    _create_client,
    _interpolate_env,
    create_mcp_backed_tools,
)
from pepsicode.tooling import ToolContext

# ---------------------------------------------------------------------------
# Stdio tests (existing)
# ---------------------------------------------------------------------------


def test_create_mcp_backed_tools_supports_newline_json(tmp_path: Path) -> None:
    server_script = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    mcp = create_mcp_backed_tools(
        cwd=str(tmp_path),
        mcp_servers={
            "fake": {
                "command": "python",
                "args": [str(server_script)],
                "protocol": "newline-json",
            }
        },
    )

    names = [tool.name for tool in mcp["tools"]]
    assert "mcp__fake__echo" in names
    assert "list_mcp_resources" in names
    assert "list_mcp_prompts" in names

    echo_tool = next(tool for tool in mcp["tools"] if tool.name == "mcp__fake__echo")
    result = echo_tool.run({"text": "hi"}, ToolContext(cwd=str(tmp_path)))
    assert result.ok is True
    assert result.output == "echo:hi"

    resource_tool = next(tool for tool in mcp["tools"] if tool.name == "read_mcp_resource")
    resource_result = resource_tool.run({"server": "fake", "uri": "fake://hello"}, ToolContext(cwd=str(tmp_path)))
    assert "hello resource" in resource_result.output

    prompt_tool = next(tool for tool in mcp["tools"] if tool.name == "get_mcp_prompt")
    prompt_result = prompt_tool.run(
        {"server": "fake", "name": "hello", "arguments": {"name": "cc"}}, ToolContext(cwd=str(tmp_path))
    )
    assert "hello cc" in prompt_result.output

    mcp["dispose"]()


# ---------------------------------------------------------------------------
# _interpolate_env tests
# ---------------------------------------------------------------------------


def test_interpolate_env_replaces_dollar_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "secret-abc")
    assert _interpolate_env("Bearer $MY_TOKEN") == "Bearer secret-abc"


def test_interpolate_env_replaces_braced_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "key-123")
    assert _interpolate_env("${API_KEY}") == "key-123"


def test_interpolate_env_preserves_unknown_vars() -> None:
    assert _interpolate_env("$NONEXISTENT_VAR_XYZ") == "$NONEXISTENT_VAR_XYZ"


def test_interpolate_env_no_vars_unchanged() -> None:
    assert _interpolate_env("just a string") == "just a string"


# ---------------------------------------------------------------------------
# Fake HTTP MCP server for testing
# ---------------------------------------------------------------------------


class _FakeMcpHandler(BaseHTTPRequestHandler):
    """处理 JSON-RPC 请求的 HTTP handler，模拟 MCP 服务器。"""

    # 存储最近收到的请求头（用于断言 token 等）
    last_headers: dict[str, str] = {}

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 禁用日志

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        message = json.loads(body)

        # 记录请求头（供测试断言）
        _FakeMcpHandler.last_headers = dict(self.headers)

        method = message.get("method", "")
        message_id = message.get("id")
        result = self._handle_method(method, message)

        response = {"jsonrpc": "2.0", "id": message_id, "result": result}
        response_bytes = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _handle_method(self, method: str, message: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"serverInfo": {"name": "fake-http"}}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text via HTTP",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        if method == "tools/call":
            args = message.get("params", {}).get("arguments", {})
            return {"content": [{"type": "text", "text": f"http-echo:{args.get('text', '')}"}]}
        if method == "resources/list":
            return {"resources": [{"uri": "fake://hello", "name": "Hello HTTP"}]}
        if method == "resources/read":
            return {"contents": [{"uri": "fake://hello", "text": "http hello resource"}]}
        if method == "prompts/list":
            return {"prompts": [{"name": "hello", "arguments": [{"name": "name", "required": True}]}]}
        if method == "prompts/get":
            name = message.get("params", {}).get("arguments", {}).get("name", "world")
            return {"messages": [{"role": "user", "content": f"hello {name}"}]}
        return {}


class _FakeMcpServer:
    """在后台线程运行的 HTTP MCP 测试服务器。"""

    def __init__(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _FakeMcpHandler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()


# ---------------------------------------------------------------------------
# HttpMcpClient tests
# ---------------------------------------------------------------------------


def test_http_mcp_client_start_and_list_tools() -> None:
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("test", {"url": server.url}, "/tmp")
        client.start()
        tools = client.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "echo"
        assert tools[0]["description"] == "Echo text via HTTP"
    finally:
        server.close()


def test_http_mcp_client_call_tool() -> None:
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("test", {"url": server.url}, "/tmp")
        client.start()
        result = client.call_tool("echo", {"text": "world"})
        assert result.ok is True
        assert result.output == "http-echo:world"
    finally:
        server.close()


def test_http_mcp_client_list_resources() -> None:
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("test", {"url": server.url}, "/tmp")
        client.start()
        resources = client.list_resources()
        assert len(resources) == 1
        assert resources[0]["uri"] == "fake://hello"
    finally:
        server.close()


def test_http_mcp_client_read_resource() -> None:
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("test", {"url": server.url}, "/tmp")
        client.start()
        result = client.read_resource("fake://hello")
        assert result.ok is True
        assert "http hello resource" in result.output
    finally:
        server.close()


def test_http_mcp_client_list_prompts() -> None:
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("test", {"url": server.url}, "/tmp")
        client.start()
        prompts = client.list_prompts()
        assert len(prompts) == 1
        assert prompts[0]["name"] == "hello"
    finally:
        server.close()


def test_http_mcp_client_get_prompt() -> None:
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("test", {"url": server.url}, "/tmp")
        client.start()
        result = client.get_prompt("hello", {"name": "alice"})
        assert result.ok is True
        assert "hello alice" in result.output
    finally:
        server.close()


def test_http_mcp_client_close_is_noop() -> None:
    """HTTP 客户端 close 不应报错。"""
    client = HttpMcpClient("test", {"url": "http://localhost:1"}, "/tmp")
    client.close()  # 不应抛异常


def test_http_mcp_client_start_without_url_raises() -> None:
    client = HttpMcpClient("test", {}, "/tmp")
    with pytest.raises(RuntimeError, match="no url configured"):
        client.start()


# ---------------------------------------------------------------------------
# Token injection tests
# ---------------------------------------------------------------------------


def test_http_mcp_client_injects_bearer_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bearer Token 应从 token 存储注入到请求头。"""
    token_file = tmp_path / "mcp-tokens.json"
    token_file.write_text(json.dumps({"my-server": "tok_abc123"}))
    monkeypatch.setattr(
        "pepsicode.config.read_mcp_tokens",
        lambda: json.loads(token_file.read_text()),
    )
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient("my-server", {"url": server.url}, "/tmp")
        client.start()
        # 触发一个请求以记录 headers
        client.list_tools()
        auth = _FakeMcpHandler.last_headers.get("Authorization", "")
        assert auth == "Bearer tok_abc123"
    finally:
        server.close()


def test_http_mcp_client_custom_headers_with_env_interpolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自定义 headers 应支持 $ENV_VAR 插值。"""
    monkeypatch.setenv("CUSTOM_VAL", "env-value-999")
    server = _FakeMcpServer()
    try:
        client = HttpMcpClient(
            "test",
            {"url": server.url, "headers": {"X-Custom": "$CUSTOM_VAL"}},
            "/tmp",
        )
        client.start()
        client.list_tools()
        custom = _FakeMcpHandler.last_headers.get("X-Custom", "")
        assert custom == "env-value-999"
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Factory function tests
# ---------------------------------------------------------------------------


def test_create_client_returns_http_when_url_present() -> None:
    client = _create_client("test", {"url": "http://example.com"}, "/tmp")
    assert isinstance(client, HttpMcpClient)


def test_create_client_returns_stdio_when_no_url() -> None:
    client = _create_client("test", {"command": "echo"}, "/tmp")
    assert isinstance(client, StdioMcpClient)


# ---------------------------------------------------------------------------
# create_mcp_backed_tools with HTTP server
# ---------------------------------------------------------------------------


def test_create_mcp_backed_tools_with_http_server(tmp_path: Path) -> None:
    """端到端测试：HTTP MCP 服务器 + create_mcp_backed_tools。"""
    server = _FakeMcpServer()
    try:
        mcp = create_mcp_backed_tools(
            cwd=str(tmp_path),
            mcp_servers={
                "remote": {"url": server.url},
            },
        )
        names = [tool.name for tool in mcp["tools"]]
        assert "mcp__remote__echo" in names
        assert "list_mcp_resources" in names
        assert "list_mcp_prompts" in names

        echo_tool = next(t for t in mcp["tools"] if t.name == "mcp__remote__echo")
        result = echo_tool.run({"text": "hello"}, ToolContext(cwd=str(tmp_path)))
        assert result.ok is True
        assert result.output == "http-echo:hello"

        mcp["dispose"]()
    finally:
        server.close()
