import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

from pepsicode.intelligence.fallback import references, symbols
from pepsicode.intelligence.lsp import LanguageServers, offset, position
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.code_intelligence import _apply_rename, create_code_intelligence_tools


@pytest.fixture
def intelligence(tmp_path):
    server = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"
    manager = LanguageServers(
        str(tmp_path), {"python": {"command": sys.executable, "args": [str(server)], "timeout": 2}}
    )
    (tmp_path / "source.py").write_text("foo = 1\n", encoding="utf-8")
    (tmp_path / "use.py").write_text("foo()\n", encoding="utf-8")
    registry = ToolRegistry(create_code_intelligence_tools(manager))
    try:
        yield tmp_path, manager, registry, ToolContext(str(tmp_path))
    finally:
        manager.close()


@pytest.mark.parametrize("action", ["symbols", "definition", "references", "diagnostics"])
def test_stdio_language_server_navigation(intelligence, action):
    _, manager, registry, context = intelligence
    result = registry.execute(
        "code_intelligence", {"file_path": "source.py", "action": action, "line": 1, "column": 1}, context
    )
    assert result.ok, result.output
    response = json.loads(result.output)
    assert response["backend"] == "lsp"
    assert response["result"]
    assert len(manager.clients) == 1


def test_language_server_sees_disk_edits_and_fresh_diagnostics(intelligence):
    root, _, registry, context = intelligence
    registry.execute("code_intelligence", {"file_path": "source.py", "action": "symbols"}, context)
    (root / "source.py").write_text("changed = 2\n", encoding="utf-8")
    result = registry.execute("code_intelligence", {"file_path": "source.py", "action": "symbols"}, context)
    assert json.loads(result.output)["result"][0]["name"] == "changed = 2"
    result = registry.execute("code_intelligence", {"file_path": "source.py", "action": "diagnostics"}, context)
    assert json.loads(result.output)["result"]["items"] == []


def test_rename_updates_all_files_after_validation(intelligence):
    root, _, registry, context = intelligence
    result = registry.execute(
        "rename_symbol", {"file_path": "source.py", "line": 1, "column": 1, "new_name": "renamed"}, context
    )
    assert result.ok, result.output
    assert (root / "source.py").read_text() == "renamed = 1\n"
    assert (root / "use.py").read_text() == "renamed()\n"
    assert registry.mutation_count == 1


def test_rename_preview_never_writes(intelligence):
    root, _, registry, context = intelligence
    result = registry.execute(
        "code_intelligence", {"action": "rename", "file_path": "source.py", "new_name": "renamed"}, context
    )
    assert result.ok
    assert (root / "source.py").read_text() == "foo = 1\n"


def test_rename_rolls_back_after_second_file_write_fails(intelligence, monkeypatch):
    from pepsicode.tools import code_intelligence

    root, _, registry, context = intelligence
    original = code_intelligence.os.replace
    calls = []

    def replace(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("simulated write failure")
        original(source, target)

    monkeypatch.setattr(code_intelligence.os, "replace", replace)
    result = registry.execute(
        "rename_symbol",
        {
            "file_path": "source.py",
            "line": 1,
            "column": 1,
            "new_name": "renamed",
        },
        context,
    )
    assert not result.ok and "simulated write failure" in result.output
    assert (root / "source.py").read_text() == "foo = 1\n"
    assert (root / "use.py").read_text() == "foo()\n"
    assert not list(root.glob(".pepsi-rename-*"))


def test_edit_denial_prevents_all_rename_writes(intelligence):
    root, manager, _, context = intelligence
    denied = Mock(is_denied=True, is_unavailable=False)
    denied.denial_message.return_value = "denied"
    context.permissions = Mock()
    context.permissions.check_path_access.return_value = Mock(is_denied=False, is_unavailable=False)
    context.permissions.check_edit.return_value = denied
    edit = {
        "changes": {
            (root / "source.py").as_uri(): [
                {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}, "newText": "bar"}
            ]
        }
    }
    with pytest.raises(PermissionError):
        _apply_rename(edit, manager, context)
    assert (root / "source.py").read_text() == "foo = 1\n"


def test_overlapping_and_outside_rename_edits_are_rejected(intelligence, tmp_path):
    root, manager, _, context = intelligence
    change = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}, "newText": "bar"}
    with pytest.raises(ValueError, match="Overlapping"):
        _apply_rename({"changes": {(root / "source.py").as_uri(): [change, change]}}, manager, context)
    with pytest.raises(PermissionError):
        _apply_rename({"changes": {(tmp_path.parent / "outside.py").as_uri(): [change]}}, manager, context)
    assert (root / "source.py").read_text() == "foo = 1\n"


def test_utf16_positions_preserve_emoji():
    assert position("😀value", 1, 2) == {"line": 0, "character": 2}
    assert offset("😀value", {"line": 0, "character": 2}) == 1
    with pytest.raises(ValueError):
        offset("😀value", {"line": 0, "character": 1})


def test_fallback_is_multilanguage_and_excludes_comments_strings_and_substrings(tmp_path):
    source = tmp_path / "app.tsx"
    source.write_text('export function foo() {}\nconst foobar = "foo"; // foo\nfoo();\n', encoding="utf-8")
    assert any(item["name"] == "foo" for item in symbols(source))
    assert [item["line"] for item in references(source, "foo")] == [1, 3]
    manager = LanguageServers(str(tmp_path), {"typescript": False})
    registry = ToolRegistry(create_code_intelligence_tools(manager))
    result = registry.execute(
        "code_intelligence",
        {"file_path": "app.tsx", "action": "references", "line": 3, "column": 1},
        ToolContext(str(tmp_path)),
    )
    assert result.ok and json.loads(result.output)["backend"] == "fallback"
    result = registry.execute(
        "rename_symbol", {"file_path": "app.tsx", "line": 3, "column": 1, "new_name": "bar"}, ToolContext(str(tmp_path))
    )
    assert not result.ok
    assert "foo();" in source.read_text()


