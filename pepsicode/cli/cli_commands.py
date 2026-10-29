from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pepsicode.cli.model_commands import handle_model_command
from pepsicode.cli.prompt_commands import PromptCommandCatalog
from pepsicode.config import (
    CLAUDE_SETTINGS_PATH,
    PEPSI_CODE_MCP_PATH,
    PEPSI_CODE_PERMISSIONS_PATH,
    PEPSI_CODE_SETTINGS_PATH,
    load_runtime_config,
)
from pepsicode.hooks.loader import LOCAL_HOOKS_FILE, PROJECT_HOOKS_FILE, USER_HOOKS_PATH


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    usage: str
    description: str
    category: str = "General"
    source: str = "builtin"
    is_prompt: bool = False

    @property
    def completion_text(self) -> str:
        return self.name if self.is_prompt else self.usage


SLASH_COMMANDS = [
    # Session
    SlashCommand("/help", "/help", "Show available slash commands.", "Session"),
    SlashCommand("/exit", "/exit", "Exit pepsi-code.", "Session"),
    SlashCommand("/clear", "/clear", "Clear the current transcript view.", "Session"),
    SlashCommand("/history", "/history", "Show recent prompt history.", "Session"),
    SlashCommand("/retry", "/retry", "在新分支重新生成上一条用户消息的回答，保留原路线。", "Session"),
    SlashCommand("/tree", "/tree [entry-id]", "查看或切换当前会话的对话分支，文件保持当前状态。", "Session"),
    SlashCommand("/fork", "/fork [user-entry-id]", "从某条用户消息创建独立会话。", "Session"),
    SlashCommand("/clone", "/clone", "将当前对话路线复制为新会话。", "Session"),
    SlashCommand("/plan", "/plan [task]", "Enter read-only Plan mode.", "Session"),
    SlashCommand("/resume", "/resume [id]", "Resume a saved session.", "Session"),
    SlashCommand("/transcript-save", "/transcript-save <path>", "Save transcript to a text file.", "Session"),
    # Tools
    SlashCommand("/tools", "/tools", "List tools available to the coding agent.", "Tools"),
    SlashCommand("/hooks", "/hooks [list|errors|reload|trust|enable|disable]", "Inspect and manage Hooks v2.", "Tools"),
    SlashCommand("/skills", "/skills", "List discovered SKILL.md workflows.", "Tools"),
    SlashCommand("/commands", "/commands [reload]", "List Markdown prompt commands and loading errors.", "Tools"),
    SlashCommand("/mcp", "/mcp", "Show MCP servers and connection state.", "Tools"),
    SlashCommand("/cmd", "/cmd [cwd::]<command> [args...]", "Run an allowed development command.", "Tools"),
    # Status
    SlashCommand("/status", "/status", "Show application state summary.", "Status"),
    SlashCommand("/model", "/model", "Show the current model.", "Status"),
    SlashCommand(
        "/model", "/model <model-name>", "Switch the current session model; append --save for defaults.", "Status"
    ),
    SlashCommand("/model", "/model list", "List configured model candidates and reasoning levels.", "Status"),
    SlashCommand("/model", "/model refresh", "从当前 API 重新读取模型名称与能力。", "Status"),
    SlashCommand("/provider", "/provider", "选择模型服务商及其模型，支持添加自定义厂商。", "Status"),
    SlashCommand("/provider", "/provider add", "逐步配置自定义厂商：地址、Key、模型和保存。", "Status"),
    SlashCommand("/effort", "/effort", "选择思考强度，查看所有档位与当前值。", "Status"),
    SlashCommand("/cost", "/cost [--detailed]", "Show API cost and usage report.", "Status"),
    SlashCommand("/context", "/context", "Show context window usage.", "Status"),
    SlashCommand("/tasks", "/tasks", "Show current task list.", "Status"),
    SlashCommand("/memory", "/memory", "Show memory system status.", "Status"),
    SlashCommand("/config", "/config", "Show configuration diagnostics.", "Status"),
    SlashCommand("/config-paths", "/config-paths", "Show settings file paths.", "Status"),
    SlashCommand("/permissions", "/permissions", "选择权限模式，√ 标记当前模式。", "Status"),
    # Files
    SlashCommand("/ls", "/ls [path]", "List files in a directory.", "Files"),
    SlashCommand("/grep", "/grep <pattern>::[path]", "Search text in files.", "Files"),
    SlashCommand("/read", "/read <path>", "Read a file directly.", "Files"),
    SlashCommand("/write", "/write <path>::<content>", "Write a file directly.", "Files"),
    SlashCommand("/edit", "/edit <path>::<search>::<replace>", "Edit a file by exact replacement.", "Files"),
    SlashCommand(
        "/patch", "/patch <path>::<search1>::<replace1>...", "Apply multiple replacements to one file.", "Files"
    ),
    SlashCommand("/modify", "/modify <path>::<content>", "Replace a file with reviewable diff.", "Files"),
    # Worktree
    SlashCommand("/worktree", "/worktree", "List active git worktrees.", "Worktree"),
    SlashCommand("/worktree", "/worktree status", "Show detailed worktree session status.", "Worktree"),
    SlashCommand("/worktree", "/worktree create <name> [base]", "Create a new isolated worktree.", "Worktree"),
    SlashCommand("/worktree", "/worktree enter <name>", "Enter a worktree (switch context into it).", "Worktree"),
    SlashCommand(
        "/worktree", "/worktree exit <name> [keep|remove]", "Exit a worktree, optionally removing it.", "Worktree"
    ),
    SlashCommand("/worktree", "/worktree cleanup [max_age_hours]", "Remove stale ephemeral worktrees.", "Worktree"),
]


