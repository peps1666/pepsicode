import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.web_search import web_search_tool


def test_tavily_search_returns_results_without_exposing_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "test-secret")
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        return io.BytesIO(
            json.dumps({"results": [{"title": "Example", "url": "https://example.com", "content": "Text"}]}).encode()
        )

    monkeypatch.setattr("urllib.request.build_opener", lambda *_a: SimpleNamespace(open=send))
    result = ToolRegistry([web_search_tool]).execute(
        "web_search", {"query": "python", "num_results": 2}, ToolContext(str(tmp_path))
    )
    assert result.ok
    assert "Example" in result.output and "https://example.com" in result.output
    assert "test-secret" not in result.output
    assert requests[0].get_header("Authorization") == "Bearer test-secret"
    assert json.loads(requests[0].data)["search_depth"] == "basic"
    assert "test-secret" not in requests[0].data.decode()


def test_missing_key_fails_without_network(monkeypatch, tmp_path):
    import pepsicode.tools.web_search as search

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        search, "tavily_search", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected network"))
    )
    result = ToolRegistry([web_search_tool]).execute("web_search", {"query": "python"}, ToolContext(str(tmp_path)))
    assert not result.ok
    assert "TAVILY_API_KEY" in result.output


def test_invalid_key_reports_auth_error_without_response_body(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "test-secret")

    def send(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"test-secret"))

    monkeypatch.setattr("urllib.request.build_opener", lambda *_a: SimpleNamespace(open=send))
    result = ToolRegistry([web_search_tool]).execute("web_search", {"query": "python"}, ToolContext(str(tmp_path)))
    assert not result.ok
    assert "Invalid Tavily API key" in result.output
    assert "test-secret" not in result.output


@pytest.mark.parametrize("mode", ["plan", "read_only"])
def test_readonly_modes_allow_fixed_tavily_search(monkeypatch, tmp_path, mode):
    from pepsicode.permissions import PermissionManager, PermissionMode

    monkeypatch.setenv("TAVILY_API_KEY", "test-secret")
    permissions = PermissionManager(str(tmp_path))
    if mode == "plan":
        permissions.enter_plan_mode()
    else:
        permissions.set_access_mode(PermissionMode.READ_ONLY)
    sent = []

    def send(request, **kwargs):
        sent.append(request)
        return io.BytesIO(b'{"results": [{"title": "Example", "url": "https://example.com"}]}')

    monkeypatch.setattr("urllib.request.build_opener", lambda *_a: SimpleNamespace(open=send))
    result = ToolRegistry([web_search_tool]).execute(
        "web_search", {"query": "python"}, ToolContext(str(tmp_path), permissions=permissions)
    )
    assert result.ok
    assert len(sent) == 1
    assert sent[0].full_url == "https://api.tavily.com/search"


def test_tavily_rejects_redirect_before_sending_credentials(monkeypatch, tmp_path):
    from pepsicode.execution.external import tavily_search

    def opener(handler):
        with pytest.raises(PermissionError, match="redirects"):
            handler.redirect_request(None, None, 302, "", {}, "https://attacker.example/")
        return SimpleNamespace(open=lambda *_a, **_kw: io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.build_opener", opener)
    with tavily_search("query", 1, "test-secret", context=ToolContext(str(tmp_path))):
        pass


def test_unrelated_post_still_blocked_in_plan(monkeypatch, tmp_path):
    import urllib.request

    from pepsicode.execution.external import urlopen
    from pepsicode.permissions import PermissionManager

    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    sent = []
    monkeypatch.setattr("urllib.request.build_opener", lambda *_a: sent.append(True))
    request = urllib.request.Request("https://api.tavily.com/other", data=b"{}", method="POST")
    with pytest.raises(PermissionError):
        urlopen(request, context=ToolContext(str(tmp_path), permissions=permissions))
    assert not sent
