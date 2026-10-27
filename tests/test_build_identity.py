import json
import subprocess
import sys
from pathlib import Path

import pytest

from pepsicode import build_info
from scripts.check_project import check


def test_versions_agree_and_entry_documents_have_local_targets():
    check(Path(__file__).resolve().parents[1])


def test_frozen_build_never_infers_identity_from_user_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(build_info, "__file__", str(tmp_path / "build_info.py"))
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: pytest.fail("must use embedded identity"))
    assert build_info.build_identity()["channel"] == "unknown"
    manifest = {
        "schema_version": 1,
        "version": build_info.VERSION,
        "git_sha": "a" * 40,
        "dirty": False,
        "channel": "development",
    }
    path = tmp_path / "build-info.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert build_info.build_identity() == manifest
    path.write_text(json.dumps({**manifest, "version": "0.0.0"}), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime version"):
        build_info.build_identity()


def test_manifest_records_unknown_git_instead_of_claiming_clean_release(tmp_path, monkeypatch):
    def unavailable(*_a, **_k):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(subprocess, "check_output", unavailable)
    path = tmp_path / "output/build-info.json"
    result = build_info.write_manifest(tmp_path, path)
    assert result["git_sha"] is None and result["dirty"] is None
    assert result["channel"] == "development"
    assert result["built_at"]
    assert json.loads(path.read_text(encoding="utf-8")) == result


def test_installed_package_cannot_borrow_an_enclosing_project_commit(tmp_path, monkeypatch):
    install = tmp_path / "site-packages"
    install.mkdir()
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: str(tmp_path))
    result = build_info.source_identity(install)
    assert result["git_sha"] is None and result["dirty"] is None


def test_zip_acceptance_rejects_mixed_desktop_and_backend_builds(tmp_path):
    from scripts.smoke_desktop_zip import verify_build_identity

    resources = tmp_path / "resources"
    resources.mkdir()
    manifest = {"schema_version": 1, "version": build_info.VERSION, "git_sha": "a" * 40, "dirty": False}
    (resources / "build-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_build_identity(tmp_path, {"build": manifest}) == manifest
    with pytest.raises(ValueError, match="identities differ"):
        verify_build_identity(tmp_path, {"build": {**manifest, "git_sha": "b" * 40}})
