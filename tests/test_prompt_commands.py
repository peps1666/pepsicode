import asyncio
from pathlib import Path

import pytest

from pepsicode.cli.cli_commands import (
    complete_slash_command,
    expand_prompt_command,
    find_matching_slash_commands,
    prompt_catalog,
    try_handle_local_command,
)
from pepsicode.cli.prompt_commands import MAX_TEMPLATE_BYTES, PromptCommandCatalog, expand_arguments


@pytest.fixture
def command_project(tmp_path, monkeypatch):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return project, home


def write_command(root, name="review", body="Review ${1:-correctness}."):
    target = root / ".pepsi-code" / "commands" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "body,arguments,expected",
    [
        ("$1 / $2 / $3", '"API compatibility" Windows', "API compatibility / Windows / "),
        ("$ARGUMENTS | $@", "中文 'two words'", "中文 two words | 中文 two words"),
        ("${1:-fallback} ${@:-all}", "", "fallback all"),
        ("${1:-fallback}", "''", "fallback"),
        ("$$1 $$ARGUMENTS $1", "literal", "$1 $ARGUMENTS literal"),
        ("$1", r'"C:\Program Files\app"', r"C:\Program Files\app"),
        ("$1", r"C:\src\module.py", r"C:\src\module.py"),
        ("$1", "'$2 $(echo injected) `cmd`'", "$2 $(echo injected) `cmd`"),
    ],
)
def test_parameter_expansion_is_single_pass_and_preserves_literal_paths(body, arguments, expected):
    assert expand_arguments(body, arguments) == expected


def test_invalid_or_excessive_expansion_fails_before_agent_execution():
    with pytest.raises(ValueError, match="arguments"):
        expand_arguments("$1", '"unfinished')
    with pytest.raises(ValueError, match="empty"):
        expand_arguments("$1", "")
    with pytest.raises(ValueError, match="32000"):
        expand_arguments("$1", "x" * 32001)
    with pytest.raises(ValueError, match="128000"):
        expand_arguments("$@" * 10, "x" * 32000)


def test_project_precedence_metadata_and_existing_prompt_command_api(command_project):
    project, home = command_project
    write_command(home, body="Personal command")
    path = write_command(
        project, body='\ufeff---\ndescription: "检查代码"\nargument-hint: "[focus]"\n---\nReview ${1:-correctness}.'
    )
    catalog = prompt_catalog(str(project), refresh=True)
    command = catalog.commands["/review"]
    assert command.description == "检查代码"
    assert command.metadata.usage == "/review [focus]"
    assert command.source == "project"
    message = expand_prompt_command("/review security", str(project))
    assert message == {
        "role": "user",
        "content": "Review security.",
        "displayContent": "/review security",
        "commandPath": str(path.resolve()),
    }

    async def execute():
        return await command.execute("security", {})

    assert asyncio.run(execute()).output == "Review security."


def test_reserved_commands_cannot_be_overridden_and_malformed_override_is_not_hidden(command_project):
    project, home = command_project
    write_command(project, "help", "malicious replacement")
    write_command(project, body="---\ndescription: incomplete")
    write_command(home, body="must not silently fall back")
    output = try_handle_local_command("/commands", cwd=str(project))
    assert "reserved built-in" in output and "frontmatter" in output
    assert expand_prompt_command("/help", str(project)) is None
    with pytest.raises(ValueError, match="Cannot load command"):
        expand_prompt_command("/review", str(project))


def test_help_and_reserved_names_follow_new_command_metadata_after_catalog_is_cached(command_project, monkeypatch):
    from pepsicode.cli import cli_commands

    project, _ = command_project
    write_command(project, "extension-command", "Previously a prompt template")
    assert expand_prompt_command("/extension-command", str(project)) is not None
    added = cli_commands.SlashCommand(
        "/extension-command", "/extension-command [value]", "Runtime extension", "New category"
    )
    monkeypatch.setattr(cli_commands, "SLASH_COMMANDS", [*cli_commands.SLASH_COMMANDS, added])
    output = cli_commands.format_slash_commands(str(project))
    assert "Runtime extension" in output and "New category" in output
    assert "/extension-command [value]" in output
    assert expand_prompt_command("/extension-command", str(project)) is None
    assert "reserved built-in" in cli_commands.prompt_catalog(str(project)).errors["/extension-command"]


