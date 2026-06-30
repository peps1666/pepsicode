"""Tests for the CoreCoder-pattern optimizations:
parallel tool execution, HISTORY_SNIP, context overflow recovery, real token
usage, LLM/heuristic summarization, opt-in governance, and the Task tool.
"""

import threading
import time

from pepsicode.agent_loop import _execute_calls_in_order, _snip_tool_outputs, run_agent_turn
from pepsicode.anthropic_adapter import ContextOverflowError
from pepsicode.context_manager import ContextManager, _heuristic_summary
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.types import AgentStep, ModelAdapter


class ScriptedModel(ModelAdapter):
    def __init__(self, steps, last_usage=None):
        self._steps = steps
        self.calls = 0
        self.last_usage = last_usage

    def next(self, messages):
        step = self._steps[self.calls]
        self.calls += 1
        return step


def _tool(name, fn, concurrency_safe=False):
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object"},
        validator=lambda v: v,
        run=fn,
        concurrency_safe=concurrency_safe,
    )


# --------------------------------------------------------------------------
# Parallel execution
# --------------------------------------------------------------------------


def test_concurrency_safe_tools_run_in_parallel():
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(input_data, _ctx):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return ToolResult(ok=True, output="ok")

    registry = ToolRegistry([_tool("read_x", slow, concurrency_safe=True)])
    calls = [{"id": str(i), "toolName": "read_x", "input": {}} for i in range(3)]
    results = _execute_calls_in_order(calls, registry, ToolContext(cwd=".", permissions=None), None, None)

    assert len(results) == 3
    assert all(r.ok for r in results)
    assert active["max"] >= 2  # at least two ran concurrently


def test_unsafe_tools_run_serially_in_order():
    order = []

    def rec(input_data, _ctx):
        order.append(input_data["n"])
        return ToolResult(ok=True, output=str(input_data["n"]))

    registry = ToolRegistry([_tool("write_x", rec, concurrency_safe=False)])
    calls = [{"id": str(i), "toolName": "write_x", "input": {"n": i}} for i in range(4)]
    results = _execute_calls_in_order(calls, registry, ToolContext(cwd=".", permissions=None), None, None)

    assert order == [0, 1, 2, 3]
    assert [r.output for r in results] == ["0", "1", "2", "3"]


def test_results_preserve_call_order_when_parallel():
    def echo(input_data, _ctx):
        # Reverse-order sleep so later calls finish first.
        time.sleep(0.02 * (3 - input_data["n"]))
        return ToolResult(ok=True, output=str(input_data["n"]))

    registry = ToolRegistry([_tool("read_x", echo, concurrency_safe=True)])
    calls = [{"id": str(i), "toolName": "read_x", "input": {"n": i}} for i in range(3)]
    results = _execute_calls_in_order(calls, registry, ToolContext(cwd=".", permissions=None), None, None)
    assert [r.output for r in results] == ["0", "1", "2"]


# --------------------------------------------------------------------------
# HISTORY_SNIP
# --------------------------------------------------------------------------


def test_snip_trims_old_large_tool_outputs():
    big = "\n".join(f"line {i}" for i in range(500))
    messages = [{"role": "system", "content": "sys"}]
    for i in range(6):
        messages.append({"role": "assistant_tool_call", "toolUseId": str(i), "toolName": "grep", "input": {}})
        messages.append(
            {"role": "tool_result", "toolUseId": str(i), "toolName": "grep", "content": big, "isError": False}
        )

    out = _snip_tool_outputs([dict(m) for m in messages])
    tool_results = [m for m in out if m.get("role") == "tool_result"]
    # Oldest are snipped, the most recent few are untouched.
    assert any("[snipped" in m["content"] for m in tool_results)
    assert tool_results[-1]["content"] == big


def test_snip_preserves_recent_and_is_idempotent():
    big = "x" * 5000
    messages = [{"role": "tool_result", "toolName": "t", "content": big, "isError": False} for _ in range(5)]
    once = _snip_tool_outputs([dict(m) for m in messages])
    twice = _snip_tool_outputs([dict(m) for m in once])
    snipped_once = [m for m in once if "[snipped" in m["content"]]
    snipped_twice = [m for m in twice if "[snipped" in m["content"]]
    assert len(snipped_once) == len(snipped_twice)  # idempotent
    assert twice[-1]["content"] == big  # most recent preserved


# --------------------------------------------------------------------------
# Context overflow recovery
# --------------------------------------------------------------------------


