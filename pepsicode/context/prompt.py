from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from pepsicode.context.instructions import load_agent_instructions
from pepsicode.prompts import load_harness_instructions

PROMPT_FORMAT_VERSION = 3


def _git_branch(cwd_path: Path) -> str | None:
    """Return the current git branch, or None if not a git repo."""
    if not (cwd_path / ".git").exists():
        return None
    try:
        from pepsicode.subprocess_utils import run as _run

        result = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = result.stdout.strip()
    return branch or None


def _environment_info(cwd_path: Path) -> str:
    """Assemble a dynamic environment section (OS, Python, git, top entries).

    Mirrors Claude Code's dynamically assembled prompt so the agent adapts to
    the project it is actually running in.
    """
    lines = [
        "## Environment",
        f"- OS: {platform.system()} {platform.release()}",
        f"- Python: {sys.version.split()[0]}",
        f"- CWD: {cwd_path}",
    ]
    branch = _git_branch(cwd_path)
    if branch:
        lines.append(f"- Git branch: {branch}")
    try:
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in cwd_path.iterdir() if not p.name.startswith("."))
        if entries:
            preview = ", ".join(entries[:20])
            if len(entries) > 20:
                preview += f", … (+{len(entries) - 20} more)"
            lines.append(f"- Top-level entries: {preview}")
    except OSError:
        pass
    return "\n".join(lines)


def _engineering_governance_rules() -> str:
    r"""Return engineering governance rules as system prompt section.

    These rules are mandatory and apply to all code generation activities.
    Based on: D:\Desktop\engineering-governance
    """
    return """## Engineering Governance Rules (MANDATORY)

These rules apply to ALL code you write. No exceptions.

### Iron Laws
1. **Theory first**: Read theory before any engineering activity
2. **Requirements first**: No code without design, no design without requirements
3. **1:1 binding**: Requirements and knowledge always appear in pairs
4. **Design-driven**: Code implements design, not independent creation
5. **Audit loop**: Execute audit after each phase, fail → fix → re-audit
6. **Single sink**: business/src/ must have exactly ONE sink file
7. **One-way dependencies**: All dependency flow is unidirectional, zero cycles
8. **No skipping**: Each phase's exit signals must be met before next phase

### Package Structure (Six Areas)
Every package must have:
- `port/port_entry/` — Entry points (can import anything)
- `wrap/src/` — External library adapters (import: port_entry, wrap/config, wrap/src)
- `business/src/` — Business logic (import: wrap sinks, business/config, business/src)
- `test/src/` — Tests (import: business/src, test/config, test/src)
- `business/config/` — Business config (zero dependencies)
- `wrap/config/` — Adapter config (zero dependencies)
- `test/config/` — Test config (zero dependencies)

### Dependency Direction Rules
- `business/src/` → `wrap/src/` sinks → `port/port_entry/` → `vendor/`
- `business/src/` CANNOT import vendor/, external libs directly
- `wrap/src/` CANNOT import business/src/
- Config imports always come LAST in import statements
- Cross-package: port_exit → port_entry (same language to same language)

### Sink Rule
- `business/src/`: EXACTLY ONE sink (file not imported by other business/src/ files)
- `wrap/src/`: Can have multiple sinks (each must be used by business/src/)
- `test/src/`: Can have multiple sinks (all must be used by port_exit)
- Multiple sinks in business/src/ = MUST split package

### Documentation System
- Requirements → Knowledge → Design → Code (strict one-way flow)
- Each requirement scenario has exactly one matching knowledge file (1:1 path mirror)
- Each design file cites: satisfied requirements, depended knowledge
- Code file paths must be isomorphic to design file paths

### Import Sorting Example
```python
# Non-config imports first
from package.wrap/src/adapter import Adapter
from package.business/src/service import Service

# Config imports LAST
from package.business/config import settings
```

### Audit Checklist (Execute After Code Changes)
Audit 0: Knowledge ↔ Requirements 1:1
Audit 1: Design ← Requirements + Knowledge coverage
Audit 2: Code ← Design isomorphism + Dependency compliance
Audit 3: business/src/ single sink + Package DAG

### Boundary Packaging (Legacy Code)
- When introducing legacy code: only through port_entry → wrap/src/ ([LEGACY] tag)
- Each [LEGACY] file must have expected cleanup date
- Legacy code can reference governance area via port_exit directly

### Repository Rules
- ZERO compositional dependencies between repositories
- Cross-repository needs: copy to local vendor/
- Vendor only imported by port_entry/"""


