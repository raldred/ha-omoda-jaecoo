"""The scheduling blueprint's packaging and documented install story.

The release archive is deliberately component-only, so neither HACS nor the ZIP
installs a blueprint. These tests fail if that changes silently, or if the
documentation starts implying an automatic install that does not happen.
"""

import re
import zipfile
from pathlib import Path

import pytest

from tests.test_release import commit, repo, run_git  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = "blueprints/automation/omoda_jaecoo/scheduled_conditioning.yaml"
BLUEPRINT = REPO / BLUEPRINT_PATH
SCHEDULING_DOC = REPO / "docs/scheduling.md"
README = REPO / "README.md"


def source_url() -> str:
    match = re.search(
        r"^\s*source_url:\s*(\S+)\s*$", BLUEPRINT.read_text(), re.MULTILINE
    )
    assert match, "The blueprint must declare a source_url for HA's import dialog."
    return match.group(1)


def test_blueprint_has_exactly_one_canonical_copy_outside_the_component():
    assert BLUEPRINT.is_file()
    searched = ("blueprints", "custom_components", "docs", "scripts", "tests")
    copies = sorted(
        str(path.relative_to(REPO))
        for folder in searched
        for path in (REPO / folder).rglob("scheduled_conditioning.yaml")
        # Test virtual environments are not repository content.
        if ".venv" not in path.parts
    )
    # A second copy inside the component would be shipped in the ZIP and would
    # then disagree with the one users import.
    assert copies == [BLUEPRINT_PATH]


def test_release_archive_never_ships_a_root_blueprint(repo, tmp_path):  # noqa: F811
    from scripts.build_release import build_release

    blueprint = repo / BLUEPRINT_PATH
    blueprint.parent.mkdir(parents=True, exist_ok=True)
    blueprint.write_text("blueprint:\n  name: Synthetic\n  domain: automation\n")
    commit(repo)
    metadata = build_release(repo, tmp_path / "dist", expected_version="0.6.0")
    with zipfile.ZipFile(tmp_path / "dist/omoda_jaecoo.zip") as archive:
        names = archive.namelist()
    assert not any("blueprint" in name for name in names)
    # The component contents are unchanged by adding a root blueprint.
    assert len(names) == metadata["file_count"] == 10


def test_blueprint_import_url_points_at_its_own_repository_path():
    url = source_url()
    expected = (
        f"https://github.com/raldred/ha-omoda-jaecoo/blob/main/{BLUEPRINT_PATH}"
    )
    # A moved or renamed blueprint must not keep advertising a stale import URL.
    assert url == expected


def test_documentation_explains_a_manual_import_and_claims_no_auto_install():
    doc = SCHEDULING_DOC.read_text()
    assert source_url() in doc
    for required in (
        "HACS does not install",
        "not included in the release ZIP",
        "Import blueprint",
        "private",
    ):
        assert required in doc, required
    # No document may promise an automatic blueprint install.
    for text in (doc, README.read_text()):
        assert not re.search(
            r"HACS\s+(?:automatically\s+)?(?:installs|downloads|adds)\s+the\s+blueprint",
            text,
            re.IGNORECASE,
        )


def test_documentation_keeps_the_safety_and_honesty_warnings():
    doc = SCHEDULING_DOC.read_text()
    for required in (
        "garage",
        "opt-in",
        "does not prove",
        "never retried",
        "cabin temperature",
        "30 seconds",
    ):
        assert required in doc, required


def test_readme_links_the_scheduling_document_and_the_new_controls():
    readme = README.read_text()
    assert "docs/scheduling.md" in readme
    for required in ("omoda_jaecoo.rapid_heat", "Rear-window defrost"):
        assert required in readme, required


@pytest.mark.parametrize("path", ["custom_components/omoda_jaecoo/manifest.json"])
def test_version_is_consistent_and_has_release_notes(path):
    import json

    version = json.loads((REPO / path).read_text())["version"]
    pyproject = (REPO / "pyproject.toml").read_text()
    assert f'version = "{version}"' in pyproject
    notes = REPO / f"docs/releases/{version}.md"
    assert notes.is_file(), f"Release notes are missing for {version}"
    assert notes.read_text().strip()
