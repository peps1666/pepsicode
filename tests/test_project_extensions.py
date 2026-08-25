from __future__ import annotations

from pathlib import Path

from pepsicode.config import read_mcp_config_file
from pepsicode.context.skills import discover_skills, load_skill
from pepsicode.mcp import create_mcp_backed_tools
from pepsicode.tooling import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_openai_docs_remote_mcp_is_configured() -> None:
    project_servers = read_mcp_config_file(PROJECT_ROOT / ".mcp.json")
    remote = project_servers["openai-developer-docs"]

    assert remote["url"] == "https://developers.openai.com/mcp"
    assert remote["readOnlyTools"] == "*"


def test_project_health_skill_is_discoverable() -> None:
    skills = discover_skills(PROJECT_ROOT)

    project_health = next((skill for skill in skills if skill.name == "pepsicode-project-health"), None)
    assert project_health is not None
    assert project_health.source == "project"

    loaded = load_skill(PROJECT_ROOT, "pepsicode-project-health")
    assert loaded is not None
    assert "mcp__workspace-inspector__project_info" in loaded.content


def test_workspace_inspector_mcp_end_to_end() -> None:
    project_servers = read_mcp_config_file(PROJECT_ROOT / ".mcp.json")
    workspace_inspector = project_servers["workspace-inspector"]
    integration = create_mcp_backed_tools(
        cwd=str(PROJECT_ROOT),
        mcp_servers={"workspace-inspector": workspace_inspector},
    )

    try:
        assert integration["servers"][0]["status"] == "connected"
        assert integration["servers"][0]["toolCount"] == 2

        tools = {tool.name: tool for tool in integration["tools"]}
        project_info = tools["mcp__workspace-inspector__project_info"]
        assert project_info.is_read_only

        result = project_info.run({}, ToolContext(cwd=str(PROJECT_ROOT)))
        assert result.ok
        assert '"name": "pepsicode"' in result.output
        assert '"pepsicode-project-health"' in result.output
        assert '"workspace-inspector"' in result.output
    finally:
        integration["dispose"]()
