import json

import pytest

from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.tooling import ToolCapability, ToolContext, ToolDefinition, ToolRegistry, ToolResult


def registry():
    def tool(name, description):
        return ToolDefinition(
            name, description, {"type": "object"}, lambda value: value, lambda value, context: ToolResult(True, "ok")
        )

    return ToolRegistry(
        [
            tool("read_file", "Read a file"),
            tool("mcp__postgres__query", "Query database SQL tables"),
            tool("mcp__docker__logs", "Read Docker container logs"),
            tool("code_review", "Review code for errors"),
        ],
        deferred=True,
    )


def test_deferred_schemas_are_loaded_by_search_on_next_model_request(tmp_path):
    tools = registry()
    adapter = AnthropicModelAdapter({"model": "test", "baseUrl": "https://example.test", "apiKey": "test"}, tools)
    request = json.loads(adapter._build_stream_request("test", "system", []).data)
    assert [tool["name"] for tool in request["tools"]] == ["read_file", "search_tools"]
    result = tools.execute("search_tools", {"query": "数据库", "limit": 1}, ToolContext(str(tmp_path)))
    assert result.ok and "mcp__postgres__query" in result.output
    request = json.loads(adapter._build_request("test", "system", []).data)
    assert [tool["name"] for tool in request["tools"]] == ["read_file", "mcp__postgres__query", "search_tools"]
    assert tools.find("search_tools").is_read_only
    assert len(tools.list()) == 5  # CLI inventory is still complete.


def test_exact_match_and_repeated_search_do_not_duplicate_schema(tmp_path):
    tools = registry()
    for _ in range(3):
        result = tools.execute("search_tools", {"query": "code_review", "limit": 1}, ToolContext(str(tmp_path)))
        assert result.output.startswith("code_review:")
    assert [tool.name for tool in tools.model_tools()].count("code_review") == 1


def test_resume_recovers_used_tools_from_both_message_formats():
    tools = registry()
    assert "code_review" in [
        tool.name for tool in tools.model_tools([{"role": "assistant_tool_call", "toolName": "code_review"}])
    ]
    assert "mcp__docker__logs" in [
        tool.name
        for tool in tools.model_tools(
            [{"role": "assistant", "content": [{"type": "tool_use", "name": "mcp__docker__logs"}]}]
        )
    ]
    tools.restore_loaded_tools(None)
    assert tools.loaded_tool_names() == ["read_file", "search_tools"]


def test_discovery_persists_unused_tool_and_stable_order(tmp_path, monkeypatch):
    from pepsicode.core import session as sessions
    from pepsicode.core.session_tree import recording_session
    from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, bind_scope

    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    session = sessions.create_new_session(str(tmp_path))
    session.messages = [{"role": "user", "content": "find a tool"}]
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    scope = ExecutionScope(str(tmp_path), journal=journal)
    original = registry()
    from pepsicode.execution.model_requests import _begin
    from pepsicode.execution.prefix_diff import compare_prepared

    adapter = AnthropicModelAdapter({"model": "test", "baseUrl": "https://example.test", "apiKey": "test"}, original)
    with bind_scope(scope), recording_session(session):
        _begin(scope, adapter._build_request("test", "system", []))
        assert original.execute(
            "search_tools", {"query": "code_review"}, ToolContext(str(tmp_path), execution=scope)
        ).ok
        assert original.execute(
            "search_tools", {"query": "code_review"}, ToolContext(str(tmp_path), execution=scope)
        ).ok
        _begin(scope, adapter._build_request("test", "system", []))
    loaded = sessions.load_session(session.session_id)
    assert loaded is not None
    restored = registry()
    restored.restore_loaded_tools(loaded.loaded_tools)
    assert restored.loaded_tool_names() == original.loaded_tool_names()
    assert "code_review" in restored.loaded_tool_names()
    records = ExecutionJournal.read(journal.path)
    assert len([x for x in records if x["event"] == "model/prefix-break"]) == 1
    assert compare_prepared(records)[0]["prefix_breaks"] == [{"reason": "tool-discovery", "added_count": 1}]


@pytest.mark.parametrize("mode", ["eager", "lazy", "auto"])
def test_configured_tool_loading_has_stable_initial_view(tmp_path, mode):
    from pepsicode.tools import create_default_tool_registry

    tools = create_default_tool_registry(str(tmp_path), {"model": "test", "toolLoading": mode, "mcpServers": {}})
    try:
        if mode in {"eager", "auto"}:
            assert tools.loaded_tool_names() == [tool.name for tool in tools.list()]
        else:
            assert len(tools.loaded_tool_names()) < len(tools.list())
        assert tools.loaded_tool_names() == [tool.name for tool in tools.model_tools()]
    finally:
        tools.dispose()


