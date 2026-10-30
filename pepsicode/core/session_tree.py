"""Conversation branches share a workspace; no operation here changes project files."""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pepsicode.core.session import SessionData

_recording: ContextVar = ContextVar("session_recording", default=None)


def current_session():
    return _recording.get()


def validate_tree(tree: dict) -> None:
    """Reject damaged graphs before they can replace a live session."""
    if tree == {}:
        return  # Legacy flat sessions are imported on first use.
    if not isinstance(tree, dict) or tree.get("version") != 1 or not isinstance(tree.get("nodes"), list):
        raise ValueError("Invalid session tree")
    seen = set()
    for node in tree["nodes"]:
        if not isinstance(node, dict):
            raise ValueError("Invalid session tree node")
        entry_id, parent = node.get("id"), node.get("parent_id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            raise ValueError("Invalid session tree ID")
        if parent is not None and (not isinstance(parent, str) or parent not in seen):
            raise ValueError("Invalid session tree parent")
        if not isinstance(node.get("message"), dict) or not isinstance(node["message"].get("role"), str):
            raise ValueError("Invalid session tree message")
        seen.add(entry_id)
    leaf = tree.get("leaf_id")
    if leaf is not None and (not isinstance(leaf, str) or leaf not in seen):
        raise ValueError("Invalid session tree leaf")


def _index(session: SessionData) -> dict:
    return {node["id"]: node for node in session.tree.get("nodes", [])}


def record_messages(session: SessionData, messages: list[dict]) -> None:
    with session._tree_lock:
        if not session.tree:
            session.tree = {"version": 1, "nodes": [], "leaf_id": None}
        index = _index(session)
        parent = session.tree["leaf_id"]
        for message in messages:
            if message.get("role") == "system":
                continue
            entry_id = message.get("entryId")
            if entry_id in index:
                for key in ("source", "task", "verification", "finishReason", "isError"):
                    if key in message:
                        index[entry_id]["message"][key] = copy.deepcopy(message[key])
                parent = entry_id
                continue
            entry_id = uuid.uuid4().hex
            message["entryId"] = entry_id
            node = {"id": entry_id, "parent_id": parent, "message": copy.deepcopy(message)}
            session.tree["nodes"].append(node)
            index[entry_id] = node
            parent = entry_id
        session.tree["leaf_id"] = parent
        session.messages = list(messages)


def checkpoint_messages(messages: list[dict]) -> None:
    session = _recording.get()
    if session is not None:
        record_messages(session, messages)


def previous_execution(runtime: dict) -> dict | None:
    session = _recording.get()
    return session.execution_state if session is not None else runtime.get("_lastExecution")


def checkpoint_execution(state: dict) -> None:
    """Persist the journal pointer before effects, even before the first autosave."""
    session = _recording.get()
    if session is not None:
        from pepsicode.core.session import save_session

        with session._tree_lock:
            session.execution_state = copy.deepcopy(state)
            save_session(session)


@contextmanager
def recording_session(session: SessionData | None):
    token = _recording.set(session)
    try:
        yield
    finally:
        _recording.reset(token)


def branch_path(session: SessionData, leaf_id: str | None = None) -> list[dict]:
    index = _index(session)
    current = session.tree.get("leaf_id") if leaf_id is None else leaf_id
    path, seen = [], set()
    while current is not None:
        if current in seen or current not in index:
            raise ValueError("Invalid session tree: broken or circular parent link")
        seen.add(current)
        node = index[current]
        path.append(node)
        current = node["parent_id"]
    return list(reversed(path))


def _is_user(node: dict) -> bool:
    message = node["message"]
    return message.get("role") == "user" and not message.get("internal")


def resolve_entry(session: SessionData, entry_id: str | None, *, user_only: bool = False) -> dict:
    nodes = session.tree.get("nodes", [])
    if entry_id is None:
        matches = [node for node in branch_path(session) if _is_user(node)]
        if not matches:
            raise ValueError("No user message is available")
        return matches[-1]
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise ValueError("A message entry ID is required")
    matches = [node for node in nodes if node["id"].startswith(entry_id.strip())]
    if len(matches) != 1:
        raise ValueError("Message entry ID was not found or is ambiguous")
    node = matches[0]
    if user_only and not _is_user(node):
        raise ValueError("Select a user message")
    return node


def checkout_entry(session: SessionData, entry_id: str, *, preserve_task: bool = False) -> str:
    """User nodes return a draft and continue BEFORE that message, like Pi."""
    with session._tree_lock:
        record_messages(session, session.messages)
        node = resolve_entry(session, entry_id)
        if node["message"].get("role") not in {"user", "assistant"} or node["message"].get("internal"):
            raise ValueError("Select a user message or a complete assistant response")
        path = branch_path(session, node["id"])
        draft = ""
        if _is_user(node):
            draft = node["message"].get("displayContent", node["message"].get("content", ""))
            path = path[:-1]
        pending = set()
        for item in path:
            message = item["message"]
            if message.get("role") == "assistant_tool_call":
                pending.add(message["toolUseId"])
            elif message.get("role") == "tool_result":
                pending.discard(message["toolUseId"])
        if pending:
            raise ValueError("Cannot continue inside an unfinished tool batch")
        system = [
            copy.deepcopy(message)
            for message in session.messages
            if message.get("role") == "system" and not message.get("_compaction_marker")
        ]
        session.tree["leaf_id"] = path[-1]["id"] if path else None
        # Signed reasoning may belong to a previously selected provider. Keep
        # it in the archive, but replay only portable messages on checkout.
        session.messages = [
            *system,
            *(copy.deepcopy(item["message"]) for item in path if item["message"].get("role") != "assistant_thinking"),
        ]
        session.transcript_entries = []
        # Verification describes shared on-disk files, not a historical branch.
        if session.verification_state:
            session.verification_state = {**session.verification_state, "last_report": None}
        # Historical text cannot grant a different branch the current goal's
        # identity or completion evidence. Regeneration explicitly keeps the
        # selected task and will recheck it before accepting a new answer.
        if not preserve_task and session.task_control.get("task"):
            session.task_control.setdefault("task_history", []).append(session.task_control.pop("task"))
        return draft


def prepare_regeneration(session: SessionData, entry_id: str | None = None) -> dict:
    with session._tree_lock:
        record_messages(session, session.messages)
        node = resolve_entry(session, entry_id, user_only=True)
        prompt = copy.deepcopy(node["message"])
        task = session.task_control.get("task")
        same_task = bool(task and prompt.get("source", {}).get("id") in task["inputs"])
        checkout_entry(session, node["id"], preserve_task=same_task)
        prompt.pop("entryId", None)
        return prompt


def copy_session(session: SessionData, *, entry_id: str | None = None, clone: bool = False):
    from pepsicode.core.session import create_new_session

    with session._tree_lock:
        record_messages(session, session.messages)
        child = create_new_session(session.workspace)
        for name in (
            "messages",
            "history",
            "permissions_summary",
            "skills",
            "mcp_servers",
            "model_settings",
            "profile",
            "verification_state",
            "execution_state",
            "task_control",
            "loaded_tools",
        ):
            setattr(child, name, copy.deepcopy(getattr(session, name)))
        child.access_mode, child.permission_mode, child.plan_file_path = (
            session.access_mode,
            session.permission_mode,
            session.plan_file_path,
        )
        child.parent_session_id = session.session_id
        child.parent_entry_id = session.tree.get("leaf_id")
        child.tree = copy.deepcopy(session.tree)
        child.task_control = {"todos": copy.deepcopy(session.task_control.get("todos", []))}
        draft = ""
        if not clone:
            node = resolve_entry(session, entry_id, user_only=True)
            child.parent_entry_id = node["id"]
            draft = checkout_entry(child, node["id"])
        child.tree["nodes"] = copy.deepcopy(branch_path(child))
        child.transcript_entries = []
        return child, draft


def tree_view(session: SessionData) -> dict:
    with session._tree_lock:
        record_messages(session, session.messages)
        active = {node["id"] for node in branch_path(session)}
        depths, rows = {}, []
        for node in session.tree["nodes"]:
            depths[node["id"]] = depths.get(node["parent_id"], -1) + 1
            message = node["message"]
            if message.get("role") not in {"user", "assistant"} or message.get("internal"):
                continue
            rows.append(
                {
                    "id": node["id"],
                    "parent_id": node["parent_id"],
                    "role": message["role"],
                    "preview": str(message.get("displayContent", message.get("content", "")))[:180],
                    "depth": depths[node["id"]],
                    "active": node["id"] in active,
                }
            )
        return {"nodes": rows, "leaf_id": session.tree["leaf_id"], "parent_session_id": session.parent_session_id}


def format_tree(session: SessionData, *, users_only: bool = False) -> str:
    view = tree_view(session)
    lines = ["Conversation tree — shared working directory; files are not restored."]
    for node in view["nodes"]:
        if users_only and node["role"] != "user":
            continue
        marker = "*" if node["id"] == view["leaf_id"] else "+" if node["active"] else " "
        lines.append(
            f"{marker} {node['id'][:8]} {'  ' * min(node['depth'], 6)}{node['role']}: {node['preview'].replace(chr(10), ' ')}"
        )
    lines.append(
        "/tree <id>: continue here; /fork <user-id>: new session; /clone: copy active branch; /retry: regenerate"
    )
    return "\n".join(lines)


def rebuild_transcript(session: SessionData) -> None:
    entries = []
    for message in session.messages:
        role = message.get("role")
        if message.get("internal") or role in {"system", "assistant_thinking"}:
            continue
        if role in {"user", "assistant", "assistant_progress"}:
            entries.append(
                {
                    "id": len(entries) + 1,
                    "kind": "progress" if role == "assistant_progress" else role,
                    "body": message.get("displayContent", message.get("content", "")),
                }
            )
        elif role in {"assistant_tool_call", "tool_result"}:
            entries.append(
                {
                    "id": len(entries) + 1,
                    "kind": "tool",
                    "toolName": message.get("toolName"),
                    "body": json.dumps(message.get("input"), ensure_ascii=False)
                    if role == "assistant_tool_call"
                    else message.get("content", ""),
                    "status": "error" if message.get("isError") else "success",
                }
            )
    session.transcript_entries = entries