@lru_cache(maxsize=32)
def _prompt_catalog(cwd: str, home: str, reserved: tuple[str, ...]) -> PromptCommandCatalog:
    return PromptCommandCatalog(cwd, home, set(reserved))


def prompt_catalog(cwd: str | None = None, *, refresh: bool = False) -> PromptCommandCatalog:
    reserved = tuple(sorted({command.name for command in SLASH_COMMANDS} | {"/queue"}))
    return _prompt_catalog(str(Path(cwd or Path.cwd()).resolve()), str(Path.home().resolve()), reserved).refresh(
        force=refresh
    )


def get_slash_commands(cwd: str | None = None) -> list[SlashCommand]:
    custom = prompt_catalog(cwd).commands.values()
    return [
        *SLASH_COMMANDS,
        *(SlashCommand(cmd.name, cmd.metadata.usage, cmd.description, "Custom", cmd.source, True) for cmd in custom),
    ]


def expand_prompt_command(user_input: str, cwd: str):
    if not user_input.startswith("/"):
        return None
    return prompt_catalog(cwd).expand(user_input)


def format_prompt_commands(cwd: str | None = None, *, refresh: bool = False) -> str:
    catalog = prompt_catalog(cwd, refresh=refresh)
    lines = ["Markdown prompt commands (.pepsi-code/commands/*.md):"]
    lines.extend(
        f"{cmd.metadata.usage} — {cmd.description} [{cmd.source}]\n  {cmd.path}" for cmd in catalog.commands.values()
    )
    if not catalog.commands:
        lines.append("No custom commands discovered.")
    if catalog.diagnostics:
        lines.extend(["Loading errors:", *catalog.diagnostics])
    return "\n".join(lines)