def test_command_behavior_and_rpc_source_are_independent_of_display_category(command_project, monkeypatch):
    from pepsicode.cli import cli_commands
    from pepsicode.server.app import ClientSession, PepsiCodeServer

    project, _ = command_project
    write_command(project, "workflow", "A custom workflow")
    builtin = cli_commands.SlashCommand("/extension", "/extension [value]", "Local extension", "Custom")
    monkeypatch.setattr(cli_commands, "SLASH_COMMANDS", [*cli_commands.SLASH_COMMANDS, builtin])
    assert cli_commands.complete_slash_command("/extension", str(project))[0] == ["/extension [value]"]
    assert cli_commands.complete_slash_command("/workflow", str(project))[0] == ["/workflow"]

    async def scenario():
        result = await PepsiCodeServer()._dispatch(
            "commands/list", {}, ClientSession(str(project)), None, None, asyncio.get_running_loop()
        )
        commands = {item["name"]: item for item in result["commands"]}
        assert commands["/extension"]["source"] == "builtin"
        assert commands["/workflow"]["source"] == "project"

    asyncio.run(scenario())


def test_edit_add_remove_and_explicit_reload_do_not_leave_stale_commands(command_project):
    project, _ = command_project
    path = write_command(project, body="first")
    assert expand_prompt_command("/review", str(project))["content"] == "first"
    path.write_text("updated", encoding="utf-8")
    assert expand_prompt_command("/review", str(project))["content"] == "updated"
    write_command(project, "fix-test", "Fix tests")
    assert "/fix-test" in try_handle_local_command("/commands reload", cwd=str(project))
    path.unlink()
    assert expand_prompt_command("/review", str(project)) is None
    assert "/review" not in find_matching_slash_commands("/rev", str(project))


def test_completions_show_hints_but_do_not_insert_them(command_project):
    project, _ = command_project
    write_command(project, body="---\nargument-hint: '[focus]'\n---\nReview $@")
    assert "/review [focus]" in find_matching_slash_commands("/rev", str(project))
    assert complete_slash_command("/rev", str(project))[0] == ["/review"]
    assert "/review" in try_handle_local_command("/help", cwd=str(project))
    assert try_handle_local_command("/commands invalid", cwd=str(project)) == "Usage: /commands [reload]"


def test_bad_encoding_empty_body_and_large_file_are_diagnosed(command_project):
    project, _ = command_project
    write_command(project, "empty", "---\ndescription: only metadata\n---\n")
    write_command(project, "large", "x" * (MAX_TEMPLATE_BYTES + 1))
    write_command(project, "invalid name", "text")
    invalid = write_command(project, "invalid")
    invalid.write_bytes(b"\xff")
    catalog = prompt_catalog(str(project), refresh=True)
    assert not catalog.commands
    assert set(catalog.errors) == {"/empty", "/large", "/invalid name", "/invalid"}


def test_file_symlink_cannot_load_a_file_outside_the_command_directory(command_project):
    project, home = command_project
    target = home / "private.md"
    target.write_text("private text", encoding="utf-8")
    path = write_command(project)
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    catalog = PromptCommandCatalog(str(project), str(home), set()).refresh(force=True)
    assert "/review" in catalog.errors and not catalog.commands


def test_nested_files_are_not_implicitly_loaded(command_project):
    project, _ = command_project
    nested = project / ".pepsi-code" / "commands" / "nested"
    nested.mkdir(parents=True)
    (nested / "hidden.md").write_text("not a command", encoding="utf-8")
    assert not prompt_catalog(str(project), refresh=True).commands
