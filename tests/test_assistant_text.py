import pytest

from pepsicode.core.agent_loop import _accumulate_stream_tokens, run_agent_turn_stream
from pepsicode.llm.assistant_text import AssistantTextStream, parse_assistant_text
from pepsicode.server.protocol import serialize_session_history
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import _render_stream_transcript
from pepsicode.tui.transcript import render_transcript
from pepsicode.tui.types import TranscriptEntry
from pepsicode.types import StreamToken


@pytest.mark.parametrize(
    "opening,closing,kind",
    [
        ("<final>", "</final>", "final"),
        ("<progress>", "</progress>", "progress"),
        ("[FINAL]", "", "final"),
        ("[PROGRESS]", "", "progress"),
    ],
)
def test_stream_wrappers_are_hidden_at_every_possible_split(opening, closing, kind):
    raw = opening + "Done: hello world" + closing
    for split in range(len(raw) + 1):
        decoder = AssistantTextStream()
        visible = decoder.feed(raw[:split]) + decoder.feed(raw[split:]) + decoder.finish()
        assert visible == "Done: hello world"
    decoder = AssistantTextStream()
    assert "".join(decoder.feed(char) for char in raw) + decoder.finish() == "Done: hello world"
    assert parse_assistant_text(raw) == ("Done: hello world", kind)


@pytest.mark.parametrize(
    "raw",
    [
        "Hello world",
        "<finalize>literal</finalize>",
        "```xml\n<final>example</final>\n```",
        "Use <final> and </final> in this example",
        "[FINALITY] literal",
        "<fi",
        "",
    ],
)
def test_normal_text_and_code_remain_literal(raw):
    decoder = AssistantTextStream()
    assert "".join(decoder.feed(char) for char in raw) + decoder.finish() == raw


def test_only_outer_wrapper_is_removed_from_code_example():
    body = "Example:\n```xml\n<final>literal</final>\n```"
    raw = f"<final>{body}</final>"
    assert parse_assistant_text(raw) == (body, "final")
    decoder = AssistantTextStream()
    assert "".join(decoder.feed(char) for char in raw) + decoder.finish() == body


@pytest.mark.parametrize("suffix", ["", "</final>", "</final>\n "])
def test_stream_normalizes_final_in_deltas_callback_and_saved_history(suffix):
    raw = "<final>这个项目目前是空的。" + suffix

    class Model:
        def next_stream(self, messages):
            yield from (StreamToken(type="text", content=char) for char in raw)
            yield StreamToken(type="done")

    deltas, completed = [], []
    history = run_agent_turn_stream(
        model=Model(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=".",
        max_steps=1,
        on_token=lambda token: deltas.append(token.content) if token.type == "text" else None,
        on_assistant_message=completed.append,
    )
    assert "".join(deltas) == "这个项目目前是空的。"
    assert completed == ["这个项目目前是空的。"]
    assert history[-1]["content"] == completed[0]


def test_pending_plain_prefix_is_emitted_before_done():
    received = []
    _accumulate_stream_tokens(
        iter([StreamToken(type="text", content="<fi"), StreamToken(type="done")]), on_token=received.append
    )
    assert [(token.type, token.content) for token in received] == [("text", "<fi"), ("done", "")]


def test_explicit_progress_is_not_treated_as_final():
    class Model:
        count = 0

        def next_stream(self, messages):
            self.count += 1
            body = "<progress>Checking</progress>" if self.count == 1 else "<final>Done</final>"
            yield StreamToken(type="text", content=body)
            yield StreamToken(type="done")

    progress, answers = [], []
    run_agent_turn_stream(
        model=Model(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=".",
        max_steps=2,
        on_progress_message=progress.append,
        on_assistant_message=answers.append,
    )
    assert progress == ["Checking"] and answers == ["Done"]


def test_old_saved_assistant_wrappers_are_hidden_in_both_terminal_layouts():
    entries = [TranscriptEntry(1, "assistant", "<final>Done</final>")]
    stream_rows, _ = _render_stream_transcript(entries, 0, 20, 80)
    assert "Done" in "\n".join(stream_rows) and "<final>" not in "\n".join(stream_rows)
    assert "Done" in render_transcript(entries, 0) and "<final>" not in render_transcript(entries, 0)


def test_desktop_history_strips_only_assistant_channel_wrappers():
    history = serialize_session_history(
        [
            {"role": "user", "content": "<final>question</final>"},
            {"role": "assistant", "content": "<final>answer</final>"},
            {"role": "assistant_progress", "content": "<progress>checking</progress>"},
            {"role": "tool_result", "toolName": "read_file", "content": "<final>file content</final>"},
        ]
    )
    assert [item["content"] for item in history] == [
        "<final>question</final>",
        "answer",
        "checking",
        "<final>file content</final>",
    ]