def test_read_only_mode_cannot_apply_rename(intelligence):
    root, _, registry, context = intelligence
    context.permissions = PermissionManager(str(root), mode="read_only")
    result = registry.execute(
        "rename_symbol", {"file_path": "source.py", "line": 1, "column": 1, "new_name": "bar"}, context
    )
    assert not result.ok
    assert (root / "source.py").read_text() == "foo = 1\n"


@pytest.mark.skipif(shutil.which("pylsp") is None, reason="optional python language server is not installed")
def test_real_pylsp_cross_file_navigation_and_rename(tmp_path):
    (tmp_path / "api.py").write_text("def square(value):\n    return value * value\n", encoding="utf-8")
    (tmp_path / "use.py").write_text("from api import square\n\nresult = square(3)\n", encoding="utf-8")
    # Keep Jedi's hashed cache filenames below Windows' legacy path limit.
    cache = TemporaryDirectory(prefix="lsp-")
    manager = LanguageServers(
        str(tmp_path),
        {
            "python": {
                "command": "pylsp",
                "timeout": 30,
                "env": {"LOCALAPPDATA": cache.name, "XDG_CACHE_HOME": cache.name},
            }
        },
    )
    registry = ToolRegistry(create_code_intelligence_tools(manager))
    context = ToolContext(str(tmp_path))
    try:
        for action in ("definition", "references"):
            result = registry.execute(
                "code_intelligence",
                {
                    "file_path": "use.py",
                    "action": action,
                    "line": 3,
                    "column": 11,
                },
                context,
            )
            response = json.loads(result.output)
            assert result.ok and response["backend"] == "lsp" and response["result"], result.output
        result = registry.execute(
            "rename_symbol",
            {
                "file_path": "api.py",
                "line": 1,
                "column": 6,
                "new_name": "squared",
            },
            context,
        )
        assert result.ok, result.output
        assert "def squared" in (tmp_path / "api.py").read_text()
        assert "result = squared(3)" in (tmp_path / "use.py").read_text()
    finally:
        manager.close()
        cache.cleanup()


def test_invalid_server_environment_is_rejected(tmp_path):
    manager = LanguageServers(str(tmp_path), {"python": {"command": sys.executable, "env": {"KEY": 12}}})
    with pytest.raises(ValueError, match="env must map"):
        manager.client(tmp_path / "file.py", ToolContext(str(tmp_path)))


def test_custom_language_extensions_and_initialization_options_work_without_code_changes(tmp_path):
    server = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"
    source = tmp_path / "source.csx"
    source.write_text("class Example {}\n")
    manager = LanguageServers(
        str(tmp_path),
        {
            "custom_dotnet": {
                "command": sys.executable,
                "args": [str(server)],
                "extensions": [".cs", ".csx"],
                "languageId": "csharp",
                "initializationOptions": {"projectMode": "script"},
                "timeout": 90,
            }
        },
    )
    try:
        context = ToolContext(str(tmp_path))
        client = manager.client(source, context)
        result = client.query("symbols", source, 1, 1, context)
        assert result[0]["detail"] == "csharp"
        assert client.capabilities["experimental"]["initializationOptions"] == {"projectMode": "script"}
        assert client.timeout == 90
    finally:
        manager.close()


def test_explicit_javascript_and_typescript_servers_do_not_share_configuration(tmp_path):
    server = Path(__file__).parent / "fixtures" / "fake_lsp_server.py"
    config = {"command": sys.executable, "args": [str(server)]}
    manager = LanguageServers(str(tmp_path), {"typescript": config, "javascriptreact": config})
    try:
        context = ToolContext(str(tmp_path))
        js = manager.client(tmp_path / "app.jsx", context)
        ts = manager.client(tmp_path / "app.ts", context)
        assert js is not ts and len(manager.clients) == 2
    finally:
        manager.close()


@pytest.mark.parametrize("name", ["$state", "名字", "foo-bar"])
def test_rename_syntax_is_validated_by_language_server_not_python(intelligence, name):
    _, _, registry, _ = intelligence
    parsed = registry.find("rename_symbol").validator(
        {"file_path": "component.ts", "line": 1, "column": 1, "new_name": name}
    )
    assert parsed["new_name"] == name


@pytest.mark.parametrize("name", [None, "", "  ", "foo\nbar", "foo\0bar"])
def test_rename_preview_and_execution_validate_required_name(intelligence, name):
    _, _, registry, _ = intelligence
    for tool in ("code_intelligence", "rename_symbol"):
        with pytest.raises(ValueError, match="new_name"):
            registry.find(tool).validator({"file_path": "file.ts", "action": "rename", "new_name": name})


def test_same_position_insertions_keep_language_server_order(intelligence):
    root, manager, _, context = intelligence
    point = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    edit = {
        "changes": {
            (root / "source.py").as_uri(): [
                {"range": point, "newText": "z"},
                {"range": point, "newText": "a"},
            ]
        }
    }
    _apply_rename(edit, manager, context)
    assert (root / "source.py").read_text() == "zafoo = 1\n"
