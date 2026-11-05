from __future__ import annotations

import json

import pytest

from pepsicode.cancellation import TurnCancelledError
from pepsicode.context.context_manager import ContextManager, estimate_tokens, validate_tool_pairs
from pepsicode.context.summarization import SUMMARY_INPUT_TOKENS, SummaryError, split_by_tokens, summarize_transcript
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.tooling import ToolRegistry


def test_chunking_covers_chinese_and_long_lines_without_gaps():
    text = "约束：必须保留公开接口。" * 2_000 + "x" * 19_000 + "TAIL_SENTINEL"
    chunks = split_by_tokens(text, 1_000, estimate_tokens)

    assert "".join(chunks) == text
    assert all(estimate_tokens(chunk) <= 1_000 for chunk in chunks)
    assert chunks[-1].endswith("TAIL_SENTINEL")


def test_all_transcript_parts_and_previous_checkpoint_reach_summarizer():
    received = []

    def summarize(text):
        received.append(text)
        return "checkpoint"

    result = summarize_transcript(
        "x" * 18_000 + "CRITICAL_LATE_CONSTRAINT",
        previous="KEEP_EXISTING_API",
        summarize=summarize,
        estimate=estimate_tokens,
        max_chars=6_000,
    )

    assert result.chunks > 1
    assert "KEEP_EXISTING_API" in received[0]
    assert "CRITICAL_LATE_CONSTRAINT" in received[-1]
    assert all(estimate_tokens(text) <= SUMMARY_INPUT_TOKENS for text in received)


def test_oversized_summaries_are_reduced_without_cropping_the_middle():
    received = []

    def summarize(text):
        received.append(text)
        if "reduction 0]" in text:
            return "start " + "x" * 3_000 + "MIDDLE_CONSTRAINT" + "y" * 3_000 + " end"
        assert "MIDDLE_CONSTRAINT" in text
        return "Keep MIDDLE_CONSTRAINT; work remains unfinished."

    result = summarize_transcript(
        "logs " * 3_000,
        previous="",
        summarize=summarize,
        estimate=estimate_tokens,
        max_chars=6_000,
    )

    assert "MIDDLE_CONSTRAINT" in result.text
    assert result.calls > result.chunks


def _large_manager(summarizer):
    manager = ContextManager(context_window=2_000, summarizer=summarizer)
    manager.messages = [{"role": "system", "content": "sys"}]
    manager.messages.extend({"role": "user", "content": f"message {index}: " + "x" * 500} for index in range(40))
    return manager


@pytest.mark.parametrize("result", ["", None, "x" * 50_000], ids=["empty", "none", "nonreducing"])
def test_incomplete_or_nonreducing_checkpoint_keeps_original_context(result):
    manager = _large_manager(lambda _text: result)
    original = list(manager.messages)

    assert manager.compact_messages(force=True) == original
    assert not manager.last_compaction_changed
    assert manager.last_compaction_error
    assert not manager.compact_boundaries


def test_summary_error_and_cancellation_do_not_discard_history():
    def failing(_text):
        raise RuntimeError("provider unavailable")

    manager = _large_manager(failing)
    original = list(manager.messages)
    manager.compact_messages(force=True)
    assert manager.messages == original
    assert "request failed" in manager.last_compaction_error

    def cancelled(_text):
        raise TurnCancelledError("stop")

    manager.summarizer = cancelled
    manager.circuit_breaker.last_fingerprint = ""
    with pytest.raises(TurnCancelledError):
        manager.compact_messages(force=True)
    assert manager.messages == original


def test_repeated_compaction_merges_old_constraints_and_preserves_tool_pairs():
    calls = []

    def summarize(text):
        calls.append(text)
        return "User constraints: KEEP_PUBLIC_API. Completed: first change. Next: fix regression."

    manager = _large_manager(summarize)
    manager.actual_input_tokens = 20_000
    manager.actual_output_tokens = 8_000
    manager.compact_messages(force=True)
    assert manager.actual_input_tokens == manager.actual_output_tokens == 0
    manager.messages.extend({"role": "user", "content": "new work " * 100} for _ in range(30))
    calls.clear()
    manager.compact_messages(force=True)

    assert manager.last_compaction_changed
    assert "KEEP_PUBLIC_API" in calls[0]
    assert validate_tool_pairs(manager.messages) == []
    boundary = manager.compact_boundaries[-1]
    assert boundary.summary_method == "model"
    assert boundary.summary_chunks >= 1
    assert boundary.summary_calls >= boundary.summary_chunks


def test_extractive_compaction_is_explicitly_marked_as_degraded():
    manager = _large_manager(None)
    manager.compact_messages(force=True)

    assert manager.last_compaction_changed
    assert manager.compact_boundaries[-1].summary_method == "extractive"
    assert "semantic summarizer unavailable" in manager.format_context_details()
    assert "semantic summarizer unavailable" in manager.get_context_summary()


def test_recovery_state_tracks_actual_edit_tools_and_batch_paths():
    manager = ContextManager()
    manager.observe_tool_result("patch_file", {"path": "src/a.py"}, "ok", True)
    manager.observe_tool_result("modify_file", {"path": "src/b.py"}, "ok", True)
    manager.observe_tool_result("multi_edit", {"changes": [{"file": "src/c.py"}, {"file": "src/d.py"}]}, "ok", True)
    assert [item.path for item in manager.recovery_state.recent_files] == [
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "src/d.py",
    ]


def test_failed_checkpoint_replacement_preserves_previous_state(monkeypatch, tmp_path):
    import pepsicode.context.context_manager as module

    monkeypatch.setattr(module, "PEPSI_CODE_DIR", tmp_path)
    manager = ContextManager(messages=[{"role": "user", "content": "original constraints"}])
    module.save_context_state(manager)
    original = (tmp_path / "context_state.json").read_bytes()
    manager.messages.append({"role": "user", "content": "new work"})

    def fail(*_args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        module.save_context_state(manager)
    assert (tmp_path / "context_state.json").read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_summary_budget_exhaustion_is_an_error_instead_of_partial_success():
    with pytest.raises(SummaryError, match="call budget"):
        summarize_transcript(
            "x" * 30_000,
            previous="",
            summarize=lambda text: "ok",
            estimate=estimate_tokens,
            max_chars=6_000,
            max_calls=1,
        )


@pytest.mark.parametrize("base", ["https://example.test", "https://example.test/v1/"])
def test_checkpoint_reuses_endpoint_and_bearer_auth_and_does_not_slice_input(monkeypatch, base):
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": base, "provider": "custom", "authType": "bearer", "apiKey": "test-secret"},
        ToolRegistry([]),
    )
    requests = []

    def send(request):
        requests.append(request)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "checkpoint"}]}, 200

    monkeypatch.setattr(adapter, "_send", send)
    text = "x" * 15_000 + "LATE_CONSTRAINT"
    assert adapter.summarize(text) == "checkpoint"
    request = requests[0]
    assert request.full_url == "https://example.test/v1/messages"
    assert request.get_header("Authorization") == "Bearer test-secret"
    assert json.loads(request.data)["messages"][0]["content"][0]["text"] == text


@pytest.mark.parametrize("reason", ["max_tokens", "pause_turn", None])
def test_truncated_checkpoint_response_is_rejected(monkeypatch, reason):
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([])
    )
    monkeypatch.setattr(
        adapter, "_send", lambda _request: ({"stop_reason": reason, "content": [{"text": "partial"}]}, 200)
    )
    with pytest.raises(RuntimeError, match="did not complete"):
        adapter.summarize("transcript")