def build_system_prompt(
    cwd: str,
    permission_summary: list[str] | None = None,
    extras: dict | None = None,
    *,
    dynamic_sections: list[str] | None = None,
) -> str:
    cwd_path = Path(cwd)
    permission_summary = permission_summary or []
    extras = extras or {}

    def add_dynamic(text: str) -> None:
        (parts if dynamic_sections is None else dynamic_sections).append(text)

    parts = [load_harness_instructions()]
    add_dynamic(f"Current cwd: {cwd}")

    # Dynamic environment section (always included, cheap, project-aware).
    add_dynamic(_environment_info(cwd_path))

    # Engineering governance rules — OPT-IN only.  Enabled when the project or
    # global config sets "governance": true, or PEPSI_CODE_GOVERNANCE=1.  These
    # rules are project-specific and otherwise inappropriate / token-costly.
    governance_enabled = bool(extras.get("governance")) or os.environ.get("PEPSI_CODE_GOVERNANCE") in {
        "1",
        "true",
        "True",
    }
    if governance_enabled:
        parts.append(_engineering_governance_rules())

    if permission_summary:
        add_dynamic("Permission context:\n" + "\n".join(permission_summary))

    if dynamic_sections is not None and not extras.get("planMode"):
        add_dynamic("PLAN MODE IS INACTIVE. Earlier plan-mode reminders no longer apply.")

    if extras.get("planMode"):
        plan_path = str(extras.get("planFilePath") or "(not initialized)")
        add_dynamic(
            f"""PLAN MODE IS ACTIVE. This overrides all general instructions to modify or execute.
You must only investigate and produce an implementation plan. Do not modify the workspace, run commands, change configuration, create commits, or call side-effecting tools.

The only file you may create or edit is the plan file below:
{plan_path}

Workflow:
1. Read and search the relevant code until you understand the request and reusable patterns.
2. If useful, call task only with agent_type='explore' or agent_type='plan'.
3. Call ask_user when a material product or implementation decision cannot be inferred safely.
4. Write the recommended plan to the plan file using write_file, then update only that same file with edit_file.
5. The plan must include context, exact files to change, implementation details, safety considerations, and verification steps.
6. When the plan is complete, call exit_plan_mode as the only tool call in the response.

Never treat ordinary edit approval as permission to bypass Plan mode."""
        )

    skills = extras.get("skills", [])
    if skills:
        add_dynamic(
            "Available skills:\n"
            + "\n".join(f"- {skill['name']}: {skill['description']}" for skill in skills)
            + "\n\n"
            + "When a task matches one of the descriptions above, call load_skill with that skill name "
            + "first, then follow the instructions it returns. Only these skills exist; do not invent others."
        )
    else:
        add_dynamic(
            "Available skills:\n"
            + "- none discovered\n"
            + "Skills are directories containing a SKILL.md, discovered under .pepsi-code/skills/ or ~/.pepsi-code/skills/."
        )

    mcp_servers = extras.get("mcpServers", [])
    if mcp_servers:
        add_dynamic(
            "Configured MCP servers:\n"
            + "\n".join(
                "- "
                + server["name"]
                + f": {server['status']}, tools={server['toolCount']}"
                + (f", resources={server['resourceCount']}" if server.get("resourceCount") is not None else "")
                + (f", prompts={server['promptCount']}" if server.get("promptCount") is not None else "")
                + (f", protocol={server['protocol']}" if server.get("protocol") else "")
                + (f" ({server['error']})" if server.get("error") else "")
                for server in mcp_servers
            )
        )
        if any(server.get("status") == "connected" for server in mcp_servers):
            add_dynamic(
                "Connected MCP tools are already exposed in the tool list with names prefixed like mcp__server__tool. Use list_mcp_resources/read_mcp_resource and list_mcp_prompts/get_mcp_prompt when a server exposes those capabilities."
            )
        # Only advertise the thinking tools we can name exactly.  The
        # registered name is mcp__<server>__<tool> and may have been
        # truncated, so it is read back from the server summary rather than
        # guessed here.
        thinking_tools = [
            tool_name
            for server in mcp_servers
            if server.get("status") == "connected"
            and any(marker in server.get("name", "").lower() for marker in ("sequential", "branch-thinking", "think"))
            for tool_name in (server.get("toolNames") or [])
            if "think" in tool_name.lower()
        ]
        if thinking_tools:
            add_dynamic(
                "A structured-thinking MCP server is connected. Use "
                + " or ".join(f"'{name}'" for name in thinking_tools)
                + " to work through problems that benefit from explicit step-by-step reasoning: "
                "breaking down complex implementations, multi-step debugging, architectural trade-offs, "
                "and migration or refactoring planning."
            )

    # Managed sessions refresh instructions as a recorded projection before each
    # model request. Standalone callers receive them in the returned prompt.
    if dynamic_sections is None:
        instructions = load_agent_instructions(cwd)
        if instructions:
            parts.append(instructions)

    memory_context = extras.get("memory_context")
    if memory_context:
        add_dynamic(f"Project Memory & Context:\n{memory_context}")

    return "\n\n".join(parts)


def build_session_prompt(
    cwd: str,
    permission_summary: list[str] | None = None,
    *,
    skills: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    memory_manager: Any | None = None,
    governance: bool = False,
    plan_mode: bool = False,
    plan_file_path: str | None = None,
    dynamic_sections: list[str] | None = None,
) -> str:
    """Build the system prompt used by every session entry point.

    Memory is read from the session's ``MemoryManager`` on each call, so a
    ``save_memory`` from the previous turn is visible on the next one. CLI,
    TUI, and the desktop server all go through this function.
    """
    extras: dict[str, Any] = {
        "skills": skills or [],
        "mcpServers": mcp_servers or [],
        "governance": governance,
        "planMode": plan_mode,
        "planFilePath": plan_file_path,
    }
    if memory_manager is not None:
        extras["memory_context"] = memory_manager.get_relevant_context()
    return build_system_prompt(cwd, permission_summary, extras, dynamic_sections=dynamic_sections)
