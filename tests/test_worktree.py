"""Tests for the pepsicode.worktree package.

These tests create a real ( disposable ) git repository under pytest's
``tmp_path`` so we exercise the full git subprocess path.  No mocking.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pepsicode.tooling import ToolContext
from pepsicode.tools.worktree import worktree_tool
from pepsicode.worktree import WorktreeError, WorktreeManager
from pepsicode.worktree.cleanup import cleanup_stale_worktrees
from pepsicode.worktree.models import WorktreeSession
from pepsicode.worktree.session import load_worktree_session, save_worktree_session
from pepsicode.worktree.slug import flatten_slug, validate_slug

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a real git repo with one commit on the default branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init", "-b", "main"], str(repo))
    _run(["config", "user.email", "test@example.com"], str(repo))
    _run(["config", "user.name", "Test"], str(repo))
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run(["add", "."], str(repo))
    _run(["commit", "-m", "initial"], str(repo))
    return repo


# ---------------------------------------------------------------------------
# slug validation
# ---------------------------------------------------------------------------


def test_validate_slug_accepts_simple_name() -> None:
    assert validate_slug("feature-auth") is None


def test_validate_slug_accepts_nested_name() -> None:
    assert validate_slug("feature/auth/login") is None


def test_validate_slug_rejects_empty() -> None:
    assert validate_slug("") == "name cannot be empty"


def test_validate_slug_rejects_dot_segment() -> None:
    assert validate_slug("../etc/passwd") is not None
    assert validate_slug("foo/../bar") is not None


def test_validate_slug_rejects_invalid_chars() -> None:
    assert validate_slug("foo bar") is not None
    assert validate_slug("foo$bar") is not None


def test_flatten_slug_replaces_slashes() -> None:
    assert flatten_slug("feature/auth") == "feature+auth"
    assert flatten_slug("simple") == "simple"


# ---------------------------------------------------------------------------
# WorktreeManager: create / enter / exit / list / status
# ---------------------------------------------------------------------------


def test_create_worktree_succeeds(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("test-feature")
    assert wt.name == "test-feature"
    assert os.path.isdir(wt.path)
    # worktree should have its own .git file pointing to the shared git dir
    assert (Path(wt.path) / ".git").exists()
    # branch should be named worktree-<slug>
    assert wt.branch == "worktree-test-feature"


def test_create_worktree_validates_name(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    with pytest.raises(WorktreeError, match="cannot be empty"):
        mgr.create("")
    with pytest.raises(WorktreeError, match="invalid segment"):
        mgr.create("bad name!")


def test_create_worktree_rejects_duplicate(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    mgr.create("dup")
    with pytest.raises(WorktreeError, match="already exists"):
        mgr.create("dup")


def test_create_worktree_fast_recovery(git_repo: Path) -> None:
    """A fresh manager should pick up a worktree left on disk by a previous run."""
    mgr1 = WorktreeManager(repo_root=str(git_repo))
    wt1 = mgr1.create("persist")
    head_sha = wt1.head_commit
    # Simulate pepsicode restart: brand-new manager instance
    mgr2 = WorktreeManager(repo_root=str(git_repo))
    wt2 = mgr2.create("persist")
    assert wt2.head_commit == head_sha
    assert os.path.isdir(wt2.path)


def test_enter_persists_session(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    mgr.create("session-test")
    session = mgr.enter("session-test")

    assert session.worktree_name == "session-test"
    assert session.original_cwd  # should have captured original cwd
    assert mgr.current_session is not None

    # session file should be on disk
    pepsi_dir = git_repo / ".pepsi-code"
    loaded = load_worktree_session(pepsi_dir)
    assert loaded is not None
    assert loaded.worktree_name == "session-test"


def test_exit_keep_keeps_worktree_on_disk(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    mgr.create("keep-me")
    mgr.enter("keep-me")
    mgr.exit("keep-me", action="keep")

    # worktree should still be on disk
    wt_path = os.path.join(str(git_repo), ".pepsi-code", "worktrees", "keep-me")
    assert os.path.isdir(wt_path)
    # session should be cleared
    assert mgr.current_session is None


def test_exit_remove_deletes_worktree(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("delete-me")
    mgr.enter("delete-me")
    mgr.exit("delete-me", action="remove")

    assert not os.path.isdir(wt.path)
    assert "delete-me" not in mgr.active


def test_exit_remove_refuses_dirty_worktree(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("dirty")
    # Make an uncommitted change inside the worktree
    (Path(wt.path) / "new.txt").write_text("data", encoding="utf-8")
    mgr.enter("dirty")
    with pytest.raises(WorktreeError, match="has changes"):
        mgr.exit("dirty", action="remove")


def test_exit_remove_force_discards_dirty(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("force-dirty")
    (Path(wt.path) / "new.txt").write_text("data", encoding="utf-8")
    mgr.enter("force-dirty")
    # force_remove should bypass the dirty check
    mgr.exit("force-dirty", action="remove", discard_changes=True)
    assert not os.path.isdir(wt.path)


def test_list_worktrees(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    mgr.create("a")
    mgr.create("b")
    names = {wt.name for wt in mgr.list_worktrees()}
    assert names == {"a", "b"}


def test_status_returns_serializable_dict(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    mgr.create("statable")
    mgr.enter("statable")
    status = mgr.status()
    assert status["repo_root"] == str(git_repo)
    assert len(status["active_worktrees"]) == 1
    assert status["active_worktrees"][0]["name"] == "statable"
    assert status["current_session"] is not None
    assert status["current_session"]["worktree_name"] == "statable"


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def test_save_load_session_roundtrip(tmp_path: Path) -> None:
    pepsi_dir = tmp_path / ".pepsi-code"
    pepsi_dir.mkdir()
    original = WorktreeSession(
        original_cwd="/home/user/project",
        worktree_path="/home/user/project/.pepsi-code/worktrees/foo",
        worktree_name="foo",
        original_branch="main",
        original_head_commit="abc123",
        session_id="sess-1",
    )
    save_worktree_session(pepsi_dir, original)
    loaded = load_worktree_session(pepsi_dir)
    assert loaded is not None
    assert loaded.worktree_name == "foo"
    assert loaded.original_branch == "main"
    assert loaded.session_id == "sess-1"


def test_save_none_clears_session(tmp_path: Path) -> None:
    pepsi_dir = tmp_path / ".pepsi-code"
    pepsi_dir.mkdir()
    original = WorktreeSession(
        original_cwd="/x",
        worktree_path="/y",
        worktree_name="n",
        original_branch="main",
        original_head_commit="abc",
    )
    save_worktree_session(pepsi_dir, original)
    save_worktree_session(pepsi_dir, None)
    assert load_worktree_session(pepsi_dir) is None


def test_load_session_returns_none_for_missing_file(tmp_path: Path) -> None:
    pepsi_dir = tmp_path / ".pepsi-code"
    pepsi_dir.mkdir()
    assert load_worktree_session(pepsi_dir) is None


# ---------------------------------------------------------------------------
# restore_session
# ---------------------------------------------------------------------------


def test_restore_session_recovers_across_manager_instances(git_repo: Path) -> None:
    mgr1 = WorktreeManager(repo_root=str(git_repo))
    mgr1.create("restore-me")
    session1 = mgr1.enter("restore-me")

    # New manager instance simulates pepsicode restart
    mgr2 = WorktreeManager(repo_root=str(git_repo))
    restored = mgr2.restore_session()
    assert restored is not None
    assert restored.worktree_name == "restore-me"
    assert restored.worktree_path == session1.worktree_path
    assert "restore-me" in mgr2.active


def test_restore_session_returns_none_when_no_session(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    assert mgr.restore_session() is None


def test_restore_session_clears_orphaned_session_file(git_repo: Path, tmp_path: Path) -> None:
    """If the worktree directory was deleted out-of-band, restore should clear the stale session."""
    mgr1 = WorktreeManager(repo_root=str(git_repo))
    wt = mgr1.create("orphan")
    mgr1.enter("orphan")

    # Manually delete the worktree directory (simulating user `rm -rf`)
    import shutil

    shutil.rmtree(wt.path, ignore_errors=True)

    mgr2 = WorktreeManager(repo_root=str(git_repo))
    assert mgr2.restore_session() is None
    # session file should have been cleared
    pepsi_dir = git_repo / ".pepsi-code"
    assert load_worktree_session(pepsi_dir) is None


# ---------------------------------------------------------------------------
# read_worktree_head_sha (the fast-recovery helper)
# ---------------------------------------------------------------------------


def test_read_worktree_head_sha_returns_commit(git_repo: Path) -> None:
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("sha-test")
    sha = WorktreeManager.read_worktree_head_sha(wt.path)
    assert sha is not None
    assert len(sha) == 40  # full SHA-1 hex


def test_read_worktree_head_sha_returns_none_for_nonexistent(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-worktree"
    bogus.mkdir()
    assert WorktreeManager.read_worktree_head_sha(str(bogus)) is None


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_cleanup_removes_ephemeral_unchanged_worktrees(git_repo: Path) -> None:
    """Stale ephemeral worktrees with no changes should be removed."""
    # Create an ephemeral-named worktree, then simulate it being old
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("agent-a1234567")

    # Backdate the worktree directory's mtime to 48h ago
    import time

    old_time = time.time() - 48 * 3600
    os.utime(wt.path, (old_time, old_time))

    removed = cleanup_stale_worktrees(mgr, cutoff_hours=24)
    assert removed == 1
    assert not os.path.isdir(wt.path)


def test_cleanup_skips_non_ephemeral_names(git_repo: Path) -> None:
    """User-named worktrees (not matching ephemeral patterns) should never be auto-cleaned."""
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("my-important-work")

    import time

    old_time = time.time() - 48 * 3600
    os.utime(wt.path, (old_time, old_time))

    removed = cleanup_stale_worktrees(mgr, cutoff_hours=24)
    assert removed == 0
    assert os.path.isdir(wt.path)


def test_cleanup_skips_dirty_worktrees(git_repo: Path) -> None:
    """Even ephemeral worktrees should be kept if they have uncommitted changes."""
    mgr = WorktreeManager(repo_root=str(git_repo))
    wt = mgr.create("wf-test123")
    (Path(wt.path) / "uncommitted.txt").write_text("data", encoding="utf-8")

    import time

    old_time = time.time() - 48 * 3600
    os.utime(wt.path, (old_time, old_time))

    removed = cleanup_stale_worktrees(mgr, cutoff_hours=24)
    assert removed == 0
    assert os.path.isdir(wt.path)


# ---------------------------------------------------------------------------
# Tool integration (tests the worktree_tool wrapper)
# ---------------------------------------------------------------------------


def test_worktree_tool_list_returns_empty_message(git_repo: Path) -> None:
    result = worktree_tool.run(
        {"action": "list"},
        ToolContext(cwd=str(git_repo), permissions=None),
    )
    assert result.ok is True
    assert "No active worktrees" in result.output


def test_worktree_tool_create_and_list(git_repo: Path) -> None:
    ctx = ToolContext(cwd=str(git_repo), permissions=None)

    create_result = worktree_tool.run(
        {"action": "create", "name": "via-tool"},
        ctx,
    )
    assert create_result.ok is True
    assert "Created worktree 'via-tool'" in create_result.output

    list_result = worktree_tool.run({"action": "list"}, ctx)
    assert list_result.ok is True
    assert "via-tool" in list_result.output


def test_worktree_tool_validation_rejects_missing_name() -> None:
    with pytest.raises(ValueError, match="name is required"):
        worktree_tool.validator({"action": "create"})


def test_worktree_tool_validation_rejects_bad_action() -> None:
    with pytest.raises(ValueError, match="action must be one of"):
        worktree_tool.validator({"action": "bogus"})


def test_worktree_tool_status_returns_json(git_repo: Path) -> None:
    import json

    ctx = ToolContext(cwd=str(git_repo), permissions=None)
    worktree_tool.run({"action": "create", "name": "json-test"}, ctx)

    result = worktree_tool.run({"action": "status"}, ctx)
    assert result.ok is True
    parsed = json.loads(result.output)
    assert parsed["repo_root"] == str(git_repo)
    assert any(wt["name"] == "json-test" for wt in parsed["active_worktrees"])
