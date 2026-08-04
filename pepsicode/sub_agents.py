"""Lightweight sub-agent system for pepsicode Python.

Inspired by Claude Code's AgentTool and coordinator/ system.
Provides specialized agents for different task types:
- Explore: Read-only, fast, for codebase exploration
- Plan: Read-only, thorough, for context gathering
- General-purpose: Full tools, for complex multi-step tasks

Each agent runs in isolation with its own context window,
preventing main conversation context from bloating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Agent types
# ---------------------------------------------------------------------------


class AgentType(str, Enum):
    """Sub-agent types (inspired by Claude Code's built-in agents)."""

    EXPLORE = "explore"  # Read-only, fast (like Haiku)
    PLAN = "plan"  # Read-only, thorough (like Sonnet in plan mode)
    GENERAL = "general"  # Full tools, complex tasks


@dataclass
class AgentDefinition:
    """Sub-agent definition.

    Inspired by Claude Code's agent definitions with custom system prompts,
    tool whitelists, and model selection.
    """

    type: AgentType
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str = "inherit"  # inherit from parent or specific model
    max_turns: int = 10
    is_read_only: bool = False

    @classmethod
    def explore_agent(cls) -> AgentDefinition:
        """Create Explore agent - fast, read-only exploration."""
        return cls(
            type=AgentType.EXPLORE,
            name="Explore",
            description="Fast, read-only agent for codebase exploration and search",
            system_prompt_template=(
                "You are an exploration agent. Your job is to quickly search and "
                "understand codebases. You should be fast and focused on finding "
                "relevant files and understanding structure. "
                "You can only use read-only tools."
            ),
            allowed_tools=["read_file", "list_files", "grep_files"],
            is_read_only=True,
            max_turns=5,
        )

    @classmethod
    def plan_agent(cls) -> AgentDefinition:
        """Create Plan agent - thorough context gathering."""
        return cls(
            type=AgentType.PLAN,
            name="Plan",
            description="Thorough agent for gathering context and understanding code",
            system_prompt_template=(
                "You are a planning agent. Your job is to thoroughly understand "
                "the codebase and task before acting. Read multiple files, trace "
                "code paths, and build a complete mental model. "
                "You can only use read-only tools."
            ),
            allowed_tools=["read_file", "list_files", "grep_files"],
            is_read_only=True,
            max_turns=8,
        )

    @classmethod
    def general_agent(cls) -> AgentDefinition:
        """Create General-purpose agent - full capabilities."""
        return cls(
            type=AgentType.GENERAL,
            name="General",
            description="Full-featured agent for complex multi-step tasks",
            system_prompt_template=(
                "You are a general-purpose coding agent. You can read, write, "
                "and modify code. Follow best practices and explain your changes. "
                "Break complex tasks into smaller steps."
            ),
            is_read_only=False,
            max_turns=15,
        )


__all__ = ["AgentType", "AgentDefinition"]
