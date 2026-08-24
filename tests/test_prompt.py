from pathlib import Path

from pepsicode.context.prompt import build_system_prompt


def test_build_system_prompt_includes_skills_and_mcp(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        ["cwd: test"],
        {
            "skills": [{"name": "demo", "description": "demo skill"}],
            "mcpServers": [
                {
                    "name": "fake",
                    "status": "connected",
                    "toolCount": 1,
                    "resourceCount": 1,
                    "promptCount": 1,
                    "protocol": "newline-json",
                }
            ],
        },
    )

    assert "Available skills:" in prompt
    assert "demo skill" in prompt
    assert "Configured MCP servers:" in prompt
    assert "fake: connected, tools=1" in prompt


def test_build_system_prompt_mentions_sequential_thinking_server(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        [],
        {
            "mcpServers": [
                {
                    "name": "SequentialThinking",
                    "status": "connected",
                    "toolCount": 1,
                    "toolNames": ["mcp__SequentialThinking__sequentialthinking"],
                }
            ]
        },
    )

    assert "structured-thinking MCP server is connected" in prompt
    # The registered name must be quoted verbatim; a guessed name would not
    # resolve to a real tool.
    assert "'mcp__SequentialThinking__sequentialthinking'" in prompt


def test_build_system_prompt_omits_thinking_block_without_tool_names(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        [],
        {"mcpServers": [{"name": "SequentialThinking", "status": "connected", "toolCount": 1}]},
    )

    assert "structured-thinking MCP server is connected" not in prompt


def test_build_system_prompt_does_not_invent_skills(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        str(tmp_path),
        [],
        {"skills": [{"name": "demo", "description": "demo skill"}]},
    )

    for absent in ("brainstorming", "writing-plans", "verification-before-completion"):
        assert absent not in prompt
