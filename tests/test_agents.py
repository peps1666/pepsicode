"""Tests for the agents subsystem: trace manager, tool filter, markdown loader."""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

from pepsicode.agents.parser import parse_frontmatter
from pepsicode.agents.tool_filter import resolve_agent_tools
from pepsicode.agents.trace import TraceManager
from pepsicode.sub_agents import AgentDefinition, AgentType
from pepsicode.tooling import ToolDefinition, ToolRegistry, ToolResult

# ---------------------------------------------------------------------------
# TraceManager
# ---------------------------------------------------------------------------


class TestTraceManager:
    def test_create_and_complete(self):
        tm = TraceManager(session_id="root")
        node = tm.create(agent_type="explore", name="Explore")
        assert node.agent_type == "explore"
        assert node.status == "running"
        assert node.parent_id == "root"  # defaults to session_id

        tm.record_tokens(node.trace_id, 100, 50)
        tm.record_tool_call(node.trace_id)
        tm.complete(node.trace_id)

        refreshed = tm.get(node.trace_id)
        assert refreshed is not None
        assert refreshed.status == "completed"
        assert refreshed.input_tokens == 100
        assert refreshed.output_tokens == 50
        assert refreshed.tool_call_count == 1
        assert refreshed.ended_at is not None

    def test_get_total_aggregates_descendants(self):
        tm = TraceManager(session_id="root")
        parent = tm.create(agent_type="general", name="General")
        child1 = tm.create(agent_type="explore", name="Explore", parent_id=parent.trace_id)
        child2 = tm.create(agent_type="explore", name="Explore", parent_id=parent.trace_id)

        tm.record_tokens(child1.trace_id, 100, 50)
        tm.record_tokens(child2.trace_id, 200, 100)
        tm.record_tokens(parent.trace_id, 50, 25)

        # Total for parent includes its own + children
        totals = tm.get_total(parent.trace_id)
        assert totals["input_tokens"] == 350
        assert totals["output_tokens"] == 175

        # Total for session root includes everything
        all_totals = tm.get_total(None)
        assert all_totals["input_tokens"] == 350

    def test_get_total_nonexistent_returns_zeros(self):
        tm = TraceManager()
        totals = tm.get_total("nonexistent")
        assert totals["input_tokens"] == 0
        assert totals["output_tokens"] == 0

    def test_format_summary(self):
        tm = TraceManager()
        node = tm.create(agent_type="explore", name="Explore")
        tm.record_tokens(node.trace_id, 100, 50)
        tm.record_tool_call(node.trace_id)
        summary = tm.format_summary(node.trace_id)
        assert "in=100" in summary
        assert "out=50" in summary
        assert "tool_calls=1" in summary

    def test_complete_failed_status(self):
        tm = TraceManager()
        node = tm.create(agent_type="explore", name="Explore")
        tm.complete(node.trace_id, status="failed")
        assert tm.get(node.trace_id).status == "failed"

    def test_record_tokens_nonexistent_noop(self):
        tm = TraceManager()
        # Should not raise
        tm.record_tokens("nonexistent", 100, 50)
        tm.record_tool_call("nonexistent")
        tm.complete("nonexistent")


# ---------------------------------------------------------------------------
# Tool filter
# ---------------------------------------------------------------------------


def _make_tool(name: str) -> ToolDefinition:
    """Create a minimal ToolDefinition with the given name."""
    return ToolDefinition(
        name=name,
        description="test",
        input_schema={},
        validator=lambda x: x,
        run=lambda x, ctx: ToolResult(ok=True, output=""),
    )