def test_overflow_triggers_compaction_and_retry():
    # First call raises overflow; second returns a final answer.
    class OverflowOnceModel(ModelAdapter):
        def __init__(self):
            self.calls = 0
            self.last_usage = None

        def next(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise ContextOverflowError("request too large")
            return AgentStep(type="assistant", content="recovered")

    cm = ContextManager(model="default", context_window=100000)
    messages = run_agent_turn(
        model=OverflowOnceModel(),
        tools=ToolRegistry([]),
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        cwd=".",
        context_manager=cm,
    )
    assert messages[-1]["content"] == "recovered"


def test_overflow_without_context_manager_fails_gracefully():
    class AlwaysOverflow(ModelAdapter):
        last_usage = None

        def next(self, messages):
            raise ContextOverflowError("too big")

    messages = run_agent_turn(
        model=AlwaysOverflow(),
        tools=ToolRegistry([]),
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )
    assert "Context too large" in messages[-1]["content"]


# --------------------------------------------------------------------------
# Real token usage + summarization
# --------------------------------------------------------------------------


def test_real_usage_overrides_estimate():
    cm = ContextManager(model="default", context_window=200000)
    cm.add_message({"role": "user", "content": "tiny"})
    cm.update_usage(input_tokens=50000, output_tokens=100)
    stats = cm.get_stats()
    assert stats.total_tokens >= 50000


def test_heuristic_summary_extracts_paths_and_errors():
    dropped = [
        {"role": "assistant_tool_call", "toolName": "edit_file", "input": {"path": "src/auth.py"}},
        {
            "role": "tool_result",
            "toolName": "run_command",
            "content": "Traceback: ValueError in tests/test_auth.py",
            "isError": True,
        },
    ]
    summary = _heuristic_summary(dropped)
    assert "src/auth.py" in summary
    assert "edit_file" in summary
    assert "Traceback" in summary or "ValueError" in summary


def test_compaction_uses_summarizer_callback():
    captured = {}

    def fake_summarizer(text):
        captured["text"] = text
        return "SUMMARY: did stuff in foo.py"

    cm = ContextManager(model="default", context_window=1000)
    cm.summarizer = fake_summarizer
    cm.add_message({"role": "system", "content": "sys"})
    for i in range(40):
        cm.add_message({"role": "user", "content": f"msg {i} " * 30})
        cm.add_message({"role": "assistant", "content": f"resp {i} " * 30})

    compacted = cm.compact_messages()
    marker = next((m for m in compacted if m.get("role") == "system" and "compacted" in m.get("content", "")), None)
    assert marker is not None
    assert "SUMMARY: did stuff in foo.py" in marker["content"]
    assert "text" in captured


# --------------------------------------------------------------------------
# Governance opt-in + dynamic environment
# --------------------------------------------------------------------------


def test_governance_block_off_by_default(tmp_path):
    from pepsicode.prompt import build_system_prompt

    prompt = build_system_prompt(str(tmp_path), [], {"skills": [], "mcpServers": []})
    assert "Iron Laws" not in prompt
    assert "## Environment" in prompt  # dynamic section always present


def test_governance_block_on_when_enabled(tmp_path):
    from pepsicode.prompt import build_system_prompt

    prompt = build_system_prompt(str(tmp_path), [], {"skills": [], "mcpServers": [], "governance": True})
    assert "Iron Laws" in prompt


def test_environment_section_reports_python_and_cwd(tmp_path):
    from pepsicode.prompt import build_system_prompt

    prompt = build_system_prompt(str(tmp_path), [], {})
    assert "Python:" in prompt
    assert str(tmp_path) in prompt


# --------------------------------------------------------------------------
# Task tool (sub-agent delegation)
# --------------------------------------------------------------------------


def test_task_tool_runs_sub_agent_and_returns_summary():
    from pepsicode.tools.task import create_task_tool

    def factory(registry):
        return ScriptedModel([AgentStep(type="assistant", content="found the file")])

    tool = create_task_tool(".", None, model_factory=factory)
    parsed = tool.validator({"agent_type": "explore", "task": "find config"})
    result = tool.run(parsed, ToolContext(cwd=".", permissions=None))
    assert result.ok
    assert "found the file" in result.output
    assert "Explore" in result.output


def test_task_tool_rejects_unknown_agent_type():
    from pepsicode.tools.task import create_task_tool

    tool = create_task_tool(".", None, model_factory=lambda r: None)
    try:
        tool.validator({"agent_type": "bogus", "task": "x"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_task_tool_requires_model():
    from pepsicode.tools.task import create_task_tool

    tool = create_task_tool(".", None)  # no runtime, no factory
    parsed = tool.validator({"agent_type": "explore", "task": "x"})
    result = tool.run(parsed, ToolContext(cwd=".", permissions=None))
    assert not result.ok


def test_task_tool_excludes_itself_from_sub_registry():
    from pepsicode.sub_agents import AgentDefinition
    from pepsicode.tools.task import _build_sub_registry

    reg = _build_sub_registry(AgentDefinition.general_agent())
    assert "task" not in [t.name for t in reg.list()]


# --------------------------------------------------------------------------
# Regression tests for adversarial-review findings
# --------------------------------------------------------------------------


def test_compaction_drops_orphaned_tool_results():
    # A tool_result whose assistant_tool_call was dropped must not survive,
    # or Anthropic rejects the payload with a 400.
    cm = ContextManager(model="default", context_window=800)
    cm.add_message({"role": "system", "content": "sys"})
    # Two-call turn produces [TC_A, TC_B, TR_A, TR_B] ordering repeatedly.
    for i in range(20):
        cm.add_message({"role": "assistant_tool_call", "toolUseId": f"a{i}", "toolName": "read", "input": {}})
        cm.add_message({"role": "assistant_tool_call", "toolUseId": f"b{i}", "toolName": "read", "input": {}})
        cm.add_message(
            {"role": "tool_result", "toolUseId": f"a{i}", "toolName": "read", "content": "x" * 80, "isError": False}
        )
        cm.add_message(
            {"role": "tool_result", "toolUseId": f"b{i}", "toolName": "read", "content": "y" * 80, "isError": False}
        )

    compacted = cm.compact_messages(force=True)
    call_ids = {m.get("toolUseId") for m in compacted if m.get("role") == "assistant_tool_call"}
    for m in compacted:
        if m.get("role") == "tool_result":
            assert m.get("toolUseId") in call_ids, "orphaned tool_result survived compaction"


def test_noop_compaction_returns_unchanged():
    # Few messages, nothing to drop, no progress to strip -> no marker added.
    cm = ContextManager(model="default", context_window=200000)
    cm.add_message({"role": "system", "content": "sys"})
    cm.add_message({"role": "user", "content": "hi"})
    cm.add_message({"role": "assistant", "content": "hello"})
    before = list(cm.messages)
    after = cm.compact_messages(force=True)
    assert after == before
    # No bogus compaction_history entry recorded.
    assert cm.compaction_history == []


def test_compaction_resets_stale_actual_tokens():
    cm = ContextManager(model="default", context_window=1000)
    cm.add_message({"role": "system", "content": "sys"})
    for i in range(40):
        cm.add_message({"role": "user", "content": f"m{i} " * 30})
        cm.add_message({"role": "assistant", "content": f"r{i} " * 30})
    cm.update_usage(input_tokens=999999, output_tokens=10)
    cm.compact_messages(force=True)
    assert cm.actual_input_tokens == 0  # stale count cleared
    # should_compact must reflect the new (small) size, not the stale value.
    assert not cm.get_stats().should_compact


def test_529_not_retried_in_send():
    from pepsicode.anthropic_adapter import _should_retry_status

    assert _should_retry_status(529) is False
    assert _should_retry_status(429) is True
    assert _should_retry_status(503) is True


def test_compaction_markers_do_not_accumulate():
    cm = ContextManager(model="default", context_window=1000)
    cm.add_message({"role": "system", "content": "real-sys"})
    for i in range(40):
        cm.add_message({"role": "user", "content": f"m{i} " * 30})
        cm.add_message({"role": "assistant", "content": f"r{i} " * 30})
    cm.compact_messages(force=True)
    # Add more and compact again.
    for i in range(40):
        cm.add_message({"role": "user", "content": f"n{i} " * 30})
        cm.add_message({"role": "assistant", "content": f"s{i} " * 30})
    cm.update_usage(0, 0)
    cm.compact_messages(force=True)
    markers = [m for m in cm.messages if m.get("_compaction_marker")]
    real_sys = [m for m in cm.messages if m.get("role") == "system" and not m.get("_compaction_marker")]
    assert len(markers) <= 2  # bounded, not unbounded growth
    assert len(real_sys) == 1  # the genuine system prompt is preserved exactly once