def format_slash_commands(cwd: str | None = None) -> str:
    lines = [
        "═══════════════════════════════════════════════════════════════════════════",
        "║ \U0001f4ce Available Commands                                  ║",
        "╟───────────────────────────────────────────────────────────────────────────╢",
    ]

    command_groups: dict[str, list[SlashCommand]] = {}
    for command in SLASH_COMMANDS:
        command_groups.setdefault(command.category, []).append(command)
    for group_name, commands in command_groups.items():
        lines.append(f"║ {group_name:<54}║")
        for command in commands:
            cmd_display = f"    {command.usage}"
            lines.append(f"║ {cmd_display:<20} {command.description:<33} ║")
        lines.append("╟───────────────────────────────────────────────────────────────────────────╢")

    lines.extend(
        [
            "║ \U0001f4a1 Tips:                                              ║",
            "║ - Use Tab to autocomplete commands                    ║",
            "║ - Prefix with / to access any command                 ║",
            "║ - Type naturally - I'll understand Chinese & English  ║",
            "╚═══════════════════════════════════════════════════════════════════════════╝",
        ]
    )

    return "\n".join(lines) + "\n\n" + format_prompt_commands(cwd)


def find_matching_slash_commands(user_input: str, cwd: str | None = None) -> list[str]:
    return [
        command.usage
        for command in get_slash_commands(cwd)
        if command.usage.startswith(user_input)
        and not (command.is_prompt and any(char.isspace() for char in user_input))
    ]


def complete_slash_command(line: str, cwd: str | None = None) -> tuple[list[str], str]:
    commands = get_slash_commands(cwd)
    hits = [command.completion_text for command in commands if command.usage.startswith(line)]
    return (
        hits if hits else [command.completion_text for command in commands],
        line,
    )


