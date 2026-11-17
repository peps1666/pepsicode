"""Authorized external delivery of integrated candidates (M4 §20.3, B16).

Default delivery is local: the integration branch and its evidence stay in the
repository. Pushing to a remote or opening a pull request requires the
explicit coordinator action (``push``/``pull_request`` flags) and
passes through the execution layer, so interactive mode gets the usual
approval prompt and service mode enforces the unattended policy.

External outcomes are never assumed: every push is reconciled against
``git ls-remote`` before it is repeated, and an unknown outcome (timeout,
cancelled, lost response) marks the delivery ``uncertain`` — no auto-retry,
no duplicate PR. A PR is created only after checking that no PR for the same
repository + head already exists.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlencode

from pepsicode.core.task_control import edit_control
from pepsicode.execution.external import authorize
from pepsicode.orchestration.git_ops import run_git
from pepsicode.orchestration.integration import _journalled, acceptance_command_digest, validate_integration
from pepsicode.tooling import ToolContext

API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 20


class DeliveryError(RuntimeError):
    """Network-level failure whose outcome is unknown (never auto-retried)."""


def http_request_json(url, *, token=None, method="GET", payload=None, timeout=REQUEST_TIMEOUT):
    """One JSON HTTP exchange; returns ``(status, payload)``.

    Kept module-level so tests can stub it instead of the network.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pepsicode-service",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise DeliveryError(f"delivery request failed: {error}") from error


def _save_delivery(session, record):
    with edit_control(session=session) as control:
        control.setdefault("orchestration", {}).setdefault("deliveries", {})[record["delivery_id"]] = dict(record)


def _existing_delivery(session, integration_id, kind):
    state = session.task_control.get("orchestration") or {}
    for record in (state.get("deliveries") or {}).values():
        if record.get("integration_id") == integration_id and record.get("kind") == kind:
            return dict(record)
    return None


def _new_record(integration, kind, target):
    return {
        "delivery_id": uuid.uuid4().hex[:12],
        "integration_id": integration["integration_id"],
        "task_id": integration["task_id"],
        "revision": integration["revision"],
        "kind": kind,
        "target": target,
        "head_sha": integration.get("head_sha", ""),
        "branch": integration.get("branch", ""),
        "status": "pending",
        "attempts": [],
        "external_refs": {},
        "created_at": time.time(),
    }


def _remote_head(context, repo, remote, branch):
    result = run_git(context, repo, "ls-remote", remote, f"refs/heads/{branch}", allowed=(0, 128))
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}":
            return sha.strip()
    return None


def _update(record, *, status=None, attempt=None, refs=None):
    if attempt is not None:
        record["attempts"].append({"at": time.time(), **attempt})
    if status is not None:
        record["status"] = status
    if refs:
        record["external_refs"].update(refs)
    record["updated_at"] = time.time()
    return record


def deliver_integration(
    session,
    scope,
    integration_id,
    *,
    push=False,
    pull_request=False,
    repository=None,
    base_branch=None,
    remote="origin",
    token_env="GITHUB_TOKEN",
    api_base=API_BASE,
    runtime=None,
):
    """Deliver an accepted integration locally, to a remote and/or as a PR."""
    integration = validate_integration(session, scope, integration_id)
    acceptance = integration.get("acceptance") or {}
    if integration["state"] != "accepted" or not acceptance.get("valid"):
        raise ValueError("Only an accepted integration can be delivered")
    if runtime is not None and acceptance["command_digest"] != acceptance_command_digest(
        runtime.get("verificationCommands") or []
    ):
        raise ValueError("Integration verification commands changed; run integration acceptance again")

    repo = Path(session.workspace).resolve()
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)

    # Local delivery is the default outcome: the integration branch already
    # exists in the repository; the record binds it to its evidence.
    record = _existing_delivery(session, integration["integration_id"], "local") or _new_record(
        integration, "local", {"workspace": str(repo)}
    )
    if record["status"] == "pending":
        _update(record, status="local", attempt={"outcome": "local_branch_available"})
    _save_delivery(session, record)

    if push:
        record = _deliver_push(session, scope, context, repo, integration, remote)
    if pull_request:
        record = _deliver_pull_request(
            session,
            scope,
            context,
            repo,
            integration,
            repository=repository,
            base_branch=base_branch,
            token_env=token_env,
            api_base=api_base,
        )
    return record