class TestToolFilter:
    def test_global_disallow_removes_task_and_ask_user(self):
        registry = ToolRegistry([_make_tool("task"), _make_tool("ask_user"), _make_tool("read_file")])
        defn = AgentDefinition(
            type=AgentType.EXPLORE,
            name="Explore",
            description="",
            system_prompt_template="",
        )
        result = resolve_agent_tools(registry, defn)
        names = {t.name for t in result.list()}
        assert "read_file" in names
        assert "task" not in names
        assert "ask_user" not in names

    def test_allowed_tools_whitelist(self):
        registry = ToolRegistry([_make_tool("read_file"), _make_tool("write_file"), _make_tool("run_command")])
        defn = AgentDefinition(
            type=AgentType.EXPLORE,
            name="Explore",
            description="",
            system_prompt_template="",
            allowed_tools=["read_file"],
        )
        result = resolve_agent_tools(registry, defn)
        names = {t.name for t in result.list()}
        assert names == {"read_file"}

    def test_disallowed_tools(self):
        registry = ToolRegistry([_make_tool("read_file"), _make_tool("write_file"), _make_tool("run_command")])
        defn = AgentDefinition(
            type=AgentType.GENERAL,
            name="General",
            description="",
            system_prompt_template="",
            disallowed_tools=["write_file"],
        )
        result = resolve_agent_tools(registry, defn)
        names = {t.name for t in result.list()}
        assert "read_file" in names
        assert "run_command" in names
        assert "write_file" not in names

    def test_read_only_flag_enforces_capability_boundary(self):
        registry = ToolRegistry([_make_tool("read_file"), _make_tool("write_file"), _make_tool("run_command")])
        defn = AgentDefinition(
            type=AgentType.GENERAL,
            name="Custom Read Only",
            description="",
            system_prompt_template="",
            is_read_only=True,
        )
        result = resolve_agent_tools(registry, defn)
        assert {tool.name for tool in result.list()} == {"read_file"}

    def test_empty_pool_returns_empty(self):
        registry = ToolRegistry([])
        defn = AgentDefinition(
            type=AgentType.GENERAL,
            name="General",
            description="",
            system_prompt_template="",
        )
        result = resolve_agent_tools(registry, defn)
        assert result.list() == []


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_basic_key_value(self):
        raw = "---\nname: Explore\nmaxTurns: 5\n---\nbody text"
        fm, body = parse_frontmatter(raw)
        assert fm["name"] == "Explore"
        assert fm["maxTurns"] == 5
        assert body == "body text"

    def test_list_values(self):
        raw = "---\nallowedTools:\n  - read_file\n  - grep_files\n---\nbody"
        fm, body = parse_frontmatter(raw)
        assert fm["allowedTools"] == ["read_file", "grep_files"]
        assert body == "body"

    def test_boolean_values(self):
        raw = "---\nisReadOnly: true\nisReadOnly2: false\n---\nbody"
        fm, _ = parse_frontmatter(raw)
        assert fm["isReadOnly"] is True
        assert fm["isReadOnly2"] is False

    def test_empty_list(self):
        raw = "---\ndisallowedTools: []\n---\nbody"
        fm, _ = parse_frontmatter(raw)
        assert fm["disallowedTools"] == []

    def test_no_frontmatter(self):
        raw = "just body text"
        fm, body = parse_frontmatter(raw)
        assert fm == {}
        assert body == raw

    def test_no_closing_delim(self):
        raw = "---\nname: Explore\nbody without closing"
        fm, body = parse_frontmatter(raw)
        assert fm == {}
        assert body == raw

    def test_quoted_strings(self):
        raw = "---\nname: \"My Agent\"\ndesc: 'single quoted'\n---\nbody"
        fm, _ = parse_frontmatter(raw)
        assert fm["name"] == "My Agent"
        assert fm["desc"] == "single quoted"

    def test_comments_ignored(self):
        raw = "---\n# comment\nname: Explore\n---\nbody"
        fm, _ = parse_frontmatter(raw)
        assert fm["name"] == "Explore"


# ---------------------------------------------------------------------------
# Agent loader
# ---------------------------------------------------------------------------


class TestAgentLoader:
    def test_load_builtin_explore(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        defn = loader.get("explore")
        assert defn is not None
        assert defn.name == "Explore"
        assert defn.type == AgentType.EXPLORE
        assert defn.is_read_only is True
        assert defn.max_turns == 5
        assert "read_file" in defn.allowed_tools

    def test_load_builtin_plan(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        defn = loader.get("plan")
        assert defn is not None
        assert defn.name == "Plan"
        assert defn.type == AgentType.PLAN
        assert defn.max_turns == 8

    def test_load_builtin_verification(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        defn = loader.get("verification")
        assert defn is not None
        assert defn.name == "Verification"
        assert "VERDICT" in defn.system_prompt_template
        assert "write_file" in defn.disallowed_tools

    def test_load_builtin_general(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        defn = loader.get("general")
        assert defn is not None
        assert defn.name == "General"
        assert defn.max_turns == 15

    def test_project_overrides_builtin(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        agents_dir = tmp_path / ".pepsi-code" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "explore.md").write_text(
            "---\nname: Custom Explore\nmaxTurns: 3\n---\nCustom prompt",
            encoding="utf-8",
        )

        loader = AgentLoader(tmp_path)
        defn = loader.get("explore")
        assert defn is not None
        assert defn.name == "Custom Explore"
        assert defn.max_turns == 3
        assert defn.system_prompt_template == "Custom prompt"

    def test_hot_reload(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        agents_dir = tmp_path / ".pepsi-code" / "agents"
        agents_dir.mkdir(parents=True)
        agent_file = agents_dir / "custom.md"
        agent_file.write_text("---\nname: V1\nmaxTurns: 3\n---\nPrompt v1", encoding="utf-8")

        loader = AgentLoader(tmp_path)
        defn = loader.get("custom")
        assert defn is not None
        assert defn.name == "V1"

        # Wait so mtime changes
        time.sleep(0.1)
        agent_file.write_text("---\nname: V2\nmaxTurns: 5\n---\nPrompt v2", encoding="utf-8")

        defn2 = loader.get("custom")
        assert defn2 is not None
        assert defn2.name == "V2"
        assert defn2.max_turns == 5

    def test_list_names(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        names = loader.list_names()
        assert "explore" in names
        assert "plan" in names
        assert "general" in names
        assert "verification" in names

    def test_get_nonexistent_returns_none(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        assert loader.get("nonexistent") is None

    def test_get_empty_name_returns_none(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        assert loader.get("") is None
        assert loader.get("   ") is None

    def test_rejects_path_traversal_name(self, tmp_path: Path):
        from pepsicode.agents.loader import AgentLoader

        loader = AgentLoader(tmp_path)
        assert loader.get("../explore") is None


def test_builtin_agent_markdown_is_declared_as_package_data():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "agents/builtins/*.md" in project["tool"]["setuptools"]["package-data"]["pepsicode"]
