"""Production releases must identify the package and have dated notes."""

from pathlib import Path

import pytest
import yaml

from scripts.check_release import release_notes, validate_release


def test_release_accepts_exact_version_and_date() -> None:
    validate_release("v0.1.0", "0.1.0", "## [Unreleased]\n\n## [0.1.0] - 2026-09-13\n")


@pytest.mark.parametrize(
    ("tag", "version", "notes"),
    [
        ("v0.2.0", "0.1.0", "## [0.1.0] - 2026-09-13"),
        ("--help", "0.1.0", "## [0.1.0] - 2026-09-13"),
        ("v0.1.0rc1", "0.1.0", "## [0.1.0] - 2026-09-13"),
        ("v0.1.0", "0.1.0", "## 0.1.0 — Beta"),
        ("v0.1.0", "0.1.0", "## [0.1.0] - 2026-02-30"),
        ("v0.1.0", "0.1.0", "## [0.1.0] - 2026-09-13\n## [0.1.0] - 2026-09-14"),
    ],
)
def test_release_rejects_invalid_candidate(tag: str, version: str, notes: str) -> None:
    with pytest.raises(ValueError):
        validate_release(tag, version, notes)


def test_release_workflow_isolates_upload_from_source_and_manual_runs() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/release.yml").read_text("utf-8"), yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["if"] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert len(publish["steps"]) == 2
    assert publish["steps"][0]["uses"].startswith("actions/download-artifact@")
    assert publish["steps"][1]["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert all("run" not in step for step in publish["steps"])
    build = workflow["jobs"]["build"]
    assert any(
        'make ci BUILD_ARGS="--output-dir dist"' in step.get("run", "") for step in build["steps"]
    )


def test_release_notes_select_only_the_requested_version() -> None:
    changelog = """# Changelog

## [Unreleased]

Future changes.

## [0.1.2] - 2026-09-20

### Fixed

- Preserve package artifacts.

## [0.1.1] - 2026-09-19

Old changes.
"""
    assert release_notes("0.1.2", changelog) == (
        "### Fixed\n\n- Preserve package artifacts.\n\n"
        "[PyPI](https://pypi.org/project/yaga-cli/0.1.2/)\n"
    )


def test_release_notes_reject_empty_section() -> None:
    with pytest.raises(ValueError, match="must describe"):
        release_notes("0.1.2", "## [0.1.2] - 2026-09-20\n\n## [0.1.1] - 2026-09-19\nOld changes.")


def test_github_release_requires_successful_publish_and_separate_permissions() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/release.yml").read_text("utf-8"), yaml.BaseLoader
    )
    announce = workflow["jobs"]["github-release"]
    assert announce["needs"] == ["build", "publish"]
    assert announce["if"] == workflow["jobs"]["publish"]["if"]
    assert "always()" not in announce["if"]
    assert announce["permissions"] == {"contents": "write"}
    assert all("checkout@" not in step.get("uses", "") for step in announce["steps"])
    downloads = [step["with"]["name"] for step in announce["steps"] if "uses" in step]
    assert downloads == ["distributions", "release-notes"]
    command = announce["steps"][-1]
    assert command["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "--verify-tag" in command["run"]
    assert "dist/*.whl dist/*.tar.gz" in command["run"]
    assert "--notes-file release-notes/release-notes.md" in command["run"]
    assert "${{" not in command["run"]
    notes = [
        step
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("with", {}).get("name") == "release-notes"
    ]
    assert len(notes) == 1
    assert notes[0]["with"]["path"] == "release-notes.md"