def try_handle_local_command(
    user_input: str,
    tools=None,
    permissions=None,
    *,
    runtime=None,
    model_adapter=None,
    messages=None,
    context_manager=None,
    cwd=None,
) -> str | None:
    if user_input.split() and user_input.split()[0] in {"/model", "/effort", "/provider"}:
        if permissions is not None:
            try:
                permissions.ensure_local_command_allowed(user_input)
            except RuntimeError as error:
                return str(error)
        return handle_model_command(
            user_input, runtime=runtime, model_adapter=model_adapter, messages=messages, context_manager=context_manager
        )
    if user_input in {"/", "/help"}:
        return format_slash_commands(cwd)

    if user_input.split() and user_input.split()[0] == "/commands":
        if user_input.split() not in (["/commands"], ["/commands", "reload"]):
            return "Usage: /commands [reload]"
        return format_prompt_commands(cwd, refresh=True)

    if user_input == "/config-paths":
        return "\n".join(
            [
                f"pepsi-code settings: {PEPSI_CODE_SETTINGS_PATH}",
                f"pepsi-code permissions: {PEPSI_CODE_PERMISSIONS_PATH}",
                f"pepsi-code mcp: {PEPSI_CODE_MCP_PATH}",
                f"user hooks: {USER_HOOKS_PATH}",
                f"project hooks: {PROJECT_HOOKS_FILE}",
                f"local hooks: {LOCAL_HOOKS_FILE}",
                f"compat fallback: {CLAUDE_SETTINGS_PATH}",
            ]
        )

    if user_input.split() and user_input.split()[0] == "/permissions":
        if permissions is None:
            return "Permissions unavailable."
        parts = user_input.split()
        if len(parts) >= 2 and parts[1] == "default":
            if len(parts) != 3:
                return "Usage: /permissions default [read_only|auto|trust_commands|full_access]"
            from pepsicode.workspace_permissions import set_workspace_default

            try:
                set_workspace_default(permissions.workspace_root, parts[2])
            except ValueError as error:
                return str(error)
            except OSError as error:
                return f"保存工作区默认权限失败：{error}"
            return f"工作区新会话默认权限已设为 {parts[2]}；当前会话不变。"
        if len(parts) > 1:
            if len(parts) != 2:
                return "Usage: /permissions [read_only|auto|trust_commands|full_access]"
            try:
                permissions.set_access_mode(parts[1])
            except ValueError:
                return "Usage: /permissions [read_only|auto|trust_commands|full_access]"
        return (
            f"权限模式: {permissions.access_mode.value}\n"
            "read_only · 只读：查看文件和搜索，禁止编辑及命令执行\n"
            "auto · 自动：项目内编辑免确认；本地命令、项目外访问和外部工具需确认\n"
            "trust_commands · 命令免确认（高风险）：所有程序以当前用户权限运行，可访问项目外文件和网络；内置工具访问项目外及外部工具仍需确认\n"
            "full_access · 完全访问：编辑、命令与外部工具均免确认\n"
            f"permission store: {PEPSI_CODE_PERMISSIONS_PATH}"
        )

    if user_input == "/worktree" or user_input.startswith("/worktree "):
        return _handle_worktree_command(user_input)

    if user_input == "/skills":
        skills = tools.get_skills() if tools else []
        if not skills:
            return "No skills discovered. Add skills under ~/.pepsi-code/skills/<name>/SKILL.md, .pepsi-code/skills/<name>/SKILL.md, .claude/skills/<name>/SKILL.md, or ~/.claude/skills/<name>/SKILL.md."
        return "\n".join(f"{skill['name']}  {skill['description']}  [{skill['source']}]" for skill in skills)

    if user_input == "/config":
        from pepsicode.config import format_config_diagnostic

        return format_config_diagnostic()

    if user_input == "/memory":
        # Memory system display
        try:
            import os

            from pepsicode.context.memory import MemoryScope, create_memory_manager

            memory_mgr = create_memory_manager(os.getcwd())

            # get_stats() returns per-scope counts/size/categories; flatten it
            # into the per-scope entry counts this command has always shown.
            stats = memory_mgr.get_stats()

            lines = ["Memory System Status", "=" * 40, ""]

            total_entries = 0
            # Display order: user -> project -> local
            scope_labels = {
                MemoryScope.USER: "User memory",
                MemoryScope.PROJECT: "Project memory",
                MemoryScope.LOCAL: "Local memory",
            }
            for scope in [MemoryScope.USER, MemoryScope.PROJECT, MemoryScope.LOCAL]:
                scope_stats = stats.get(scope.value, {})
                count = scope_stats.get("entries", 0)
                total_entries += count
                lines.append(f"{scope_labels[scope]}: {count} entries")
            lines.append(f"Total: {total_entries} entries")
            lines.append("")

            # Show recent entries (most recently created first)
            lines.append("Recent Entries:")
            all_entries: list = []
            for scope in MemoryScope:
                all_entries.extend(memory_mgr.memories[scope].entries)
            all_entries.sort(key=lambda e: e.created_at, reverse=True)
            recent = all_entries[:10]
            if recent:
                for entry in recent:
                    tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
                    lines.append(f"  - {entry.id} {entry.content[:80]}{tags_str}")
            else:
                lines.append("  No entries yet")

            return "\n".join(lines)
        except Exception as e:
            return f"Error loading memory: {e}"

    if user_input == "/context":
        # Context usage display
        try:
            from pepsicode.context.context_manager import load_context_state

            ctx_mgr = load_context_state()
            if ctx_mgr:
                return ctx_mgr.format_context_details()
            else:
                return "No context state available. Context tracking starts after first turn."
        except Exception as e:
            return f"Error loading context: {e}"

    if user_input == "/mcp":
        servers = tools.get_mcp_servers() if tools else []
        if not servers:
            return "No MCP servers configured. Add mcpServers to ~/.pepsi-code/settings.json, ~/.pepsi-code/mcp.json, or project .mcp.json."
        lines = []
        for server in servers:
            suffix = f"  error={server['error']}" if server.get("error") else ""
            protocol = f"  protocol={server['protocol']}" if server.get("protocol") else ""
            resources = f"  resources={server['resourceCount']}" if server.get("resourceCount") is not None else ""
            prompts = f"  prompts={server['promptCount']}" if server.get("promptCount") is not None else ""
            lines.append(
                f"{server['name']}  status={server['status']}  tools={server['toolCount']}{resources}{prompts}{protocol}{suffix}"
            )
        return "\n".join(lines)

    if user_input == "/status":
        try:
            runtime = runtime if runtime is not None else load_runtime_config()
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"
        auth = "ANTHROPIC_AUTH_TOKEN" if runtime.get("authToken") else "ANTHROPIC_API_KEY"
        lines = [
            f"model: {runtime['model']}",
            f"reasoning effort: {runtime.get('reasoningEffort', 'default')}",
            f"baseUrl: {runtime['baseUrl']}",
            f"auth: {auth}",
            f"mcp servers: {len(runtime.get('mcpServers', {}))}",
            runtime.get("sourceSummary", "active session settings"),
        ]
        if permissions is not None:
            lines.append(f"permission mode: {permissions.mode.value}")
            if permissions.plan_file_path:
                lines.append(f"plan file: {permissions.plan_file_path}")
        return "\n".join(lines)

    return None


