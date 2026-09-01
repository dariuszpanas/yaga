"""Tests for bounded committed-path text, JSON, and GitHub reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.paths.checker import check_paths
from yaga.paths.models import (
    MAX_PATH_DIAGNOSTICS,
    PATH_ASCII_CASE_COLLISION_CODE,
    PATH_RULES,
    PATH_WINDOWS_CHARACTER_CODE,
    PATH_WINDOWS_RESERVED_CODE,
    PATH_WINDOWS_TRAILING_CODE,
    WINDOWS_COMPATIBLE_V1_PROFILE,
    PathDiagnostic,
    PathPolicy,
    PathReport,
    PathRuleCount,
    PathSelection,
)
from yaga.paths.reporting import (
    MAX_GITHUB_ANNOTATIONS,
    MAX_TEXT_DIAGNOSTICS,
    PathOutputFormat,
    render_path_error,
    render_path_report,
)
from yaga.paths.service import CheckedPath


def _checked(
    tmp_path: Path,
    *,
    paths: tuple[str, ...],
    policy: PathPolicy | None = None,
) -> CheckedPath:
    selected_policy = policy or PathPolicy(
        path_policy_version=1,
        rules=PATH_RULES,
        profile=WINDOWS_COMPATIBLE_V1_PROFILE,
    )
    selection = PathSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=tuple(sorted(paths)),
    )
    return CheckedPath(
        report=check_paths(selected_policy, selection),
        policy_path=(tmp_path / "path-policy.toml").resolve(),
    )


def test_json_pass_report_has_the_exact_stable_schema_and_counts(tmp_path: Path) -> None:
    checked = _checked(tmp_path, paths=("README.md", "src/main.py"))

    document = json.loads(render_path_report(checked, PathOutputFormat.JSON))

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
        "kind": "path_policy",
        "status": "passed",
        "valid": True,
        "identity": {
            "policy_path": str((tmp_path / "path-policy.toml").resolve()),
            "repository_path": str((tmp_path / "repository").resolve()),
            "revision": "release-candidate",
            "commit_sha": "a" * 40,
            "tree_sha": "b" * 40,
        },
        "policy": {
            "path_policy_version": 1,
            "profile": WINDOWS_COMPATIBLE_V1_PROFILE,
            "rules": list(PATH_RULES),
        },
        "counts": {
            "paths": 2,
            "components": 3,
            "findings": 0,
            "by_code": {},
        },
        "diagnostics": [],
        "diagnostics_omitted": 0,
    }


def test_json_reports_each_stable_diagnostic_shape_and_exact_code_counts(
    tmp_path: Path,
) -> None:
    checked = _checked(
        tmp_path,
        paths=(
            "CON.txt",
            "README.md",
            "Readme.md",
            "bad?.txt",
            "folder/name. ",
        ),
    )

    document = json.loads(render_path_report(checked, PathOutputFormat.JSON))

    assert document["status"] == "failed"
    assert document["valid"] is False
    assert document["counts"] == {
        "paths": 5,
        "components": 6,
        "findings": 5,
        "by_code": {
            PATH_WINDOWS_CHARACTER_CODE: 1,
            PATH_WINDOWS_TRAILING_CODE: 1,
            PATH_WINDOWS_RESERVED_CODE: 1,
            PATH_ASCII_CASE_COLLISION_CODE: 2,
        },
    }
    diagnostics = {item["code"]: item for item in document["diagnostics"]}
    assert diagnostics[PATH_WINDOWS_CHARACTER_CODE] == {
        "code": PATH_WINDOWS_CHARACTER_CODE,
        "message": "path component contains Windows-incompatible character U+003F",
        "path": "bad?.txt",
        "component": 1,
        "related_path": None,
    }
    assert diagnostics[PATH_WINDOWS_TRAILING_CODE] == {
        "code": PATH_WINDOWS_TRAILING_CODE,
        "message": "path component ends in an ASCII space or period",
        "path": "folder/name. ",
        "component": 2,
        "related_path": None,
    }
    assert diagnostics[PATH_WINDOWS_RESERVED_CODE] == {
        "code": PATH_WINDOWS_RESERVED_CODE,
        "message": "path component uses a reserved Windows device basename",
        "path": "CON.txt",
        "component": 1,
        "related_path": None,
    }
    collision_diagnostics = [
        item for item in document["diagnostics"] if item["code"] == PATH_ASCII_CASE_COLLISION_CODE
    ]
    assert collision_diagnostics == [
        {
            "code": PATH_ASCII_CASE_COLLISION_CODE,
            "message": "path has an ASCII case-insensitive checkout collision",
            "path": "README.md",
            "component": None,
            "related_path": "Readme.md",
        },
        {
            "code": PATH_ASCII_CASE_COLLISION_CODE,
            "message": "path has an ASCII case-insensitive checkout collision",
            "path": "Readme.md",
            "component": None,
            "related_path": "README.md",
        },
    ]


def test_json_preserves_exact_counts_while_omitting_bounded_diagnostics(
    tmp_path: Path,
) -> None:
    paths = tuple(f"bad?/file-{index:03}.txt" for index in range(300))
    checked = _checked(
        tmp_path,
        paths=paths,
        policy=PathPolicy(1, ("windows-characters",)),
    )

    document = json.loads(render_path_report(checked, PathOutputFormat.JSON))

    assert document["counts"] == {
        "paths": 300,
        "components": 600,
        "findings": 300,
        "by_code": {PATH_WINDOWS_CHARACTER_CODE: 300},
    }
    assert len(document["diagnostics"]) == MAX_PATH_DIAGNOSTICS
    assert document["diagnostics"][0]["path"] == "bad?/file-000.txt"
    assert document["diagnostics"][-1]["path"] == "bad?/file-255.txt"
    assert document["diagnostics_omitted"] == 300 - MAX_PATH_DIAGNOSTICS
    assert "paths" not in document


def test_json_preserves_complete_policy_bounded_revision_and_path_values(
    tmp_path: Path,
) -> None:
    revision = "r" * 500
    path = "a" * 4_000 + "?"
    policy = PathPolicy(1, ("windows-characters",))
    selection = PathSelection(
        repository=(tmp_path / "repository").resolve(),
        revision=revision,
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=(path,),
    )
    checked = CheckedPath(
        report=check_paths(policy, selection),
        policy_path=(tmp_path / "path-policy.toml").resolve(),
    )

    document = json.loads(render_path_report(checked, PathOutputFormat.JSON))

    assert document["identity"]["revision"] == revision
    assert document["diagnostics"][0]["path"] == path


def test_text_report_has_exact_identity_bounded_findings_and_summary(tmp_path: Path) -> None:
    paths = tuple(f"bad?/file-{index:02}.txt" for index in range(10))
    checked = _checked(
        tmp_path,
        paths=paths,
        policy=PathPolicy(1, ("windows-characters",)),
    )

    rendered = render_path_report(checked, PathOutputFormat.TEXT)

    assert rendered.startswith(
        f"Policy: {(tmp_path / 'path-policy.toml').resolve()}\n"
        f"Repository: {(tmp_path / 'repository').resolve()}\n"
        "Revision: release-candidate\n"
        f"Commit: {'a' * 40}\n"
        f"Tree: {'b' * 40}\n"
    )
    assert rendered.count(f"FAILED  [{PATH_WINDOWS_CHARACTER_CODE}]") == MAX_TEXT_DIAGNOSTICS
    assert "bad?/file-07.txt" in rendered
    assert "bad?/file-08.txt" not in rendered
    assert "... 2 additional diagnostic(s) omitted" in rendered
    assert rendered.endswith("Path policy: 10 path(s), 20 component(s), 10 finding(s).")


def test_text_and_github_reports_sanitize_and_escape_untrusted_diagnostic_text(
    tmp_path: Path,
) -> None:
    path = "bad%\n,:?.txt"
    policy = PathPolicy(1, ("windows-characters",))
    selection = PathSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=(path,),
    )
    diagnostic = PathDiagnostic(
        code=PATH_WINDOWS_CHARACTER_CODE,
        message="bad%\x1b[31m\r\nline",
        path=path,
        component=1,
    )
    checked = CheckedPath(
        report=PathReport(
            policy=policy,
            selection=selection,
            diagnostics=(diagnostic,),
            rule_counts=(PathRuleCount("windows-characters", 1),),
        ),
        policy_path=(tmp_path / "path-policy.toml").resolve(),
    )

    text = render_path_report(checked, PathOutputFormat.TEXT)
    github = render_path_report(checked, PathOutputFormat.GITHUB)

    assert "\x1b" not in text
    assert "\r" not in text
    assert "bad%?%2C%3A?.txt" not in text
    annotation, summary = github.splitlines()
    assert annotation.startswith(
        "::error file=bad%25%0A%2C%3A?.txt,title=YAGA path.windows-character::"
    )
    assert "bad%25?[31m??line; component 1" in annotation
    assert "\x1b" not in github
    assert "\r" not in github
    assert summary == "YAGA path policy: 1 path(s), 1 component(s), 1 finding(s)."


def test_github_report_reserves_the_last_annotation_for_omitted_count(
    tmp_path: Path,
) -> None:
    paths = tuple(f"bad?/file-{index:02}.txt" for index in range(40))
    checked = _checked(
        tmp_path,
        paths=paths,
        policy=PathPolicy(1, ("windows-characters",)),
    )

    lines = render_path_report(checked, PathOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "bad?/file-30.txt" in annotations[-2]
    assert "bad?/file-31.txt" not in "\n".join(annotations)
    assert annotations[-1] == (
        "::error title=YAGA path policy::9 additional committed-path violation(s) omitted"
    )
    assert lines[-1] == "YAGA path policy: 40 path(s), 80 component(s), 40 finding(s)."


@pytest.mark.parametrize("output_format", list(PathOutputFormat))
def test_operational_errors_are_structured_sanitized_and_bounded(
    output_format: PathOutputFormat,
) -> None:
    error = ConfigurationError("bad%\x1b[31m\r\n" + "x" * 1_000)

    rendered = render_path_error(error, output_format)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    if output_format is PathOutputFormat.JSON:
        document = json.loads(rendered)
        assert document == {
            "schema_version": 1,
            "kind": "path_policy",
            "status": "error",
            "valid": False,
            "error": {
                "kind": "configuration",
                "message": document["error"]["message"],
            },
        }
        assert len(document["error"]["message"]) <= 500
    elif output_format is PathOutputFormat.GITHUB:
        assert rendered.startswith("::error title=YAGA path policy::")
        assert "%25" in rendered
        assert len(rendered.removeprefix("::error title=YAGA path policy::")) <= 500
    else:
        assert rendered.startswith("YAGA configuration error: bad%?")
