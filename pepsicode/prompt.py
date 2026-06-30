from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


def _maybe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _git_branch(cwd_path: Path) -> str | None:
    """Return the current git branch, or None if not a git repo."""
    if not (cwd_path / ".git").exists():
        return None
    try:
        result = subprocess.run(
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


def _memory_strategy() -> str:
    r"""Return the proactive memory strategy that drives save_memory usage.

    Tells the agent *when* to persist a memory entry so long-term memory
    accumulates value instead of noise.  Mirrors the structured-block style of
    the other prompt sections (governance, skills) for consistency.
    """
    return """## 主动记忆策略

你会看到注入的项目记忆(save_memory 积累的跨会话记忆)。作为回报,你应在以下时机主动调用 save_memory 工具记录高价值信息,让未来的会话受益。

### 总原则
代码、git、CLAUDE.md 已记录的内容**绝不重复记录**。只记"重新发现或重新决策会花费力气"的东西。每次记录前自问:**5 个会话后这条记忆还有用吗?** 答案为否则跳过。

### 触发时机(按场景)

1. **理解项目后** — 记提炼后的架构地图,而非具体函数实现。
   - 记:核心入口、模块职责划分、构建/测试命令
   - 例:`[architecture] 入口 main.py;核心循环 agent_loop.py;记忆系统 memory.py;工具在 tools/;测试用 pytest 跑 tests/`
   - 不记:某个函数怎么实现的(grep/read_file 能查)

2. **做出非显然的技术决策后** — 记决策理由与被否方案,而非结论本身。
   - 触发:选型有理由,或否决了备选方案
   - 例:`[decision] 记忆后端选 PostgreSQL 而非纯文件,因要跨会话查询;保留文件双写做兜底`
   - 不记:显而易见的选择

3. **发现隐式项目约定时** — 从 2 处以上代码推断出一致模式才记。
   - 记:命名/结构/风格约定
   - 例:`[convention] 工具定义统一用 ToolDefinition + _validate + _run 三件套模式`
   - 不记:孤立的单点现象

4. **踩了非显然的坑并解决后** — 记症状+根因+解法。
   - 触发:第一次没试对的问题
   - 例:`[pitfall] 旧 _save_scope 重复定义,Python 用后者找已删方法崩溃——改后端要删干净旧方法`
   - 不记:一次性的临时调试命令

5. **用户明确表达持久偏好时** — 这是唯一记到 user 层(跨项目)的东西。
   - 例:`[preference] 回答用中文;引用代码带 file:line`

### 不记清单(防止记忆膨胀)
- 当前任务进度 → 用 todo_write,不进记忆
- 文件内容/函数实现 → 工具能读
- git 历史 → git log 能查
- 临时调试输出、一次性命令 → 下次无意义
- CLAUDE.md 已写的内容 → 重复

### scope 选择
- `user`:跨项目偏好(只有"用户偏好"这一类)
- `project`:团队共享、可进 git 的项目级约定/架构/决策
- `local`:本机特定、不进 git 的坑/本地环境"""


def build_system_prompt(
    cwd: str,
    permission_summary: list[str] | None = None,
    extras: dict | None = None,
) -> str:
    cwd_path = Path(cwd)
    permission_summary = permission_summary or []
    extras = extras or {}
    global_claude_md = _maybe_read(Path.home() / ".claude" / "CLAUDE.md")
    project_claude_md = _maybe_read(cwd_path / "CLAUDE.md")

    parts = [
        "You are pepsicode, a terminal coding assistant.",
        "Default behavior: inspect the repository, use tools, make code changes when appropriate, and explain results clearly.",
        "Prefer reading files, searching code, editing files, and running verification commands over giving purely theoretical advice.",
        f"Current cwd: {cwd}",
        "You can inspect or modify paths outside the current cwd when the user asks, but tool permissions may pause for approval first.",
        "When making code changes, keep them minimal, practical, and working-oriented.",
        "If the user clearly asked you to build, modify, optimize, or generate something, do the work instead of stopping at a plan.",
        "If you need user clarification, call the ask_user tool with one concise question and wait for the user reply. Do not ask clarifying questions as plain assistant text.",
        "Do not choose subjective preferences such as colors, visual style, copy tone, or naming unless the user explicitly told you to decide yourself.",
        "When using read_file, pay attention to the header fields. If it says TRUNCATED: yes, continue reading with a larger offset before concluding that the file itself is cut off.",
        "If the user names a skill or clearly asks for a workflow that matches a listed skill, call load_skill before following it.",
        "Structured response protocol:",
        "- When you are still working and will continue with more tool calls, start your text with <progress>.",
        "- Only when the task is actually complete and you are ready to hand control back, start your text with <final>.",
        "- Use ask_user when clarification is required; that tool ends the turn and waits for user input.",
        "- Do not stop after a progress update. After a <progress> message, continue the task in the next step.",
        "- Plain assistant text without <progress> is treated as a completed assistant message for this turn.",
    ]

    # Proactive memory strategy — drives save_memory usage so long-term memory
    # accumulates value.  Always included; it is compact and the agent needs it
    # in every session to know when to persist memories.
    parts.append(_memory_strategy())

    # Dynamic environment section (always included, cheap, project-aware).
    parts.append(_environment_info(cwd_path))

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
        parts.append("Permission context:\n" + "\n".join(permission_summary))

    skills = extras.get("skills", [])
    if skills:
        parts.append(
            "Available skills:\n"
            + "\n".join(f"- {skill['name']}: {skill['description']}" for skill in skills)
            + "\n\n"
            + "SKILL USAGE GUIDE:\n"
            + "- When user asks for creative brainstorming, use 'brainstorming' skill\n"
            + "- When writing implementation plans, use 'writing-plans' skill\n"
            + "- When debugging systematically, use 'systematic-debugging' skill\n"
            + "- When doing TDD, use 'test-driven-development' skill\n"
            + "- When reviewing code in Chinese, use 'chinese-code-review' skill\n"
            + "- When user asks about workflows, check 'using-superpowers' skill first\n"
            + "- For complex multi-step tasks, consider 'subagent-driven-development'\n"
            + "- Before completing, ALWAYS use 'verification-before-completion'"
        )
    else:
        parts.append(
            "Available skills:\n"
            + "- none discovered\n"
            + "Tip: Install skills via `npx superpowers-zh` in your project directory"
        )

    mcp_servers = extras.get("mcpServers", [])
    if mcp_servers:
        parts.append(
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
            parts.append(
                "Connected MCP tools are already exposed in the tool list with names prefixed like mcp__server__tool. Use list_mcp_resources/read_mcp_resource and list_mcp_prompts/get_mcp_prompt when a server exposes those capabilities."
            )
        sequential_servers = [
            server
            for server in mcp_servers
            if "sequential" in server.get("name", "").lower()
            or "branch-thinking" in server.get("name", "").lower()
            or "think" in server.get("name", "").lower()
        ]
        if any(server.get("status") == "connected" for server in sequential_servers):
            parts.append(
                "\nSEQUENTIAL THINKING MCP SERVER IS CONNECTED!\n"
                "When to use sequential_thinking tool:\n"
                "- Breaking down complex implementation problems\n"
                "- Multi-step debugging or investigation\n"
                "- Architectural decisions requiring structured analysis\n"
                "- Migration or refactoring planning\n"
                "- Any situation requiring step-by-step reasoning\n\n"
                "Usage: Call 'sequential_thinking' with structured thoughts before complex tool sequences"
            )

    if global_claude_md:
        parts.append(f"Global instructions from ~/.claude/CLAUDE.md:\n{global_claude_md}")
    if project_claude_md:
        parts.append(f"Project instructions from {cwd_path / 'CLAUDE.md'}:\n{project_claude_md}")

    memory_context = extras.get("memory_context")
    if memory_context:
        parts.append(f"Project Memory & Context:\n{memory_context}")

    return "\n\n".join(parts)
