"""Checked, versioned atomic file publication shared by all editing tools."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pepsicode.cancellation import TurnCancelledError, raise_if_cancelled
from pepsicode.execution.runtime import current_scope, current_tool_context


class StaleFileError(ValueError):
    """The observed file or its resolved target changed before publication."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    content: bytes | None
    version: str

    @property
    def text(self):
        return (self.content or b"").decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def version(content: bytes | None) -> str:
    return "missing" if content is None else hashlib.sha256(content).hexdigest()


class FileSystemService:
    """Owns per-target locks and per-task read versions, not a kernel sandbox."""

    def __init__(self):
        self._lock = threading.RLock()
        self._targets = weakref.WeakValueDictionary()
        self._observed = weakref.WeakKeyDictionary()

    def _task(self, context):
        scope = getattr(context, "execution", None) or current_scope()
        # Main-turn observations survive into the next turn of this session.
        # Children own independent views so a child's write cannot silently
        # bless an overwrite based on its parent's older model context.
        if scope and scope.parent_task_id is not None:
            return scope
        return context.permissions or (scope if scope and (scope.budget or scope.journal) else None)

    def context(self, path=None):
        from pepsicode.tooling import ToolContext

        context = current_tool_context()
        if context is not None:
            return context
        scope = current_scope()
        return ToolContext(
            scope.cwd if scope else str(Path(path or Path.cwd()).resolve().parent),
            permissions=scope.permissions if scope else None,
            execution=scope,
        )

    def read_text(self, path, *, context=None, encoding="utf-8"):
        snapshot = self.snapshot(context or self.context(path), path)
        if snapshot.content is None:
            raise FileNotFoundError(str(path))
        return snapshot.content.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")

    def read_bytes(self, path, *, context=None):
        snapshot = self.snapshot(context or self.context(path), path)
        if snapshot.content is None:
            raise FileNotFoundError(str(path))
        return snapshot.content

    def checked_path(self, path, intent="read"):
        return self._path(self.context(path), path, intent)

    def mkdir(self, path):
        context = self.context(path)
        target = self._path(context, path, "write")
        if target.is_dir():
            return
        mode = getattr(context.permissions, "mode", None)
        if context.permissions:
            outcome = context.permissions.check_edit(str(target), "Create directory")
            if not outcome.is_allowed:
                raise PermissionError(outcome.denial_message(scope=str(target)))
        if getattr(context.permissions, "mode", None) != mode:
            raise PermissionError("Permissions changed during directory approval")
        if self._path(context, path, "write") != target:
            raise StaleFileError("Directory target changed during approval")
        scope = getattr(context, "execution", None) or current_scope()
        if scope:
            scope.record("directory/prepared", path=str(target))
        target.mkdir(parents=True, exist_ok=True)
        context.effect_state = "committed"
        if scope:
            scope.record("directory/committed", path=str(target))

    def copy(self, source, destination):
        content = self.read_bytes(source)
        return self.write(self.context(destination), destination, content)

    def symlink(self, source, destination):
        context = self.context(destination)
        source = self._path(context, source, "read")
        target = self._path(context, destination, "write")
        mode = getattr(context.permissions, "mode", None)
        if context.permissions:
            outcome = context.permissions.check_edit(str(target), f"Create symlink to {source}")
            if not outcome.is_allowed:
                raise PermissionError(outcome.denial_message(scope=str(target)))
        if getattr(context.permissions, "mode", None) != mode:
            raise PermissionError("Permissions changed during symlink approval")
        if self._path(context, destination, "write") != target:
            raise StaleFileError("Symlink target changed during approval")
        scope = getattr(context, "execution", None) or current_scope()
        if scope:
            scope.record("symlink/prepared", source=str(source), path=str(target))
        os.symlink(str(source), str(target), target_is_directory=source.is_dir())
        context.effect_state = "committed"
        if scope:
            scope.record("symlink/committed", source=str(source), path=str(target))

    def _path(self, context, path, intent):
        from pepsicode.workspace import resolve_tool_path

        raise_if_cancelled()
        if context.cancellation_event is not None and context.cancellation_event.is_set():
            raise TurnCancelledError("File operation cancelled before dispatch")
        scope = getattr(context, "execution", None) or current_scope()
        if scope:
            scope.check_cancelled()
            if context.permissions is None:
                context.permissions = scope.permissions
            if intent not in {"read", "list", "search"} and scope.has_unknown:
                raise PermissionError("Reconcile unknown operations before further writes")
        target = resolve_tool_path(context, str(path), intent)
        if intent not in {"read", "list", "search"}:
            reserved = (Path(context.cwd) / ".pepsi-code" / "executions").resolve()
            if target == reserved or reserved in target.parents:
                raise PermissionError("Execution journals are owned by the harness and cannot be edited by tools")
        return target

    def snapshot(self, context, path, *, intent="read", for_edit=False, observe=True) -> FileSnapshot:
        target = self._path(context, path, intent)
        content = target.read_bytes() if target.exists() else None
        result = FileSnapshot(target, content, version(content))
        owner = self._task(context)
        if owner is not None and (observe or for_edit):
            with self._lock:
                observations = self._observed.setdefault(owner, {})
                observed = observations.get(str(target))
                if for_edit and observed is not None and observed != result.version:
                    raise StaleFileError(f"File changed since it was read: {target}; read it again before editing")
                if observe and not for_edit:
                    observations[str(target)] = result.version
        return result

    @contextmanager
    def _local_lock(self, lock, context):
        while not lock.acquire(timeout=0.05):
            self._path(context, context.cwd, "read")
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def _exclusive(self, target, context):
        with self._lock:
            lock = self._targets.setdefault(str(target), threading.RLock())
        with self._local_lock(lock, context):
            # The same physical target maps to the same OS lock across chats
            # and processes. Locks never live alongside model-controlled files.
            root = Path(tempfile.gettempdir()) / "pepsicode-file-locks"
            root.mkdir(parents=True, exist_ok=True)
            path = root / (hashlib.sha256(os.path.normcase(str(target)).encode()).hexdigest() + ".lock")
            with path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if not stream.tell():
                    stream.write(b"0")
                    stream.flush()
                while True:
                    self._path(context, target, "write")
                    try:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, PermissionError):
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def write(self, context, path, content: bytes, *, expected: str | None = None):
        scope = getattr(context, "execution", None) or current_scope()
        if scope and scope.has_unknown:
            raise PermissionError("Resolve the previous unknown operation before further writes")
        target = self._path(context, path, "write")
        original = target.read_bytes() if target.exists() else None
        owner = self._task(context)
        with self._lock:
            observed = self._observed.get(owner, {}).get(str(target)) if owner is not None else None
        expected = expected if expected is not None else observed if observed is not None else version(original)
        if version(original) != expected:
            raise StaleFileError(f"File changed since it was read: {target}; read it again before editing")
        if original == content:
            return target
        from pepsicode.core.file_review import build_unified_diff

        try:
            preview = build_unified_diff(str(target), (original or b"").decode("utf-8"), content.decode("utf-8"))
        except UnicodeDecodeError:
            preview = (
                f"Replace binary content: {len(original or b'')} -> {len(content)} bytes; SHA256 {version(content)}"
            )
        permission_mode = getattr(context.permissions, "mode", None)
        if context.permissions is not None:
            outcome = context.permissions.check_edit(str(target), preview)
            if outcome.is_denied or outcome.is_unavailable:
                raise PermissionError(outcome.denial_message(scope=str(target)))
        with self._exclusive(target, context):
            if getattr(context.permissions, "mode", None) != permission_mode:
                raise PermissionError("Permissions changed while the write was awaiting approval")
            fresh = self._path(context, path, "write")
            if fresh != target:
                raise StaleFileError("Resolved file target changed during approval")
            current = fresh.read_bytes() if fresh.exists() else None
            if version(current) != expected:
                raise StaleFileError(f"File changed during approval: {fresh}")
            scope = getattr(context, "execution", None) or current_scope()
            if scope:
                scope.check_cancelled()
                scope.record("file/prepared", path=str(fresh), before=expected, after=version(content))
            temporary = None
            try:
                fresh.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix=".pepsi-edit-", dir=fresh.parent)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if fresh.exists():
                    os.chmod(temporary, stat.S_IMODE(fresh.stat().st_mode))
                if scope:
                    scope.check_cancelled()
                raise_if_cancelled()
                if (
                    self._path(context, path, "write") != fresh
                    or version(fresh.read_bytes() if fresh.exists() else None) != expected
                ):
                    raise StaleFileError("File target changed before atomic replacement")
                os.replace(temporary, fresh)
                temporary = None
                context.effect_state = "committed"
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
            if owner is not None:
                with self._lock:
                    self._observed.setdefault(owner, {})[str(fresh)] = version(content)
            if scope:
                scope.record("file/committed", path=str(fresh), version=version(content))
            return fresh

    def write_text(self, context, path, text, *, expected=None):
        return self.write(context, path, text.encode("utf-8"), expected=expected)


# One explicit process-owned service coordinates callers in different sessions.
FILES = FileSystemService()
