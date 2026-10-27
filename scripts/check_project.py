"""Fail development/release checks when identities or entry links drift."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote


def check(root):
    root = Path(root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    module = ast.parse((root / "pepsicode/version.py").read_text(encoding="utf-8"))
    runtime = next(
        ast.literal_eval(n.value)
        for n in module.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in n.targets)
    )
    package = json.loads((root / "pepsicode-client/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "pepsicode-client/package-lock.json").read_text(encoding="utf-8"))
    if {version, runtime, package["version"], lock["version"], lock["packages"][""]["version"]} != {version}:
        raise ValueError("Python, runtime, desktop and lockfile versions differ")
    if "${version}" not in package["build"]["artifactName"]:
        raise ValueError("Desktop artifactName must contain its package version")
    readme = (root / "README.md").read_text(encoding="utf-8")
    if f"version-{version}-" not in readme:
        raise ValueError("README version badge does not match the candidate")
    for name in ("README.md", "ONBOARDING.md"):
        for target in re.findall(r"\]\(([^)]+)\)", (root / name).read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "#", "mailto:")):
                continue
            path = unquote(target.split("#")[0].strip("<>"))
            if path and not (root / path).exists():
                raise ValueError(f"Broken local link in {name}: {path}")
    print(f"Candidate versions and entry links agree: {version}; this is not release acceptance")


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1])
