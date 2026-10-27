"""Auditable file roles; mentioning an input is not a delivery requirement."""

from __future__ import annotations

import re

ROLES = frozenset({"input", "output", "delete_target", "protected", "reference"})
_PATH = re.compile(r"(?<![\w/])([\w./-]+\.(?:py|tsx?|jsx?|md|txt|csv|json|ya?ml|html|css|png|svg|pdf|docx))\b")


def file_mentions(text, input_id=None):
    mentions = {}
    for match in _PATH.finditer(text):
        path = match[1]
        if path.startswith(("/", "../")) or ".." in path.split("/"):
            continue
        before = re.split(r"[;；。\n]", text[: match.start()])[-1].lower()
        # Restrict positive inference to clear local relationships. Ambiguous
        # mentions remain reviewable references instead of hard requirements.
        role = "reference"
        if re.search(r"(?:do not|don't|不要|禁止)\s*(?:create|generate|创建|生成)\s*$", before):
            role = "reference"
        elif re.search(r"(?:remove|delete|删除|移除)\s*(?:the\s+)?$", before):
            role = "delete_target"
        elif re.search(r"(?:do not (?:edit|modify)|preserve|keep unchanged|不要修改|禁止修改|保持不变)\s*$", before):
            role = "protected"
        elif re.search(r"\bfrom\s*$|(?:从|读取|输入)\s*$", before):
            role = "input"
        elif re.search(r"(?:\bto|rename\s+\S+\s+to|迁移到|重命名为)\s*$", before) or re.search(
            r"(?:implement|create|write|generate|生成|创建|编写|实现)\s*(?:the\s+)?$", before
        ):
            role = "output"
        candidate = {
            "path": path,
            "role": role,
            "source": "inferred",
            "input_id": input_id,
            "confirmed": role != "reference",
            "basis": text[max(0, match.start() - 64) : match.end() + 32],
        }
        # An explicit deletion later in the same request supersedes a migration
        # source mention; a bare subsequent reference cannot override a role.
        if path not in mentions or role != "reference":
            mentions[path] = candidate
    return list(mentions.values())


def artifact_requirements(text):
    return [item for item in file_mentions(text) if item["role"] == "output" and item["confirmed"]]


def correct_file_roles(task, roles, *, reason, instruction_id=None):
    from pepsicode.core.task_control import is_continuation

    mentions = {item["path"]: item for item in task.get("file_mentions", [])}
    artifacts = {item["path"]: item for item in task.get("required_artifacts", [])}
    for update in roles:
        path, role = update["path"], update["role"]
        previous = artifacts.get(path) or mentions.get(path)
        if previous is None:
            raise ValueError("Correct only an existing file mention; define acceptance for additional files")
        explicit = artifacts.get(path, {}).get("source") in {"caller", "user"}
        if explicit and (
            not task["instructions"]
            or instruction_id != task.get("last_delivered_input_id", task["inputs"][-1])
            or task.get("pending_intents")
            or task.get("criteria_revision") == task["revision"]
            or is_continuation(task["instructions"][-1])
        ):
            raise PermissionError("Explicit file constraints require a new delivered human instruction")
        task.setdefault("file_role_changes", []).append(
            {
                "before": previous,
                "role": role,
                "reason": reason,
                "input_id": instruction_id,
                "revision": task["revision"],
            }
        )
        updated = {**previous, "path": path, "role": role, "confirmed": True, "basis": reason}
        mentions[path] = updated
        artifacts.pop(path, None)
        if role == "output":
            artifacts[path] = updated
        if explicit and role != "output":
            required = task.get("requirements", {}).get("artifacts", [])
            if path in required:
                required.remove(path)
        sources = task.get("criterion_sources", {})
        task["criteria"] = [
            c
            for c in task["criteria"]
            if not (c.get("path") == path and sources.get(c["id"], {}).get("source") == "inferred")
        ]
    task["file_mentions"] = list(mentions.values())
    task["required_artifacts"] = list(artifacts.values())
    task["revision"] += 1
    task["admitted_revision"] = task["revision"]
    task.update(evidence=[], acceptance=None)


def migrate_file_contract(task):
    """Downgrade only legacy inference; never erase explicit caller constraints."""
    if task.get("file_contract_version", 1) >= 2:
        return False
    inferred = [item for item in task.get("required_artifacts", []) if item.get("source") == "inferred"]
    task["file_mentions"] = [
        {**item, "role": "reference", "confirmed": False, "basis": "legacy inferred existence requires review"}
        for item in inferred
    ]
    task["required_artifacts"] = [
        item for item in task.get("required_artifacts", []) if item.get("source") != "inferred"
    ]
    task["file_contract_version"] = 2
    if inferred:
        paths = {item["path"] for item in inferred}
        sources = task.get("criterion_sources", {})
        task["criteria"] = [
            c
            for c in task["criteria"]
            if not (c.get("path") in paths and sources.get(c["id"], {}).get("source") == "inferred")
        ]
        task["legacy_evidence"] = task.get("evidence", [])
        task.update(evidence=[], acceptance=None, assurance="needs_recheck")
        if task["status"] == "complete":
            task["status"] = "incomplete"
    return bool(inferred)
