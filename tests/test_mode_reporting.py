"""Tests for bounded committed-mode text, JSON, and GitHub reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.modes.checker import check_modes
from yaga.modes.models import (
    MAX_MODE_DIAGNOSTICS,
    MODE_DISALLOWED_CODE,
    ModeEntry,
    ModeKind,
    ModePathOverride,
    ModePolicy,
    ModeSelection,
)
from yaga.modes.reporting import (
    MAX_GITHUB_ANNOTATIONS,
    MAX_TEXT_DIAGNOSTICS,
    ModeOutputFormat,
    render_mode_error,
    render_mode_report,
)
from yaga.modes.service import CheckedMode

_MODE_METADATA = {
    ModeKind.REGULAR: ("100644", "blob"),
    ModeKind.EXECUTABLE: ("100755", "blob"),
    ModeKind.SYMLINK: ("120000", "blob"),
    ModeKind.GITLINK: ("160000", "commit"),
}


def _entry(path: str, kind: ModeKind) -> ModeEntry:
    mode, object_type = _MODE_METADATA[kind]
    return ModeEntry(path, "c" * 40, mode, object_type)


def _checked(
    tmp_path: Path,
    *,
    entries: tuple[ModeEntry, ...],
    policy: ModePolicy | None = None,
) -> CheckedMode:
    selected_policy = policy or ModePolicy(1, tuple(ModeKind))
    selection = ModeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        entries=tuple(sorted(entries, key=lambda entry: entry.path)),
    )
    return CheckedMode(
        report=check_modes(selected_policy, selection),
        policy_path=(tmp_path / "mode-policy.toml").resolve(),
    )


def test_json_pass_report_has_the_exact_stable_schema_and_fixed_mode_counts(
    tmp_path: Path,
) -> None:
    checked = _checked(
        tmp_path,
        entries=(
            _entry("README.md", ModeKind.REGULAR),
            _entry("bin/run", ModeKind.EXECUTABLE),
            _entry("docs/current", ModeKind.SYMLINK),
            _entry("vendor/dependency", ModeKind.GITLINK),
        ),
        policy=ModePolicy(
            1,
            (ModeKind.GITLINK, ModeKind.SYMLINK, ModeKind.EXECUTABLE, ModeKind.REGULAR),
        ),
    )

    document = json.loads(render_mode_report(checked, ModeOutputFormat.JSON))

    assert list(document) == [
        "schema_version",
        "kind",
        "status",
        "valid",
        "identity",
        "policy",
        "counts",
        "diagnostics",
        "diagnostics_omitted",
    ]
    assert document == {
        "schema_version": 1,
        "kind": "mode_policy",
        "status": "passed",
        "valid": True,
        "identity": {
            "policy_path": str((tmp_path / "mode-policy.toml").resolve()),
            "repository_path": str((tmp_path / "repository").resolve()),
            "revision": "release-candidate",
            "commit_sha": "a" * 40,
            "tree_sha": "b" * 40,
        },
        "policy": {
            "mode_policy_version": 1,
            "default_allowed_modes": [
                "regular",
                "executable",
                "symlink",
                "gitlink",
            ],
            "path_overrides": [],
        },
        "counts": {
            "entries": 4,
            "by_mode": {
                "regular": 1,
                "executable": 1,
                "symlink": 1,
                "gitlink": 1,
            },
            "findings": 0,
        },
        "diagnostics": [],
        "diagnostics_omitted": 0,
    }


def test_json_reports_normalized_policy_and_complete_diagnostic_shapes(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        entries=(
            _entry("docs/current", ModeKind.SYMLINK),
            _entry("scripts/run", ModeKind.EXECUTABLE),
        ),
        policy=ModePolicy(
            1,
            (ModeKind.REGULAR,),
            (
                ModePathOverride(
                    "scripts/**",
                    (ModeKind.GITLINK, ModeKind.SYMLINK),
                ),
            ),
        ),
    )

    document = json.loads(render_mode_report(checked, ModeOutputFormat.JSON))

    assert document["status"] == "failed"
    assert document["valid"] is False
    assert document["policy"] == {
        "mode_policy_version": 1,
        "default_allowed_modes": ["regular"],
        "path_overrides": [
            {
                "pattern": "scripts/**",
                "allowed_modes": ["symlink", "gitlink"],
            }
        ],
    }
    assert document["counts"] == {
        "entries": 2,
        "by_mode": {
            "regular": 0,
            "executable": 1,
            "symlink": 1,
            "gitlink": 0,
        },
        "findings": 2,
    }
    assert document["diagnostics"] == [
        {
            "code": MODE_DISALLOWED_CODE,
            "message": "committed entry mode is not allowed by policy",
            "path": "docs/current",
            "actual_mode": "120000",
            "actual_kind": "symlink",
            "allowed_modes": ["regular"],
            "pattern": None,
        },
        {
            "code": MODE_DISALLOWED_CODE,
            "message": "committed entry mode is not allowed by policy",
            "path": "scripts/run",
            "actual_mode": "100755",
            "actual_kind": "executable",
            "allowed_modes": ["symlink", "gitlink"],
            "pattern": "scripts/**",
        },
    ]


def test_json_preserves_exact_counts_while_bounding_displayed_diagnostics(
    tmp_path: Path,
) -> None:
    entries = tuple(_entry(f"bin/tool-{index:03}", ModeKind.EXECUTABLE) for index in range(300))
    checked = _checked(
        tmp_path,
        entries=entries,
        policy=ModePolicy(1, (ModeKind.REGULAR,)),
    )

    document = json.loads(render_mode_report(checked, ModeOutputFormat.JSON))

    assert document["counts"] == {
        "entries": 300,
        "by_mode": {
            "regular": 0,
            "executable": 300,
            "symlink": 0,
            "gitlink": 0,
        },
        "findings": 300,
    }
    assert len(document["diagnostics"]) == MAX_MODE_DIAGNOSTICS
    assert document["diagnostics"][0]["path"] == "bin/tool-000"
    assert document["diagnostics"][-1]["path"] == "bin/tool-255"
    assert document["diagnostics_omitted"] == 300 - MAX_MODE_DIAGNOSTICS
    assert "entries" not in document


def test_json_preserves_bounded_revision_path_and_pattern_values(tmp_path: Path) -> None:
    revision = "r" * 500
    path = "a" * 4_000
    pattern = "a" * 500 + "*"
    policy = ModePolicy(
        1,
        (ModeKind.REGULAR,),
        (ModePathOverride(pattern, (ModeKind.SYMLINK,)),),
    )
    selection = ModeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision=revision,
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        entries=(_entry(path, ModeKind.EXECUTABLE),),
    )
    checked = CheckedMode(
        report=check_modes(policy, selection),
        policy_path=(tmp_path / "mode-policy.toml").resolve(),
    )

    document = json.loads(render_mode_report(checked, ModeOutputFormat.JSON))

    assert document["identity"]["revision"] == revision
    assert document["policy"]["path_overrides"][0]["pattern"] == pattern
    assert document["diagnostics"][0]["path"] == path
    assert document["diagnostics"][0]["pattern"] == pattern


def test_text_report_has_identity_bounded_findings_mode_details_and_summary(
    tmp_path: Path,
) -> None:
    entries = tuple(_entry(f"bin/tool-{index:02}", ModeKind.EXECUTABLE) for index in range(10))
    checked = _checked(
        tmp_path,
        entries=entries,
        policy=ModePolicy(1, (ModeKind.REGULAR,)),
    )

    rendered = render_mode_report(checked, ModeOutputFormat.TEXT)

    assert rendered.startswith(
        f"Policy: {(tmp_path / 'mode-policy.toml').resolve()}\n"
        f"Repository: {(tmp_path / 'repository').resolve()}\n"
        "Revision: release-candidate\n"
        f"Commit: {'a' * 40}\n"
        f"Tree: {'b' * 40}\n"
    )
    assert rendered.count(f"FAILED  [{MODE_DISALLOWED_CODE}]") == MAX_TEXT_DIAGNOSTICS
    assert "bin/tool-07" in rendered
    assert "bin/tool-08" not in rendered
    assert "actual: 100755 executable; allowed: regular" in rendered
    assert "... 2 additional diagnostic(s) omitted" in rendered
    assert rendered.endswith(
        "Mode policy: 10 entry(s); regular 0, executable 10, symlink 0, gitlink 0; 10 finding(s)."
    )


def test_text_and_github_reports_sanitize_and_escape_untrusted_paths(tmp_path: Path) -> None:
    path = "bad%\n,:.sh"
    checked = _checked(
        tmp_path,
        entries=(_entry(path, ModeKind.EXECUTABLE),),
        policy=ModePolicy(1, (ModeKind.REGULAR,)),
    )

    text = render_mode_report(checked, ModeOutputFormat.TEXT)
    github = render_mode_report(checked, ModeOutputFormat.GITHUB)

    assert path not in text
    assert "bad%?,:.sh" in text
    assert "\r" not in text
    annotation, summary = github.splitlines()
    assert annotation.startswith(
        f"::error file=bad%25%0A%2C%3A.sh,title=YAGA {MODE_DISALLOWED_CODE}::"
    )
    assert "actual mode 100755 (executable); allowed modes: regular" in annotation
    assert "\r" not in github
    assert summary == (
        "YAGA mode policy: 1 entry(s); regular 0, executable 1, symlink 0, gitlink 0; 1 finding(s)."
    )


def test_github_report_reserves_last_annotation_for_exact_omitted_count(
    tmp_path: Path,
) -> None:
    entries = tuple(_entry(f"bin/tool-{index:02}", ModeKind.EXECUTABLE) for index in range(40))
    checked = _checked(
        tmp_path,
        entries=entries,
        policy=ModePolicy(1, (ModeKind.REGULAR,)),
    )

    lines = render_mode_report(checked, ModeOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "bin/tool-30" in annotations[-2]
    assert "bin/tool-31" not in "\n".join(annotations)
    assert annotations[-1] == (
        "::error title=YAGA mode policy::9 additional committed-mode violation(s) omitted"
    )
    assert lines[-1].endswith("40 finding(s).")


@pytest.mark.parametrize("output_format", list(ModeOutputFormat))
def test_operational_errors_are_structured_sanitized_and_bounded(
    output_format: ModeOutputFormat,
) -> None:
    error = ConfigurationError("bad%\x1b[31m\r\n" + "x" * 1_000)

    rendered = render_mode_error(error, output_format)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    if output_format is ModeOutputFormat.JSON:
        document = json.loads(rendered)
        assert document == {
            "schema_version": 1,
            "kind": "mode_policy",
            "status": "error",
            "valid": False,
            "error": {
                "kind": "configuration",
                "message": document["error"]["message"],
            },
        }
        assert len(document["error"]["message"]) <= 500
    elif output_format is ModeOutputFormat.GITHUB:
        assert rendered.startswith("::error title=YAGA mode policy::")
        assert "%25" in rendered
        assert len(rendered.removeprefix("::error title=YAGA mode policy::")) <= 500
    else:
        assert rendered.startswith("YAGA configuration error: bad%?")
