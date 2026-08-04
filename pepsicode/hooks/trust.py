from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pepsicode.config import PEPSI_CODE_DIR

HOOK_TRUST_PATH = PEPSI_CODE_DIR / "hook-trust.json"


def hook_file_fingerprint(path: str | Path) -> str:
    candidate = Path(path).resolve()
    digest = hashlib.sha256()
    digest.update(str(candidate).encode("utf-8"))
    digest.update(b"\0")
    digest.update(candidate.read_bytes())
    return digest.hexdigest()


class HookTrustStore:
    def __init__(self, path: str | Path = HOOK_TRUST_PATH) -> None:
        self.path = Path(path)
        self._trusted = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        trusted = data.get("trusted", {}) if isinstance(data, dict) else {}
        return {str(key): str(value) for key, value in trusted.items()} if isinstance(trusted, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "trusted": self._trusted}, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def is_trusted(self, path: str | Path) -> bool:
        candidate = str(Path(path).resolve())
        try:
            return self._trusted.get(candidate) == hook_file_fingerprint(candidate)
        except OSError:
            return False

    def trust(self, path: str | Path) -> None:
        candidate = str(Path(path).resolve())
        self._trusted[candidate] = hook_file_fingerprint(candidate)
        self._save()

    def revoke(self, path: str | Path) -> None:
        self._trusted.pop(str(Path(path).resolve()), None)
        self._save()
