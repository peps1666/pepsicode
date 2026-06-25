from __future__ import annotations

from dataclasses import dataclass

from pepsicode.poly_commands import create_builtin_commands, CommandRegistry
from pepsicode.config import (
    CLAUDE_SETTINGS_PATH,
    PEPSI_CODE_MCP_PATH,
    PEPSI_CODE_PERMISSIONS_PATH,
    PEPSI_CODE_SETTINGS_PATH,
    load_runtime_config,
    save_mini_code_settings,
)


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    usage: str
    description: str
    category: str = "General"


SLASH_COMMANDS = [
    # Session
    SlashCommand("/help", "/help", "Show available slash commands.", "Session"),
    SlashCommand("/exit", "/exit", "Exit pepsi-code.", "Session"),
    SlashCommand("/clear", "/clear", "Clear the current transcript view.", "Session"),
    SlashCommand("/history", "/history", "Show recent prompt history.", "Session"),
    SlashCommand("/retry", "/retry", "Retry the last natural-language prompt.", "Session"),
    SlashCommand("/resume", "/resume [id]", "Resume a saved session.", "Session"),
    SlashCommand("/transcript-save", "/transcript-save <path>", "Save transcript to a text file.", "Session"),
    # Tools
    SlashCommand("/tools", "/tools", "List tools available to the coding agent.", "Tools"),
    SlashCommand("/skills", "/skills", "List discovered SKILL.md workflows.", "Tools"),
    SlashCommand("/mcp", "/mcp", "Show MCP servers and connection state.", "Tools"),
    SlashCommand("/cmd", "/cmd [cwd::]<command> [args...]", "Run an allowed development command.", "Tools"),
    # Status
    SlashCommand("/status", "/status", "Show application state summary.", "Status"),
    SlashCommand("/model", "/model", "Show the current model.", "Status"),
    SlashCommand("/model", "/model <model-name>", "Persist a model override into settings.", "Status"),
    SlashCommand("/cost", "/cost [--detailed]", "Show API cost and usage report.", "Status"),
    SlashCommand("/context", "/context", "Show context window usage.", "Status"),
    SlashCommand("/tasks", "/tasks", "Show current task list.", "Status"),
    SlashCommand("/memory", "/memory", "Show memory system status.", "Status"),
    SlashCommand("/config", "/config", "Show configuration diagnostics.", "Status"),
    SlashCommand("/config-paths", "/config-paths", "Show settings file paths.", "Status"),
    SlashCommand("/permissions", "/permissions", "Show permission storage path.", "Status"),
    # Files
    SlashCommand("/ls", "/ls [path]", "List files in a directory.", "Files"),
    SlashCommand("/grep", "/grep <pattern>::[path]", "Search text in files.", "Files"),
    SlashCommand("/read", "/read <path>", "Read a file directly.", "Files"),
    SlashCommand("/write", "/write <path>::<content>", "Write a file directly.", "Files"),
    SlashCommand("/edit", "/edit <path>::<search>::<replace>", "Edit a file by exact replacement.", "Files"),
    SlashCommand("/patch", "/patch <path>::<search1>::<replace1>...", "Apply multiple replacements to one file.", "Files"),
    SlashCommand("/modify", "/modify <path>::<content>", "Replace a file with reviewable diff.", "Files"),
]


