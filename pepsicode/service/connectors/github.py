"""GitHub pull-request review and CI check-run connector.

Read-only REST polling with a bearer token taken from the environment (never
persisted or logged).  Each poll fetches recently updated pull requests and
emits:

- ``github.review`` — a new review state on a tracked PR.
- ``github.check`` — a check-run conclusion bound to the exact head SHA, so a
  success on an old commit can never stand in for the current candidate.

External IDs are stable (review/check-run id + repository), giving idempotent
delivery across restarts and reconnects.  Unknown repositories are dropped by
:func:`pepsicode.service.connectors.base.filter_allowed`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from pepsicode.service.connectors.base import ConnectorError, ExternalEvent

API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 20
MAX_PRS_PER_POLL = 10


def http_get_json(url: str, *, token: str | None) -> tuple[int, Any]:
    """Fetch one JSON URL; returns ``(status, payload)``.

    Kept module-level so tests can stub it instead of the network.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pepsicode-service",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ConnectorError(f"github request failed: {error}") from error


class GitHubConnector:
    name = "github"

    def __init__(
        self,
        *,
        repository: str,
        token: str | None,
        api_base: str = API_BASE,
        max_prs: int = MAX_PRS_PER_POLL,
    ) -> None:
        if not repository or "/" not in repository:
            raise ValueError("repository must be 'owner/name'")
        self.repository = repository
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._max_prs = max_prs

    def health(self) -> dict:
        return {"name": self.name, "enabled": self._token is not None, "repository": self.repository}

    def _get(self, path: str) -> tuple[int, Any]:
        return http_get_json(f"{self._api_base}{path}", token=self._token)

    def poll(self) -> list[ExternalEvent]:
        if self._token is None:
            return []
        status, pulls = self._get(
            f"/repos/{self.repository}/pulls?state=all&sort=updated&direction=desc&per_page={self._max_prs}"
        )
        if status != 200:
            raise ConnectorError(f"github pull list returned HTTP {status}")
        events: list[ExternalEvent] = []
        for pull in pulls[: self._max_prs]:
            if not isinstance(pull, dict):
                continue
            number = pull.get("number")
            updated_at = str(pull.get("updated_at", ""))
            if not isinstance(number, int):
                continue
            events.extend(
                self._poll_pr(number, str(pull.get("title", f"PR #{number}")), pull.get("head", {}), updated_at)
            )
        return events

    def _poll_pr(self, number: int, title: str, head: Any, updated_at: str) -> list[ExternalEvent]:
        events: list[ExternalEvent] = []
        head_sha = head.get("sha") if isinstance(head, dict) else None
        status, reviews = self._get(f"/repos/{self.repository}/pulls/{number}/reviews?per_page=20")
        if status == 200 and isinstance(reviews, list):
            for review in reviews:
                if not isinstance(review, dict):
                    continue
                review_id = review.get("id")
                state = str(review.get("state", ""))
                if review_id is None or not state or review.get("commit_id", head_sha) != head_sha:
                    continue
                events.append(
                    ExternalEvent(
                        external_id=f"{self.repository}#review-{review_id}",
                        kind="github.review",
                        repository=self.repository,
                        title=f"PR #{number} {title}: review {state}",
                        body=str(review.get("body", "") or "")[:2000],
                        head_sha=str(head_sha) if head_sha else None,
                        payload={
                            "pr": number,
                            "review_state": state,
                            "head_sha": head_sha,
                            "pr_updated_at": updated_at,
                        },
                    )
                )
        if head_sha:
            status, checks = self._get(f"/repos/{self.repository}/commits/{head_sha}/check-runs?per_page=50")
            if status == 200 and isinstance(checks, dict):
                for check in checks.get("check_runs", []) or []:
                    if not isinstance(check, dict):
                        continue
                    check_id = check.get("id")
                    conclusion = check.get("conclusion")
                    if check_id is None or conclusion is None:
                        continue
                    events.append(
                        ExternalEvent(
                            external_id=f"{self.repository}#check-{check_id}",
                            kind="github.check",
                            repository=self.repository,
                            title=f"PR #{number} check {check.get('name', 'unknown')}: {conclusion}",
                            body=f"head {head_sha} run {check.get('html_url', '')}",
                            head_sha=str(head_sha),
                            payload={
                                "pr": number,
                                "check_name": check.get("name"),
                                "conclusion": conclusion,
                                "run_id": check.get("run_id"),
                                "head_sha": head_sha,
                                "pr_updated_at": updated_at,
                            },
                        )
                    )
        return events


def build_github_connector(settings: dict) -> GitHubConnector | None:
    """Create the connector from validated settings; ``None`` when disabled.

    The token is read from the environment at construction time; a missing
    token disables the connector (reported in health) instead of failing the
    service start.
    """
    if not isinstance(settings, dict) or not settings.get("enabled"):
        return None
    import os

    token_env = settings.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(str(token_env))
    return GitHubConnector(repository=str(settings.get("repository", "")), token=token)
