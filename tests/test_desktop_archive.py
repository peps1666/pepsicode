import zipfile

import pytest

from scripts.archive_desktop import create_archive


def test_archive_roundtrips_binary_and_unicode_paths(tmp_path):
    stage = tmp_path / "application"
    (stage / "resources/backend").mkdir(parents=True)
    payloads = {
        "Pepsicode.exe": bytes(range(256)) * 100,
        "PepsicodeDesktop.exe": b"desktop",
        "resources/app.asar": b"renderer",
        "resources/backend/pepsicode-server.exe": b"backend",
        "使用说明.txt": "完整解压后启动".encode(),
    }
    for name, content in payloads.items():
        (stage / name).write_bytes(content)
    target = tmp_path / "release.zip"
    create_archive(stage, target)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert {name: archive.read(name) for name in archive.namelist()} == payloads


def test_incomplete_staging_does_not_replace_existing_zip(tmp_path):
    stage = tmp_path / "application"
    stage.mkdir()
    target = tmp_path / "release.zip"
    target.write_bytes(b"existing release")
    with pytest.raises(FileNotFoundError):
        create_archive(stage, target)
    assert target.read_bytes() == b"existing release"
