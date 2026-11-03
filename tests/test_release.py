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
    assert publish["needs"] == ["build", "minimum-runtime"]
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


@pytest.mark.parametrize("filename", ["ci.yml", "release.yml"])
def test_minimum_security_matrix_is_complete_and_preserves_evidence(filename: str) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows" / filename).read_text("utf-8"), yaml.BaseLoader
    )
    audit = workflow["jobs"]["minimum-runtime"]
    assert workflow["permissions"] == {"contents": "read"}
    assert audit.get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert audit["strategy"]["matrix"] == {"os": ["ubuntu-latest", "windows-latest"]}
    assert audit["strategy"]["fail-fast"] == "false"
    assert audit["runs-on"] == "${{ matrix.os }}"
    assert "if" not in audit and "continue-on-error" not in audit
    assert all("continue-on-error" not in step for step in audit["steps"])
    assert all("if" not in step for step in audit["steps"][:-1])
    checkout = audit["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] == "false"
    expected_ref = (
        "${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}"
        if filename == "ci.yml"
        else "${{ github.sha }}"
    )
    assert checkout["with"]["ref"] == expected_ref
    commands = [step["run"] for step in audit["steps"] if "run" in step]
    assert "uv sync --frozen --group dev --python 3.12" in commands
    assert len(commands) == 2
    qualification = audit["steps"][-2]
    assert qualification["shell"] == "bash"
    assert "uv run --frozen --python 3.12 python scripts/check_minimum_runtime.py" in commands[-1]
    assert '--report "$YAGA_MINIMUM_REPORT"' in commands[-1]
    assert qualification["env"]["YAGA_MINIMUM_REPORT"] == "${{ runner.temp }}/minimum-runtime.json"
    evidence = audit["steps"][-1]
    assert evidence["if"] == "always()"
    assert evidence["uses"].startswith("actions/upload-artifact@")
    assert evidence["with"]["path"] == "${{ runner.temp }}/minimum-runtime.json"
    assert evidence["with"]["if-no-files-found"] == "error"
    assert "${{ matrix.os }}" in evidence["with"]["name"]
    assert evidence["with"]["name"] != "distributions"
    if filename == "release.yml":
        assert audit["needs"] == "build"
        download = audit["steps"][1]
        assert download["uses"].startswith("actions/download-artifact@")
        assert download["with"] == {"name": "distributions", "path": "dist/"}
        assert "--wheel dist/*.whl" in qualification["run"]
        assert "uv build" not in "\n".join(commands)
    else:
        gate = workflow["jobs"]["gate"]
        assert gate["needs"] == ["check", "test", "minimum-runtime"]
        assert gate["if"] == "always()"
        assert "continue-on-error" not in gate
        step = gate["steps"][0]
        assert step["env"]["MINIMUM_RUNTIME_RESULT"] == "${{ needs.minimum-runtime.result }}"
        assert '[[ "$MINIMUM_RUNTIME_RESULT" == success ]]' in step["run"]
        assert "set -euo pipefail" in step["run"]


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
