import sys

from pepsicode import python_runtime
from pepsicode.core.verification_discovery import discover_checks


def test_source_runtime_uses_current_interpreter(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert python_runtime.project_python(str(tmp_path)) == sys.executable


def test_frozen_runtime_prefers_project_virtual_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    executable = tmp_path / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.touch()
    assert python_runtime.project_python(str(tmp_path)) == str(executable)


def test_frozen_runtime_never_uses_server_executable_as_python(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(python_runtime.shutil, "which", lambda name: None)
    assert python_runtime.project_python(str(tmp_path)) == "python"
    (tmp_path / "test_example.py").write_text("def test_ok(): pass", encoding="utf-8")
    checks = discover_checks(str(tmp_path))
    assert checks[0].argv[:3] == ["python", "-m", "pytest"]
    assert checks[0].argv[0] != sys.executable