def test_auto_tool_loading_respects_small_configured_window(tmp_path):
    from pepsicode.tools import create_default_tool_registry

    tools = create_default_tool_registry(
        str(tmp_path), {"model": "test", "models": [{"id": "test", "contextWindow": 300}], "toolLoading": "auto"}
    )
    try:
        assert len(tools.loaded_tool_names()) < len(tools.list())
    finally:
        tools.dispose()


def test_search_cannot_discover_tools_outside_filtered_registry(tmp_path):
    source = registry()
    filtered = ToolRegistry([source.find("read_file")], deferred=True)
    result = filtered.execute("search_tools", {"query": "postgres database"}, ToolContext(str(tmp_path)))
    assert "No matching" in result.output
    assert filtered.find("mcp__postgres__query") is None


@pytest.mark.parametrize(
    "value", [{}, {"query": ""}, {"query": "docker", "limit": True}, {"query": "docker", "limit": 100}]
)
def test_tool_search_validates_arguments(tmp_path, value):
    assert not registry().execute("search_tools", value, ToolContext(str(tmp_path))).ok


def test_full_mode_preserves_existing_schema_behavior():
    tool = ToolDefinition(
        "example",
        "",
        {},
        lambda value: value,
        lambda value, context: ToolResult(True, ""),
        capabilities={ToolCapability.READ_ONLY},
    )
    tools = ToolRegistry([tool])
    assert tools.model_tools() == tools.list() == [tool]


@pytest.mark.parametrize("enabled", [False, True])
def test_prompt_cache_is_explicit_and_shared_by_stream_and_nonstream(enabled):
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "test", "promptCaching": enabled}, registry()
    )
    for build in (adapter._build_request, adapter._build_stream_request):
        body = json.loads(build("test", "system", []).data)
        assert ("cache_control" in body) is enabled


def test_broad_requests_can_browse_all_capabilities_with_pagination(tmp_path):
    tools = registry()
    found = []
    for offset in (0, 2):
        result = tools.execute("search_tools", {"query": "*", "limit": 2, "offset": offset}, ToolContext(str(tmp_path)))
        found.extend(line.split(":", 1)[0] for line in result.output.splitlines() if not line.startswith("Showing"))
    assert set(found) == {"read_file", "mcp__postgres__query", "mcp__docker__logs", "code_review"}
    assert len(found) == len(set(found))


def test_extensions_can_declare_search_metadata_aliases_and_core_visibility(tmp_path):
    tool = ToolDefinition(
        "custom_route_optimizer",
        "Analyze routes",
        {"type": "object", "properties": {"fleet_id": {"type": "string"}}},
        lambda value: value,
        lambda value, context: ToolResult(True, "done"),
        tags=["logistics"],
        aliases=["运力规划"],
        always_loaded=True,
    )
    tools = ToolRegistry([tool], deferred=True, core_tools=[], search_aliases={"车队": ["fleet_id", "logistics"]})
    assert [item.name for item in tools.model_tools()] == [tool.name, "search_tools"]
    for query in ("车队", "运力规划", "fleet_id", "logistics"):
        assert tool.name in tools.execute("search_tools", {"query": query}, ToolContext(str(tmp_path))).output


def test_core_tool_overrides_are_used_instead_of_fixed_names():
    original = registry()
    tools = ToolRegistry(
        [tool for tool in original.list() if tool.name != "search_tools"],
        deferred=True,
        core_tools=["mcp__docker__logs"],
    )
    assert [tool.name for tool in tools.model_tools()] == ["mcp__docker__logs", "search_tools"]


@pytest.mark.parametrize("value", [{"query": "*", "offset": -1}, {"query": "*", "offset": True}])
def test_catalog_pagination_validates_arguments(tmp_path, value):
    assert not registry().execute("search_tools", value, ToolContext(str(tmp_path))).ok


def test_copying_discovery_into_filtered_registry_rebinds_its_scope(tmp_path):
    parent = registry()
    filtered = ToolRegistry([parent.find("read_file"), parent.find("search_tools")])
    result = filtered.execute("search_tools", {"query": "*"}, ToolContext(str(tmp_path)))
    assert "read_file:" in result.output
    assert "mcp__" not in result.output and "code_review:" not in result.output
    assert filtered.find("search_tools").run.__self__ is filtered
