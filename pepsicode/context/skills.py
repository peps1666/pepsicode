from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillSummary:
    name: str
    description: str
    path: str
    source: str


@dataclass(slots=True)
class LoadedSkill(SkillSummary):
    content: str


def split_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into its YAML frontmatter fields and its body.

    Only the flat ``key: value`` subset of YAML is parsed — that is all the
    SKILL.md format uses, and it avoids a PyYAML dependency.  Files without
    frontmatter yield an empty mapping and the original text.
    """
    normalized = markdown.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized

    closing = normalized.find("\n---", 3)
    if closing == -1:
        return {}, normalized

    block = normalized[4:closing]
    body = normalized[closing + len("\n---") :].lstrip("\n")

    fields: dict[str, str] = {}
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        # Indented lines belong to a nested structure we do not model.
        if line[:1].isspace():
            continue
        key, _, value = stripped.partition(":")
        value = value.strip().strip("'\"")
        if key.strip() and value:
            fields[key.strip()] = value
    return fields, body


def extract_description(markdown: str) -> str:
    """Best-effort one-line description for a skill.

    Prefers the frontmatter ``description`` (the standard SKILL.md field),
    then ``name``, and only then falls back to the first prose line of the
    body.  Without the frontmatter step a standard SKILL.md yields ``"---"``,
    which makes the skill list injected into the system prompt useless.
    """
    fields, body = split_frontmatter(markdown)
    for key in ("description", "name"):
        value = fields.get(key)
        if value:
            return value.replace("`", "")

    paragraphs = [block.strip() for block in body.split("\n\n") if block.strip()]
    for block in paragraphs:
        if block.startswith("#"):
            continue
        for line in [part.strip() for part in block.split("\n")]:
            if line and not line.startswith("#"):
                return line.replace("`", "")
    return "No description provided."


def extract_skill_name(markdown: str, fallback: str) -> str:
    """Frontmatter ``name`` if present and safe, else the directory name."""
    fields, _ = split_frontmatter(markdown)
    declared = fields.get("name", "").strip()
    if declared and _is_safe_skill_name(declared):
        return declared
    return fallback


def _is_safe_skill_name(name: str) -> bool:
    """Reject names that could escape the skill root when joined to a path."""
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return not Path(name).is_absolute()


def _home_dir() -> Path:
    return Path.home()


def _skill_roots(cwd: str | Path) -> list[tuple[Path, str]]:
    base = Path(cwd)
    home = _home_dir()
    return [
        (base / ".pepsi-code" / "skills", "project"),
        (home / ".pepsi-code" / "skills", "user"),
        (base / ".claude" / "skills", "compat_project"),
        (home / ".claude" / "skills", "compat_user"),
    ]


def _list_skill_dirs(root: Path, source: str) -> list[LoadedSkill]:
    if not root.exists():
        return []
    results: list[LoadedSkill] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        skill_path = entry / "SKILL.md"
        if not skill_path.exists():
            continue
        content = skill_path.read_text(encoding="utf-8")
        results.append(
            LoadedSkill(
                name=extract_skill_name(content, entry.name),
                description=extract_description(content),
                path=str(skill_path),
                source=source,
                content=content,
            )
        )
    return results


def discover_skills(cwd: str | Path) -> list[SkillSummary]:
    by_name: dict[str, LoadedSkill] = {}
    for root, source in _skill_roots(cwd):
        for skill in _list_skill_dirs(root, source):
            by_name.setdefault(skill.name, skill)
    return [
        SkillSummary(
            name=skill.name,
            description=skill.description,
            path=skill.path,
            source=skill.source,
        )
        for skill in by_name.values()
    ]


def load_skill(cwd: str | Path, name: str) -> LoadedSkill | None:
    normalized_name = name.strip()
    # The name is joined straight onto a skill root, so anything that could
    # walk out of that root (separators, "..", drive letters) is rejected
    # rather than sanitized.
    if not _is_safe_skill_name(normalized_name):
        return None
    for root, source in _skill_roots(cwd):
        skill_path = root / normalized_name / "SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text(encoding="utf-8")
            return LoadedSkill(
                name=extract_skill_name(content, normalized_name),
                description=extract_description(content),
                path=str(skill_path),
                source=source,
                content=content,
            )
    # Fall back to frontmatter names, which discover_skills advertises and
    # which need not match the directory they live in.
    for root, source in _skill_roots(cwd):
        for skill in _list_skill_dirs(root, source):
            if skill.name == normalized_name:
                return skill
    return None


def _managed_skill_root(scope: str, cwd: str | Path) -> Path:
    return (Path(cwd) / ".pepsi-code" / "skills") if scope == "project" else (_home_dir() / ".pepsi-code" / "skills")


def install_skill(cwd: str | Path, source_path: str, name: str | None = None, scope: str = "user") -> dict[str, str]:
    source = Path(source_path)
    if not source.is_absolute():
        source = Path(cwd) / source
    if source.is_dir():
        skill_file = source / "SKILL.md"
        inferred_name = source.name
    else:
        skill_file = source if source.name == "SKILL.md" else source / "SKILL.md"
        inferred_name = skill_file.parent.name
    if not skill_file.exists():
        raise RuntimeError(f"No SKILL.md found in {source}")

    skill_name = (name or inferred_name).strip()
    if not skill_name:
        raise RuntimeError("Skill name cannot be empty.")

    target_dir = _managed_skill_root(scope, cwd) / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill_file, target_dir / "SKILL.md")
    return {"name": skill_name, "targetPath": str(target_dir / "SKILL.md")}


def remove_managed_skill(cwd: str | Path, name: str, scope: str = "user") -> dict[str, object]:
    target_path = _managed_skill_root(scope, cwd) / name
    if not target_path.exists():
        return {"removed": False, "targetPath": str(target_path)}
    shutil.rmtree(target_path)
    return {"removed": True, "targetPath": str(target_path)}