def _handle_worktree_command(user_input: str) -> str:
    """Handle /worktree subcommands: list, status, create, enter, exit, cleanup.

    All operations run against the current working directory's git repo.
    """
    import os

    from pepsicode.worktree import WorktreeError, WorktreeManager

    parts = user_input.split()
    # parts[0] == "/worktree"
    sub = parts[1] if len(parts) > 1 else "list"

    try:
        manager = WorktreeManager(repo_root=os.getcwd())
        manager.restore_session()
    except Exception as e:  # noqa: BLE001
        return f"Failed to initialize worktree manager: {e}"

    try:
        if sub in ("list", ""):
            wts = manager.list_worktrees()
            if not wts:
                return "No active worktrees.\nUsage: /worktree create <name> [base_branch]"
            lines = ["Active worktrees:", ""]
            for wt in wts:
                marker = (
                    " *" if (manager.current_session and manager.current_session.worktree_name == wt.name) else "  "
                )
                lines.append(
                    f"{marker}{wt.name}\n"
                    f"      path:   {wt.path}\n"
                    f"      branch: {wt.branch}\n"
                    f"      head:   {wt.head_commit[:12] if wt.head_commit else '(unknown)'}"
                )
            return "\n".join(lines)

        if sub == "status":
            import json

            return json.dumps(manager.status(), indent=2, ensure_ascii=False)

        if sub == "create":
            if len(parts) < 3:
                return "Usage: /worktree create <name> [base_branch]"
            name = parts[2]
            base = parts[3] if len(parts) > 3 else "HEAD"
            wt = manager.create(name, base_branch=base)
            return (
                f"Created worktree '{wt.name}':\n"
                f"  path:   {wt.path}\n"
                f"  branch: {wt.branch}\n"
                f"  based:  {wt.based_on}\n"
                f"  head:   {wt.head_commit[:12] if wt.head_commit else '(unknown)'}"
            )

        if sub == "enter":
            if len(parts) < 3:
                return "Usage: /worktree enter <name>"
            name = parts[2]
            session = manager.enter(name)
            return (
                f"Entered worktree '{name}'.\n"
                f"  worktree path:  {session.worktree_path}\n"
                f"  original cwd:   {session.original_cwd}\n"
                f"  original branch: {session.original_branch}\n"
                f"\nNote: pepsicode's cwd is not changed automatically. "
                f"Use the worktree path above for subsequent file operations."
            )

        if sub == "exit":
            if len(parts) < 3:
                return "Usage: /worktree exit <name> [keep|remove]"
            name = parts[2]
            action_on_exit = parts[3] if len(parts) > 3 else "keep"
            if action_on_exit not in ("keep", "remove"):
                return "action must be 'keep' or 'remove'"
            manager.exit(name, action=action_on_exit)
            suffix = " (worktree removed)" if action_on_exit == "remove" else " (worktree kept on disk)"
            return f"Exited worktree '{name}'{suffix}."

        if sub == "cleanup":
            from pepsicode.worktree.cleanup import cleanup_stale_worktrees

            max_age = int(parts[2]) if len(parts) > 2 else 24
            removed = cleanup_stale_worktrees(manager, cutoff_hours=max_age)
            return f"Cleaned up {removed} stale worktree(s) older than {max_age}h."

        return f"Unknown subcommand: {sub}\nAvailable: list, status, create, enter, exit, cleanup"
    except WorktreeError as e:
        return f"Worktree error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"Unexpected error: {e}"
