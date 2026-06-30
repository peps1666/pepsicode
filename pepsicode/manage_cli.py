from __future__ import annotations

from typing import Any

from pepsicode.config import (
    get_mcp_config_path,
    load_scoped_mcp_servers,
    read_mcp_tokens,
    save_mcp_tokens,
    save_scoped_mcp_servers,
)
from pepsicode.skills import discover_skills, install_skill, remove_managed_skill


def _print_usage() -> None:
    print(
        "pepsicode management commands\n\n"
        "pepsicode mcp list [--project]\n"
        "pepsicode mcp add <name> [--project] [--url <url>] [--header KEY=VALUE ...]\n"
        "    [--protocol <auto|content-length|newline-json|streamable-http>]\n"
        "    [--env KEY=VALUE ...] -- <command> [args...]\n"
        "pepsicode mcp remove <name> [--project]\n"
        "pepsicode mcp login <name> --token <bearer-token>\n"
        "pepsicode mcp logout <name>\n\n"
        "pepsicode skills list\n"
        "pepsicode skills add <path-to-skill-or-dir> [--name <name>] [--project]\n"
        "pepsicode skills remove <name> [--project]"
    )


def _parse_scope(args: list[str]) -> tuple[str, list[str]]:
    rest = list(args)
    if "--project" in rest:
        rest.remove("--project")
        return "project", rest
    return "user", rest


def _take_option(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        raise RuntimeError(f"Missing value for {name}")
    value = args[index + 1]
    del args[index : index + 2]
    return value


def _take_repeat_option(args: list[str], name: str) -> list[str]:
    values: list[str] = []
    while name in args:
        index = args.index(name)
        if index + 1 >= len(args):
            raise RuntimeError(f"Missing value for {name}")
        values.append(args[index + 1])
        del args[index : index + 2]
    return values


def _parse_env_pairs(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise RuntimeError(f"Invalid --env value: {entry}")
        key, value = entry.split("=", 1)
        if not key.strip():
            raise RuntimeError(f"Invalid --env value: {entry}")
        env[key.strip()] = value
    return env


def _handle_mcp_command(cwd: str, args: list[str]) -> bool:
    if not args:
        _print_usage()
        return True
    subcommand, *rest_args = args
    scope, rest = _parse_scope(rest_args)
    if subcommand == "list":
        servers = load_scoped_mcp_servers(scope, cwd)
        if not servers:
            print(f"No MCP servers configured in {get_mcp_config_path(scope, cwd)}.")
            return True
        for name, server in servers.items():
            if server.get("url"):
                print(f"{name}: url={server['url']} type=http")
            else:
                args_summary = " ".join(server.get("args", []))
                protocol = f" protocol={server['protocol']}" if server.get("protocol") else ""
                print(f"{name}: {server['command']} {args_summary}{protocol}".strip())
        return True
    if subcommand == "add":
        if "--" not in rest:
            raise RuntimeError("Use `--` before the MCP command.")
        separator_index = rest.index("--")
        head = rest[:separator_index]
        command_parts = rest[separator_index + 1 :]
        if not head or not command_parts:
            raise RuntimeError("Missing MCP server name or command.")
        name = head.pop(0)
        url = _take_option(head, "--url")
        protocol = _take_option(head, "--protocol")
        env = _parse_env_pairs(_take_repeat_option(head, "--env"))
        headers = _parse_env_pairs(_take_repeat_option(head, "--header"))
        if head:
            raise RuntimeError(f"Unknown arguments: {' '.join(head)}")
        command, *command_args = command_parts
        existing = load_scoped_mcp_servers(scope, cwd)
        server_config: dict[str, Any] = {
            "command": command,
            "args": command_args,
            "env": env or None,
            "protocol": protocol,
        }
        if url:
            server_config["url"] = url
        if headers:
            server_config["headers"] = headers
        existing[name] = server_config
        save_scoped_mcp_servers(scope, existing, cwd)
        print(f"Added MCP server {name} to {get_mcp_config_path(scope, cwd)}")
        return True
    if subcommand == "remove":
        if not rest:
            raise RuntimeError("Missing MCP server name.")
        name = rest[0]
        existing = load_scoped_mcp_servers(scope, cwd)
        if name not in existing:
            print(f"MCP server {name} not found in {get_mcp_config_path(scope, cwd)}")
            return True
        del existing[name]
        save_scoped_mcp_servers(scope, existing, cwd)
        print(f"Removed MCP server {name} from {get_mcp_config_path(scope, cwd)}")
        return True
    if subcommand == "login":
        if not rest:
            raise RuntimeError("Missing MCP server name.")
        name = rest[0]
        token = _take_option(rest, "--token")
        if not token:
            raise RuntimeError("Missing --token <bearer-token>.")
        if rest:
            raise RuntimeError(f"Unknown arguments: {' '.join(rest)}")
        tokens = read_mcp_tokens()
        tokens[name] = token
        save_mcp_tokens(tokens)
        print(f"Saved token for MCP server {name}")
        return True
    if subcommand == "logout":
        if not rest:
            raise RuntimeError("Missing MCP server name.")
        name = rest[0]
        tokens = read_mcp_tokens()
        if name not in tokens:
            print(f"No token found for MCP server {name}")
            return True
        del tokens[name]
        save_mcp_tokens(tokens)
        print(f"Removed token for MCP server {name}")
        return True
    _print_usage()
    return True


def _handle_skills_command(cwd: str, args: list[str]) -> bool:
    if not args:
        _print_usage()
        return True
    subcommand, *rest_args = args
    scope, rest = _parse_scope(rest_args)
    if subcommand == "list":
        skills = discover_skills(cwd)
        if not skills:
            print("No skills discovered.")
            return True
        for skill in skills:
            print(f"{skill.name}: {skill.description} ({skill.path})")
        return True
    if subcommand == "add":
        if not rest:
            raise RuntimeError("Missing skill source path.")
        source_path = rest[0]
        name = _take_option(rest, "--name")
        result = install_skill(cwd, source_path, name=name, scope=scope)
        print(f"Installed skill {result['name']} at {result['targetPath']}")
        return True
    if subcommand == "remove":
        if not rest:
            raise RuntimeError("Missing skill name.")
        result = remove_managed_skill(cwd, rest[0], scope=scope)
        if not result["removed"]:
            print(f"Skill {rest[0]} not found at {result['targetPath']}")
            return True
        print(f"Removed skill {rest[0]} from {result['targetPath']}")
        return True
    _print_usage()
    return True


def maybe_handle_management_command(cwd: str, argv: list[str]) -> bool:
    if not argv:
        return False
    category, *rest = argv
    if category == "mcp":
        return _handle_mcp_command(cwd, rest)
    if category == "skills":
        return _handle_skills_command(cwd, rest)
    if category in {"help", "--help", "-h"}:
        _print_usage()
        return True
    return False