def format_slash_commands() -> str:
    lines = [
        "═══════════════════════════════════════════════════════════════════════════",
        "║ \U0001f4ce Available Commands                                  ║",
        "╟───────────────────────────────────────────────────────────────────────────╢",
    ]

    command_groups = {
        "\U0001f4dd Core Commands": [
            ("/help", "Show this help message"),
            ("/exit", "Exit pepsi-code"),
            ("/clear", "Clear the current transcript view"),
            ("/history", "Show recent prompt history"),
        ],
        "\U0001f6e0️ Tool Commands": [
            ("/tools", "List all available tools"),
            ("/skills", "List discovered SKILL.md workflows"),
            ("/mcp", "Show MCP servers and connection state"),
            ("/cmd", "Run development commands directly"),
        ],
        "\U0001f4ca Status & Info": [
            ("/status", "Show application state summary"),
            ("/model", "Show or change current model"),
            ("/cost", "Show API cost and usage report"),
            ("/context", "Show context window usage"),
            ("/tasks", "Show current task list"),
            ("/memory", "Show memory system status"),
        ],
        "\U0001f4dd File Operations": [
            ("/ls [path]", "List files in directory"),
            ("/grep <pattern>", "Search text in files"),
            ("/read <path>", "Read a file directly"),
            ("/write <path>", "Write content to file"),
            ("/edit <path>", "Edit file by exact replacement"),
            ("/patch <path>", "Apply multiple replacements in one go"),
            ("/modify <path>", "Replace file with reviewable diff"),
        ],
        "\U0001f4c5 Session Management": [
            ("/resume [id]", "Resume a saved session"),
            ("/transcript-save <path>", "Save transcript to text file"),
            ("/retry", "Retry the last prompt"),
            ("/permissions", "Show permission storage path"),
            ("/config-paths", "Show settings file paths"),
        ],
    }

    for group_name, commands in command_groups.items():
        lines.append(f"║ {group_name:<54}║")
        for cmd, desc in commands:
            cmd_display = f"    {cmd}"
            lines.append(f"║ {cmd_display:<20} {desc:<33} ║")
        lines.append("╟───────────────────────────────────────────────────────────────────────────╢")

    lines.extend([
        "║ \U0001f4a1 Tips:                                              ║",
        "║ - Use Tab to autocomplete commands                    ║",
        "║ - Prefix with / to access any command                 ║",
        "║ - Type naturally - I'll understand Chinese & English  ║",
        "╚═══════════════════════════════════════════════════════════════════════════╝",
    ])
    
    return "\n".join(lines)


def find_matching_slash_commands(user_input: str) -> list[str]:
    return [command.usage for command in SLASH_COMMANDS if command.usage.startswith(user_input)]


def complete_slash_command(line: str) -> tuple[list[str], str]:
    hits = [command.usage for command in SLASH_COMMANDS if command.usage.startswith(line)]
    return (hits if hits else [command.usage for command in SLASH_COMMANDS], line)


def try_handle_local_command(user_input: str, tools=None) -> str | None:
    if user_input in {"/", "/help"}:
        return format_slash_commands()

    if user_input == "/config-paths":
        return "\n".join(
            [
                f"pepsi-code settings: {PEPSI_CODE_SETTINGS_PATH}",
                f"pepsi-code permissions: {PEPSI_CODE_PERMISSIONS_PATH}",
                f"pepsi-code mcp: {PEPSI_CODE_MCP_PATH}",
                f"compat fallback: {CLAUDE_SETTINGS_PATH}",
            ]
        )

    if user_input == "/permissions":
        return f"permission store: {PEPSI_CODE_PERMISSIONS_PATH}"

    if user_input == "/skills":
        skills = tools.get_skills() if tools else []
        if not skills:
            return "No skills discovered. Add skills under ~/.pepsi-code/skills/<name>/SKILL.md, .pepsi-code/skills/<name>/SKILL.md, .claude/skills/<name>/SKILL.md, or ~/.claude/skills/<name>/SKILL.md."
        return "\n".join(
            f"{skill['name']}  {skill['description']}  [{skill['source']}]"
            for skill in skills
        )

    if user_input == "/config":
        from pepsicode.config import format_config_diagnostic
        return format_config_diagnostic()

    if user_input == "/memory":
        # Memory system display
        try:
            import os
            from pepsicode.memory import MemoryManager, MemoryScope
            memory_mgr = MemoryManager(workspace=os.getcwd())

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
                    lines.append(f"  - {entry.content[:80]}{tags_str}")
            else:
                lines.append("  No entries yet")

            return "\n".join(lines)
        except Exception as e:
            return f"Error loading memory: {e}"

    if user_input == "/context":
        # Context usage display
        try:
            from pepsicode.context_manager import load_context_state
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
            runtime = load_runtime_config()
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"
        auth = "ANTHROPIC_AUTH_TOKEN" if runtime.get("authToken") else "ANTHROPIC_API_KEY"
        return "\n".join(
            [
                f"model: {runtime['model']}",
                f"baseUrl: {runtime['baseUrl']}",
                f"auth: {auth}",
                f"mcp servers: {len(runtime.get('mcpServers', {}))}",
                runtime["sourceSummary"],
            ]
        )

    if user_input == "/model":
        try:
            runtime = load_runtime_config()
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"
        return f"current model: {runtime['model']}"

    if user_input.startswith("/model "):
        model = user_input[len("/model ") :].strip()
        if not model:
            return "usage: /model <model-name>"
        save_mini_code_settings({"model": model})
        return f"saved model={model} to {PEPSI_CODE_SETTINGS_PATH}"

    return None
