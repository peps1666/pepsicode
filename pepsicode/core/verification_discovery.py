"""Evidence-based check discovery; explicit argv commands always take priority."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pepsicode.python_runtime import project_python
from pepsicode.tools.run_command import COMMAND_TIMEOUT

DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "__pycache__",
    ".tox",
    ".pepsi-code",
}
PACKAGE_LOCKS = {
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
    "pnpm": ("pnpm-lock.yaml", "pnpm-workspace.yaml"),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lock", "bun.lockb"),
}


@dataclass(frozen=True)
class Check:
    name: str
    argv: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    timeout: int | None = None
    no_tests_exit_codes: tuple[int, ...] = ()


def command_timeout(value, name="verificationTimeout") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= COMMAND_TIMEOUT:
        raise ValueError(f"{name} must be an integer from 1 to {COMMAND_TIMEOUT}")
    return value


def _configured_checks(root: Path, configured) -> list[Check]:
    if not isinstance(configured, list):
        raise ValueError("verificationCommands must be an array of command objects")
    checks = []
    for item in configured:
        if not isinstance(item, dict):
            raise ValueError("Each verification command must be an object")
        argv = item.get("command")
        if (
            not isinstance(argv, list)
            or not argv
            or not isinstance(argv[0], str)
            or not argv[0]
            or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
        ):
            raise ValueError("Verification command must be a non-empty argv array, not a shell string")
        relative = item.get("cwd", ".")
        if not isinstance(relative, str):
            raise ValueError("Verification cwd must be a string")
        path = (root / relative).resolve()
        path.relative_to(root)
        if not path.is_dir():
            raise ValueError(f"Verification directory does not exist: {relative}")
        environment = item.get("env", {})
        if not isinstance(environment, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and "\0" not in k + v and "=" not in k
            for k, v in environment.items()
        ):
            raise ValueError("Verification env must map environment names to strings")
        timeout = command_timeout(item["timeout"], "command timeout") if "timeout" in item else None
        codes = item.get("noTestsExitCodes", [])
        if not isinstance(codes, list) or not all(
            isinstance(code, int) and not isinstance(code, bool) for code in codes
        ):
            raise ValueError("noTestsExitCodes must be an array of integers")
        checks.append(
            Check(str(item.get("name", " ".join(argv))), argv, str(path), dict(environment), timeout, tuple(codes))
        )
    return checks


def _read_manifest(path: Path, root: Path) -> dict:
    path.resolve().relative_to(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("manifest must be an object")
        return data
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read verification scripts from {path}: {error}") from error


def _package_manager(package: Path, root: Path, manifests: dict, override) -> str:
    if override is not None:
        if override not in PACKAGE_LOCKS:
            raise ValueError("verificationDiscovery.packageManager must be npm, pnpm, yarn or bun")
        return override
    current = package
    while True:
        declaration = manifests.get(current, {}).get("packageManager")
        if declaration is not None:
            if not isinstance(declaration, str) or declaration.split("@", 1)[0] not in PACKAGE_LOCKS:
                raise ValueError(f"Unsupported packageManager in {current}; configure verificationCommands explicitly")
            return declaration.split("@", 1)[0]
        found = [
            manager for manager, names in PACKAGE_LOCKS.items() if any((current / name).is_file() for name in names)
        ]
        if len(found) > 1:
            raise ValueError(
                f"Conflicting package manager lockfiles in {current}; set verificationDiscovery.packageManager"
            )
        if found:
            return found[0]
        if current == root:
            return "npm"
        current = current.parent


def discover_checks(cwd: str, configured=None, discovery=None) -> list[Check]:
    root = Path(cwd).resolve()
    if configured is not None:
        return _configured_checks(root, configured)
    options = {} if discovery is None else discovery
    if not isinstance(options, dict):
        raise ValueError("verificationDiscovery must be an object")
    depth = options.get("maxDepth", 6)
    limit = options.get("maxDirectories", 2000)
    for name, value, minimum in (("maxDepth", depth, 0), ("maxDirectories", limit, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"verificationDiscovery.{name} must be an integer >= {minimum}")
    excludes = options.get("exclude", [])
    scripts = options.get("scripts", ["typecheck", "lint", "test", "build"])
    for name, values in (("exclude", excludes), ("scripts", scripts)):
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"verificationDiscovery.{name} must be an array of non-empty strings")
    excluded = DEFAULT_EXCLUDES | set(excludes)
    manifests = {}
    python_projects = {root}
    python_tests = []
    pytest_projects = set()
    for visited, (parent, directories, files) in enumerate(os.walk(root), start=1):
        if visited > limit:
            raise ValueError(
                "Verification discovery directory limit reached; narrow discovery or configure verificationCommands"
            )
        path = Path(parent)
        relative = path.relative_to(root)
        directories[:] = (
            sorted(
                name
                for name in directories
                if name not in excluded
                and (path / name).relative_to(root).as_posix() not in excluded
                and not (path / name).is_symlink()
            )
            if len(relative.parts) < depth
            else []
        )
        names = set(files)
        if "package.json" in names:
            manifests[path] = _read_manifest(path / "package.json", root)
        if names.intersection({"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini"}):
            python_projects.add(path)
        if "pytest.ini" in names:
            pytest_projects.add(path)
        if "pyproject.toml" in names:
            manifest = path / "pyproject.toml"
            manifest.resolve().relative_to(root)
            try:
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError(f"Cannot read {manifest}: {error}") from error
            if "pytest" in data.get("tool", {}):
                pytest_projects.add(path)
        python_tests.extend(
            path / name
            for name in files
            if name.endswith(".py")
            and (name.startswith("test_") or name.endswith("_test.py"))
            and not (path / name).is_symlink()
        )
    for test in python_tests:
        owners = [project for project in python_projects if test.is_relative_to(project)]
        pytest_projects.add(max(owners, key=lambda project: len(project.parts)))
    checks = [
        Check(
            "Python tests" if package == root else f"{package.relative_to(root).as_posix()}: Python tests",
            [project_python(str(package)), "-m", "pytest", "-q"],
            str(package),
            no_tests_exit_codes=(5,),
        )
        for package in sorted(pytest_projects)
    ]
    for package, data in sorted(manifests.items()):
        commands = data.get("scripts", {})
        if not isinstance(commands, dict):
            raise ValueError(f"scripts must be an object in {package / 'package.json'}")
        selected = list(
            dict.fromkeys(
                script for script in scripts if isinstance(commands.get(script), str) and commands[script].strip()
            )
        )
        if not selected:
            continue
        manager = _package_manager(package, root, manifests, options.get("packageManager"))
        for script in selected:
            label = package.name if package == root else package.relative_to(root).as_posix()
            checks.append(Check(f"{label}: {script}", [manager, "run", script], str(package), {"CI": "1"}))
    return checks
