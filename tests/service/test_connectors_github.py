"""GitHub/CI connector: dedup, cursors, source allowlist, head-SHA binding."""

from __future__ import annotations

import pytest

from pepsicode.service.connectors import github as github_module
from pepsicode.service.connectors.base import ConnectorError, ConnectorManager, ExternalEvent, filter_allowed
from pepsicode.service.connectors.github import GitHubConnector


@pytest.fixture()
def repo_api(tmp_path, monkeypatch):
    """Fake GitHub REST responses served from a module-level dict."""
    responses: dict[str, object] = {}

    def fake_get(url, *, token):
        path = url.split("api.github.com", 1)[1].split("?")[0]
        if path not in responses:
            raise ConnectorError(f"unexpected path {path}")
        return 200, responses[path]

    monkeypatch.setattr(github_module, "http_get_json", fake_get)
    return responses


def _seed_pr(responses, *, pr_number=7, head_sha="sha-new", reviews=None, checks=None):
    responses[f"/repos/owner/repo/pulls/{pr_number}/reviews"] = reviews or []
    responses[f"/repos/owner/repo/commits/{head_sha}/check-runs"] = {"check_runs": checks or []}
    responses["/repos/owner/repo/pulls"] = [
        {
            "number": pr_number,
            "title": "Add feature",
            "updated_at": "2026-11-12T00:00:00Z",
            "head": {"sha": head_sha},
        }
    ]


def test_review_and_check_events_carry_head_sha(repo_api):
    _seed_pr(
        repo_api,
        reviews=[
            {"id": 9000, "state": "APPROVED", "commit_id": "sha-old"},
            {"id": 9001, "state": "CHANGES_REQUESTED", "body": "please fix", "commit_id": "sha-new"},
        ],
        checks=[{"id": 42, "name": "ci", "conclusion": "success", "run_id": 7, "html_url": "u"}],
    )
    connector = GitHubConnector(repository="owner/repo", token="t")
    events = connector.poll()
    assert len(events) == 2
    kinds = {event.kind for event in events}
    assert kinds == {"github.review", "github.check"}
    check = next(event for event in events if event.kind == "github.check")
    assert check.head_sha == "sha-new"
    assert check.payload["pr"] == 7 and check.payload["run_id"] == 7


def test_duplicate_polls_are_deduped_by_external_id(repo_api, tmp_path):
    _seed_pr(repo_api, reviews=[{"id": 1, "state": "APPROVED", "body": ""}])
    connector = GitHubConnector(repository="owner/repo", token="t")
    first = connector.poll()
    assert len(first) == 1
    # The connector checks reviews and CI even when the PR timestamp is stable.
    # Only the manager advances the cursor, after successful host admission.
    assert connector.poll() == first
    manager = ConnectorManager(tmp_path, [GitHubConnector(repository="owner/repo", token="t")])
    assert len(manager.poll_once()) == 1
    assert manager.poll_once() == []


def test_failed_admission_does_not_advance_connector_cursor(repo_api, tmp_path):
    _seed_pr(repo_api, checks=[{"id": 42, "name": "ci", "conclusion": "failure"}])
    manager = ConnectorManager(tmp_path, [GitHubConnector(repository="owner/repo", token="t")])

    def reject(events):
        assert len(events) == 1
        raise OSError("storage unavailable")

    with pytest.raises(OSError, match="storage unavailable"):
        manager.poll_once(commit=reject)
    accepted = []
    manager.poll_once(commit=accepted.extend)
    assert len(accepted) == 1
    assert manager.poll_once() == []


def test_new_head_sha_produces_a_new_check_event(repo_api):
    _seed_pr(repo_api, checks=[{"id": 42, "name": "ci", "conclusion": "success", "run_id": 7, "html_url": "u"}])
    connector = GitHubConnector(repository="owner/repo", token="t")
    assert [event.kind for event in connector.poll()] == ["github.check"]
    # A push moves the head SHA: the old success must never cover the new commit.
    _seed_pr(
        repo_api,
        head_sha="sha-2",
        checks=[{"id": 43, "name": "ci", "conclusion": "failure", "run_id": 8, "html_url": "u"}],
    )
    repo_api["/repos/owner/repo/pulls"][0]["updated_at"] = "2026-11-12T02:00:00Z"
    events = connector.poll()
    assert [event.payload["head_sha"] for event in events] == ["sha-2"]
    assert events[0].payload["conclusion"] == "failure"


def test_manager_persists_dedup_state_across_restart(repo_api, tmp_path):
    _seed_pr(repo_api, reviews=[{"id": 5, "state": "APPROVED", "body": ""}])
    connector = GitHubConnector(repository="owner/repo", token="t")
    manager = ConnectorManager(tmp_path, [connector])
    assert len(manager.poll_once()) == 1
    # A "restarted" manager must not re-deliver the same fact.
    restarted = ConnectorManager(tmp_path, [GitHubConnector(repository="owner/repo", token="t")])
    assert restarted.poll_once() == []


def test_manager_backs_off_after_failure(tmp_path, monkeypatch):
    def failing_get(url, *, token):
        raise ConnectorError("network down")

    monkeypatch.setattr(github_module, "http_get_json", failing_get)
    connector = GitHubConnector(repository="owner/repo", token="t")
    manager = ConnectorManager(tmp_path, [connector])
    assert manager.poll_once() == []
    health = manager.health()[0]
    assert health["last_error"] and health["backoff_seconds"] >= 5
    # Not due again immediately — the backoff protects the upstream API.
    assert not manager.due(connector.name, interval=0)


def test_source_allowlist_drops_unknown_repositories():
    events = [
        ExternalEvent("1", "github.check", "owner/repo", "t", ""),
        ExternalEvent("2", "github.check", "stranger/repo", "t", ""),
    ]
    kept, dropped = filter_allowed(events, ["owner/repo"])
    assert [event.external_id for event in kept] == ["1"] and dropped == 1


def test_connector_disabled_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from pepsicode.service.connectors.github import build_github_connector

    # A missing token keeps the connector registered but disabled, so health
    # can show *why* GitHub events are not flowing.
    disabled = build_github_connector({"enabled": True, "repository": "owner/repo"})
    assert disabled is not None and disabled.health()["enabled"] is False
    assert disabled.poll() == []
    assert build_github_connector({"enabled": False}) is None
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    connector = build_github_connector({"enabled": True, "repository": "owner/repo"})
    assert connector is not None and connector.health()["enabled"] is True


def test_http_failure_maps_to_connector_error(monkeypatch):
    import urllib.error

    def raise_url_error(request, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(github_module.urllib.request, "urlopen", raise_url_error)
    with pytest.raises(ConnectorError, match="github request failed"):
        github_module.http_get_json("https://api.github.com/repos/owner/repo/pulls", token="t")
