from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict


class ProviderThinkingBlock(TypedDict):
    type: Literal["thinking", "redacted_thinking"]


class ChatMessage(TypedDict, total=False):
    role: Literal[
        "system",
        "user",
        "assistant",
        "assistant_progress",
        "assistant_thinking",
        "assistant_tool_call",
        "tool_result",
    ]
    content: str
    blocks: list[ProviderThinkingBlock]
    toolUseId: str
    toolName: str
    input: Any
    isError: bool


class ToolCall(TypedDict):
    id: str
    toolName: str
    input: Any


@dataclass(slots=True)
class StepDiagnostics:
    stopReason: str | None = None
    blockTypes: list[str] = field(default_factory=list)
    ignoredBlockTypes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentStep:
    type: Literal["assistant", "tool_calls"]
    content: str = ""
    kind: Literal["final", "progress"] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    contentKind: Literal["progress"] | None = None
    thinkingBlocks: list[ProviderThinkingBlock] = field(default_factory=list)
    diagnostics: StepDiagnostics | None = None


@dataclass(slots=True)
class StreamToken:
    """A single token (or event) emitted by a streaming model adapter.

    Attributes:
        type:            "text"     - incremental text delta
                        "tool_use"  - a tool-call block (start or incremental JSON)
                        "thinking"  - a thinking-block marker
                        "done"      - end-of-stream marker
        content:         raw text delta (populated for type=="text")
        tool_name:       tool name (set when a tool_use block starts)
        tool_id:         tool use id (set when a tool_use block starts)
        tool_input_partial:  incremental JSON for a tool_use input (string delta)
    """
    type: Literal["text", "tool_use", "thinking", "done"]
    content: str = ""
    tool_name: str | None = None
    tool_id: str | None = None
    tool_input_partial: str = ""


class ModelAdapter(Protocol):
    def next(self, messages: list[ChatMessage]) -> AgentStep: ...
    def next_stream(
        self, messages: list[ChatMessage]
    ) -> Generator[StreamToken, None, None]: ...

