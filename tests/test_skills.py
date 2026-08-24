from pathlib import Path

import pytest

from pepsicode.context.skills import discover_skills, extract_description, load_skill

FRONTMATTER_SKILL = """---
name: data-viz
description: Build charts from tabular data
---

# Data Viz

Some body text that should not become the description.
"""


def test_discover_skills_prefers_project_root(tmp_path: Path, monkeypatch) -> None:
    project_skill = tmp_path / ".pepsi-code" / "skills" / "demo" / "SKILL.md"
    project_skill.parent.mkdir(parents=True)
    project_skill.write_text("# Demo\n\nProject description\n", encoding="utf-8")

    user_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    user_skill = user_home / ".pepsi-code" / "skills" / "demo" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("# Demo\n\nUser description\n", encoding="utf-8")

    skills = discover_skills(tmp_path)

    assert len(skills) == 1
    assert skills[0].description == "Project description"
    loaded = load_skill(tmp_path, "demo")
    assert loaded is not None
    assert loaded.content.startswith("# Demo")


def test_extract_description_reads_frontmatter() -> None:
    assert extract_description(FRONTMATTER_SKILL) == "Build charts from tabular data"


def test_extract_description_ignores_frontmatter_in_body_fallback() -> None:
    without_description = "---\nname: data-viz\n---\n\n# Title\n\nBody line\n"
    assert extract_description(without_description) == "data-viz"

    no_fields = "---\n---\n\n# Title\n\nBody line\n"
    assert extract_description(no_fields) == "Body line"


def test_frontmatter_name_overrides_directory_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    skill_file = tmp_path / ".pepsi-code" / "skills" / "01-charts" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(FRONTMATTER_SKILL, encoding="utf-8")

    skills = discover_skills(tmp_path)

    assert [skill.name for skill in skills] == ["data-viz"]
    # The advertised (frontmatter) name must be loadable, not just the dir name.
    assert load_skill(tmp_path, "data-viz") is not None
    assert load_skill(tmp_path, "01-charts") is not None


@pytest.mark.parametrize(
    "name",
    ["../demo", "..\\demo", "..", "sub/demo", "", "   ", "C:\\Windows\\System32"],
)
def test_load_skill_rejects_path_traversal(tmp_path: Path, monkeypatch, name: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    outside = tmp_path / ".pepsi-code" / "demo" / "SKILL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# Outside\n\nShould stay unreachable\n", encoding="utf-8")

    assert load_skill(tmp_path, name) is None
