from __future__ import annotations

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


class ModelAdapter(Protocol):
    def next(self, messages: list[ChatMessage]) -> AgentStep: ...