def _deliver_push(session, scope, context, repo, integration, remote):
    branch = integration["branch"]
    head = integration["head_sha"]
    record = _existing_delivery(session, integration["integration_id"], "push") or _new_record(
        integration, "push", {"remote": remote, "branch": branch}
    )
    _save_delivery(session, record)
    _update(record, attempt={"outcome": "reconcile_remote"})
    try:
        current = _remote_head(context, repo, remote, branch)
    except RuntimeError as error:
        # Cannot see the remote: if we never pushed, this is a failure; the
        # outcome of a previous attempt stays uncertain — never auto-retry.
        if record["status"] == "uncertain":
            _update(record, attempt={"outcome": "reconcile_failed", "detail": str(error)[:400]})
            _save_delivery(session, record)
            return record
        _update(record, status="failed", attempt={"outcome": "remote_unreachable", "detail": str(error)[:400]})
        _save_delivery(session, record)
        raise ValueError(f"Remote {remote} is unreachable: {error}") from error
    if current == head:
        _update(record, status="delivered", attempt={"outcome": "already_on_remote"}, refs={"remote": remote})
        _save_delivery(session, record)
        return record
    if record["status"] == "uncertain":
        # The remote does not show our head; the previous push may still have
        # landed elsewhere or been lost. Requiring human reconciliation keeps
        # us from ever pushing twice on unknown outcomes (B16).
        _update(record, attempt={"outcome": "reconcile_inconclusive"})
        _save_delivery(session, record)
        return record

    _update(record, status="uncertain", attempt={"outcome": "pushing", "remote": remote})
    _save_delivery(session, record)
    try:
        _journalled(
            scope,
            context,
            f"m4-deliver:{record['delivery_id']}:push",
            "m4_push_candidate",
            lambda: run_git(context, repo, "push", remote, f"{head}:refs/heads/{branch}"),
        )
    except Exception as error:
        # A push whose process completed with a nonzero exit is a known
        # rejection; a lost process (started/unknown) leaves the outcome open.
        unknown = getattr(context, "effect_state", "none") in {"started", "unknown"}
        _update(
            record,
            status="uncertain" if unknown else "failed",
            attempt={"outcome": "push_unknown" if unknown else "push_rejected", "detail": str(error)[:400]},
        )
        _save_delivery(session, record)
        if unknown:
            return record
        raise ValueError(f"Push was rejected: {error}") from error
    verified = _remote_head(context, repo, remote, branch)
    if verified == head:
        _update(record, status="delivered", attempt={"outcome": "push_verified"}, refs={"remote": remote})
    else:
        _update(record, status="uncertain", attempt={"outcome": "push_unverified"})
    _save_delivery(session, record)
    return record


def _deliver_pull_request(
    session,
    scope,
    context,
    repo,
    integration,
    *,
    repository,
    base_branch,
    token_env,
    api_base,
):
    if not repository or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, re.ASCII):
        raise ValueError("PR delivery requires repository='owner/name'")
    if not base_branch:
        raise ValueError("PR delivery requires the target base branch")
    token = os.environ.get(token_env) or ""
    if not token:
        raise ValueError(f"PR delivery requires a token in the {token_env} environment variable")
    branch = integration["branch"]
    owner = repository.split("/", 1)[0]
    target = {"repository": repository, "base": base_branch, "head": f"{owner}:{branch}"}
    record = _existing_delivery(session, integration["integration_id"], "pull_request")
    if record and record["target"] != target:
        raise ValueError("This integration already has a different PR delivery target")
    record = record or _new_record(integration, "pull_request", target)
    _save_delivery(session, record)
    _update(record, attempt={"outcome": "checking_existing_pr"})
    url = f"{api_base}/repos/{repository}/pulls"
    query = urlencode({"head": target["head"], "base": base_branch, "state": "all"})
    try:
        status, pulls = http_request_json(f"{url}?{query}", token=token)
    except DeliveryError as error:
        _update(record, status="uncertain", attempt={"outcome": "pr_check_failed", "detail": str(error)[:400]})
        _save_delivery(session, record)
        return record
    if status == 200 and isinstance(pulls, list):
        for pull in pulls:
            head_sha = ((pull.get("head") or {}).get("sha")) or ""
            if head_sha == integration["head_sha"]:
                _update(
                    record,
                    status="delivered",
                    attempt={"outcome": "pr_already_exists"},
                    refs={"pr_url": pull.get("html_url", ""), "pr_number": pull.get("number")},
                )
                _save_delivery(session, record)
                return record
    if status != 200 or not isinstance(pulls, list) or record["status"] in {"uncertain", "delivered"}:
        _update(record, status="uncertain", attempt={"outcome": "pr_reconcile_inconclusive"})
        _save_delivery(session, record)
        return record

    acceptance = integration.get("acceptance") or {}
    payload = {
        "title": f"[pepsicode] {integration['task_id']} integration {integration['integration_id']}"[:200],
        "head": f"{owner}:{branch}",
        "base": base_branch,
        "body": (
            f"Integration of candidates {', '.join(integration['candidates'])} at "
            f"`{integration['head_sha'][:12]}` (base `{integration['base_sha'][:12]}`, "
            f"task revision {integration['revision']}).\n\n"
            f"Acceptance commands: {len(acceptance.get('commands') or [])}, "
            f"digest `{acceptance.get('command_digest', '')}`, "
            f"bound head `{acceptance.get('head_sha', '')[:12]}`.\n\n"
            "Delivered by PepsiCode M4 delivery loop; verification evidence is attached "
            "to the orchestration record."
        )[:6000],
    }
    authorize(context, "Create GitHub pull request", f"{url}\n{target['head']} → {base_branch}", mutating=True)
    _update(record, status="uncertain", attempt={"outcome": "pr_creating"})
    _save_delivery(session, record)
    scope.check_cancelled()
    if scope.policy_checkpoint:
        scope.policy_checkpoint()
    try:
        status, created = http_request_json(url, token=token, method="POST", payload=payload)
    except DeliveryError as error:
        _update(record, status="uncertain", attempt={"outcome": "pr_create_unknown", "detail": str(error)[:400]})
        _save_delivery(session, record)
        return record
    if status in (201, 200):
        _update(
            record,
            status="delivered",
            attempt={"outcome": "pr_created"},
            refs={"pr_url": created.get("html_url", ""), "pr_number": created.get("number")},
        )
    elif status == 422 or status >= 500:
        # A PR for this head already exists (race) — reconcile instead of resending.
        _update(record, status="uncertain", attempt={"outcome": "pr_maybe_exists", "detail": f"HTTP {status}"})
    else:
        _update(record, status="failed", attempt={"outcome": "pr_rejected", "detail": f"HTTP {status}"})
    _save_delivery(session, record)
    return record
