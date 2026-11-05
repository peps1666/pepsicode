import json

import pytest

from pepsicode.context.context_manager import ContextManager
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter, ContextOverflowError
from pepsicode.llm.model_settings import model_capabilities, update_context_window
from pepsicode.tooling import ToolRegistry


def runtime():
    return {
        "model": "test",
        "baseUrl": "https://example.test",
        "apiKey": "test",
        "models": [{"id": "test", "contextWindow": 10000, "maxOutputTokens": 8000}],
    }


@pytest.mark.parametrize("stream", [False, True])
def test_request_output_fits_remaining_context(stream):
    model = AnthropicModelAdapter(runtime(), ToolRegistry([]))
    builder = model._build_stream_request if stream else model._build_request
    small = json.loads(builder("test", "sys", [{"role": "user", "content": "hi"}]).data)
    large = json.loads(builder("test", "sys", [{"role": "user", "content": "x" * 24000}]).data)
    assert small["max_tokens"] == 8000
    assert 256 <= large["max_tokens"] < small["max_tokens"]
    with pytest.raises(ContextOverflowError):
        builder("test", "sys", [{"role": "user", "content": "x" * 50000}])


def test_manual_model_limits_override_api_and_reserve_compaction_space():
    r = runtime()
    r["_discoveredModels"] = [{"id": "test", "contextWindow": 1000000, "maxOutputTokens": 300000}]
    c = model_capabilities(r, "test")
    assert c["contextWindow"] == 10000 and c["maxOutputTokens"] == 8000
    assert c["limitSources"] == {"contextWindow": "config", "maxOutputTokens": "config"}
    manager = ContextManager(model="test")
    update_context_window(r, manager)
    assert manager.output_reserve == 8000
    assert manager.compaction_ceiling() == 7400
    manager.messages = [{"role": "user", "content": "x" * 31000}]
    assert manager.should_auto_compact()


def test_thinking_budget_is_not_silently_cut_below_minimum():
    r = runtime()
    r["models"][0].update(reasoningMode="budget", reasoningLevels=["medium"])
    r["reasoningEffort"] = "medium"
    adapter = AnthropicModelAdapter(r, ToolRegistry([]))
    with pytest.raises(ContextOverflowError):
        adapter._build_request("test", "sys", [{"role": "user", "content": "x" * 24000}])


def test_automatic_compaction_rechecks_after_tool_result(tmp_path):
    from pepsicode.core.agent_loop import run_agent_turn
    from pepsicode.execution.runtime import ExecutionScope, bind_scope
    from pepsicode.tooling import ToolCapability, ToolDefinition, ToolResult
    from pepsicode.types import AgentStep

    seen = []

    class Model:
        def next(self, messages):
            seen.append(list(messages))
            if len(seen) == 1:
                return AgentStep(type="tool_calls", calls=[{"id": "t", "toolName": "read", "input": {}}])
            return AgentStep(type="assistant", content="done")

    tool = ToolDefinition(
        "read",
        "read",
        {},
        lambda x: x,
        lambda *_: ToolResult(True, "z" * 10000),
        capabilities={ToolCapability.READ_ONLY},
        max_result_size_chars=10000,
    )
    messages = [{"role": "system", "content": "sys"}]
    for _ in range(12):
        messages.extend([{"role": "user", "content": "u" * 1000}, {"role": "assistant", "content": "a" * 1000}])
    manager = ContextManager(model="test", context_window=10000, output_reserve=8000)
    with bind_scope(ExecutionScope(str(tmp_path))):
        run_agent_turn(
            model=Model(), tools=ToolRegistry([tool]), messages=messages, cwd=str(tmp_path), context_manager=manager
        )
    assert len(seen) == 2
    assert not any(m.get("_compaction_marker") for m in seen[0])
    assert any(m.get("_compaction_marker") for m in seen[1])


@pytest.mark.parametrize("bad", [0, -1, True, "100"])
def test_manual_limits_reject_invalid_values(bad):
    r = runtime()
    r["models"][0]["contextWindow"] = bad
    with pytest.raises(ValueError, match="contextWindow"):
        model_capabilities(r, "test")
