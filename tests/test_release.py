"""Production releases must identify the package and have dated notes."""

from pathlib import Path

import pytest
import yaml

from scripts.check_release import release_notes, validate_release, verify_bundled_assets
from scripts.native_release import TARGETS, verify_image
from scripts.release_wheels import PLATFORMS, wheel_tags
from tests.test_macos_release_wheels import MACOS_11, legacy
from tests.test_macos_release_wheels import image as mach_image


def test_standalone_executable_must_fit_the_action_distribution(tmp_path: Path) -> None:
    image = tmp_path / "yaga"
    with image.open("wb") as stream:
        stream.write(mach_image(legacy(MACOS_11), kind=2))
        stream.truncate(100 * 1024**2)
    verify_image(image, "aarch64-apple-darwin")
    with image.open("ab") as stream:
        stream.write(b"\0")
    with pytest.raises(ValueError, match="100 MiB Action distribution limit"):
        verify_image(image, "aarch64-apple-darwin")


def test_release_accepts_exact_version_and_date() -> None:
    validate_release(
        "v0.1.0",
        "0.1.0",
        "## [Unreleased]\n\n## [0.1.0] - 2026-09-13\n",
        runtime_version="0.1.0",
        core_dependency_version="0.1.0",
        action_versions={"0.1.0"},
    )


def test_release_rejects_mismatched_runtime_version() -> None:
    with pytest.raises(ValueError, match="Cargo workspace version"):
        validate_release(
            "v0.1.0",
            "0.1.0",
            "## [Unreleased]\n\n## [0.1.0] - 2026-09-13\n",
            runtime_version="0.0.9",
        )


def test_release_rejects_mismatched_core_dependency_or_action_version() -> None:
    with pytest.raises(ValueError, match="yaga-core dependency version"):
        validate_release(
            "v0.6.0",
            "0.6.0",
            "## [Unreleased]\n\n## [0.6.0] - 2026-11-24\n",
            runtime_version="0.6.0",
            core_dependency_version="0.5.0",
        )
    with pytest.raises(ValueError, match=r"action\.yml version"):
        validate_release(
            "v0.6.0",
            "0.6.0",
            "## [Unreleased]\n\n## [0.6.0] - 2026-11-24\n",
            runtime_version="0.6.0",
            core_dependency_version="0.6.0",
            action_versions={"0.5.0", "0.6.0"},
        )


def test_crate_license_copies_match_repository_license() -> None:
    root = Path(__file__).resolve().parents[1]
    expected = (root / "LICENSE").read_text(encoding="utf-8")
    for crate in ("crates/yaga-core/LICENSE", "crates/yaga-cli/LICENSE"):
        assert (root / crate).read_text(encoding="utf-8") == expected, crate
    verify_bundled_assets(root)


def test_verify_bundled_assets_rejects_drift(tmp_path: Path) -> None:
    for rel, content in (
        ("docs/install.sh", b"#!/bin/sh\n"),
        ("crates/yaga-cli/assets/install.sh", b"#!/bin/sh\n"),
        ("docs/install.ps1", b"# ps1\n"),
        ("crates/yaga-cli/assets/install.ps1", b"# ps1\n"),
        ("LICENSE", b"BSD-3-Clause\n"),
        ("crates/yaga-core/LICENSE", b"BSD-3-Clause\n"),
        ("crates/yaga-cli/LICENSE", b"BSD-3-Clause\n"),
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    verify_bundled_assets(tmp_path)

    (tmp_path / "crates/yaga-cli/assets/install.ps1").write_bytes(b"# drifted\n")
    with pytest.raises(ValueError, match=r"crates/yaga-cli/assets/install\.ps1"):
        verify_bundled_assets(tmp_path)


def test_linux_release_wheels_advertise_manylinux_and_musllinux_tags() -> None:
    for arch in ("x86_64", "aarch64"):
        tag = PLATFORMS[f"{arch}-unknown-linux-musl"]
        assert wheel_tags(tag) == [
            f"py3-none-manylinux2014_{arch}",
            f"py3-none-manylinux_2_17_{arch}",
            f"py3-none-musllinux_1_1_{arch}",
        ]


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
    assert publish["needs"] == ["build", "native-assets", "python-assets"]
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
    assert any(
        "cargo package --workspace --locked" in step.get("run", "") for step in build["steps"]
    )
    crates = workflow["jobs"]["publish-crates"]
    assert crates["needs"] == ["build", "native-assets", "python-assets"]
    assert crates["if"] == publish["if"]
    assert crates["permissions"] == {"contents": "read", "id-token": "write"}
    assert crates["environment"]["name"] == "crates-io"
    assert crates["steps"][0]["uses"].startswith("actions/checkout@")
    assert crates["steps"][0]["with"]["persist-credentials"] == "false"
    assert any(
        step.get("uses", "").startswith("rust-lang/crates-io-auth-action@")
        for step in crates["steps"]
    )
    assert any(
        "cargo publish --workspace --locked" in step.get("run", "") for step in crates["steps"]
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
        "[PyPI](https://pypi.org/project/yaga-cli/0.1.2/) · "
        "[crates.io (`yaga-cli`)](https://crates.io/crates/yaga-cli/0.1.2) · "
        "[crates.io (`yaga-core`)](https://crates.io/crates/yaga-core/0.1.2)\n"
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
    assert announce["needs"] == ["build", "publish", "publish-crates"]
    assert announce["if"] == workflow["jobs"]["publish"]["if"]
    assert "always()" not in announce["if"]
    assert announce["permissions"] == {"contents": "write"}
    assert all("checkout@" not in step.get("uses", "") for step in announce["steps"])
    downloads = [step["with"]["name"] for step in announce["steps"] if "uses" in step]
    assert downloads == ["python-release", "release-notes", "native-release"]
    command = announce["steps"][-1]
    assert command["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "--verify-tag" in command["run"]
    assert "dist/*.whl dist/*.tar.gz" in command["run"]
    assert "native/yaga-*" in command["run"]
    assert "--notes-file release-notes/release-notes.md" in command["run"]
    assert "${{" not in command["run"]
    notes = [
        step
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("with", {}).get("name") == "release-notes"
    ]
    assert len(notes) == 1
    assert notes[0]["with"]["path"] == "release-notes.md"


def test_native_python_release_requires_all_verified_wheels() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / ".github/workflows/release.yml").read_bytes(), yaml.BaseLoader)
    jobs = workflow["jobs"]
    source = next(
        s["with"]
        for s in jobs["build"]["steps"]
        if s.get("with", {}).get("name") == "source-distribution"
    )
    assert source["path"] == "dist/*.tar.gz"
    assert any(
        s.get("with", {}).get("name") == "source-distribution"
        for s in jobs["python-assets"]["steps"]
    )
    assert set(PLATFORMS) == set(TARGETS)
    assert jobs["python-assets"]["needs"] == ["build", "native-binaries"]
    assert jobs["publish"]["steps"][0]["with"]["name"] == "python-release"
