"""Tests for bounded committed-tree text, JSON, and GitHub reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.trees.models import (
    TREE_FORBIDDEN_CODE,
    TREE_REQUIRED_CODE,
    TreeDiagnostic,
    TreePolicy,
    TreeReport,
    TreeSelection,
)
from yaga.trees.reporting import (
    MAX_GITHUB_ANNOTATIONS,
    MAX_JSON_DIAGNOSTICS,
    MAX_TEXT_DIAGNOSTICS,
    TreeOutputFormat,
    render_tree_error,
    render_tree_report,
)
from yaga.trees.service import CheckedTree


def _checked(
    tmp_path: Path,
    *,
    paths: tuple[str, ...],
    required: tuple[str, ...] = ("README.md",),
    forbidden: tuple[str, ...] = ("blocked/**",),
    diagnostics: tuple[TreeDiagnostic, ...] = (),
) -> CheckedTree:
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=required,
        forbidden_patterns=forbidden,
    )
    selection = TreeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision="release-candidate",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=paths,
    )
    return CheckedTree(
        report=TreeReport(policy=policy, selection=selection, diagnostics=diagnostics),
        policy_path=(tmp_path / "tree-policy.toml").resolve(),
    )


def _forbidden(path: str, *, message: str = "tracked path is forbidden") -> TreeDiagnostic:
    return TreeDiagnostic(
        code=TREE_FORBIDDEN_CODE,
        message=message,
        path=path,
        pattern="blocked/**",
    )


def test_json_pass_report_has_the_exact_stable_schema(tmp_path: Path) -> None:
    checked = _checked(tmp_path, paths=("README.md", "src/main.py"))

    document = json.loads(render_tree_report(checked, TreeOutputFormat.JSON))

    assert document == {
        "schema_version": 1,
        "kind": "tree_policy",
        "status": "passed",
        "valid": True,
        "policy_path": str((tmp_path / "tree-policy.toml").resolve()),
        "repository_path": str((tmp_path / "repository").resolve()),
        "revision": "release-candidate",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "entries_checked": 2,
        "required_paths": ["README.md"],
        "forbidden_patterns": ["blocked/**"],
        "diagnostics": [],
        "diagnostics_omitted": 0,
        "missing_required": 0,
        "forbidden_paths": 0,
    }


def test_json_diagnostics_keep_report_order_and_are_bounded(tmp_path: Path) -> None:
    blocked = tuple(f"blocked/file-{index:03}.txt" for index in range(300))
    diagnostics = (
        TreeDiagnostic(
            code=TREE_REQUIRED_CODE,
            message="required tracked path is missing",
            path="README.md",
        ),
        *(_forbidden(path) for path in blocked),
    )
    checked = _checked(tmp_path, paths=blocked, diagnostics=diagnostics)

    document = json.loads(render_tree_report(checked, TreeOutputFormat.JSON))

    assert document["status"] == "failed"
    assert document["valid"] is False
    assert document["missing_required"] == 1
    assert document["forbidden_paths"] == 300
    assert len(document["diagnostics"]) == MAX_JSON_DIAGNOSTICS
    assert document["diagnostics_omitted"] == 301 - MAX_JSON_DIAGNOSTICS
    assert document["diagnostics"][0] == {
        "code": TREE_REQUIRED_CODE,
        "message": "required tracked path is missing",
        "path": "README.md",
        "pattern": None,
    }
    assert document["diagnostics"][1]["path"] == "blocked/file-000.txt"
    assert document["diagnostics"][1]["pattern"] == "blocked/**"
    assert "paths" not in document


def test_json_preserves_complete_core_bounded_revision_and_tree_paths(tmp_path: Path) -> None:
    revision = "r" * 400
    blocked = "blocked/" + "a" * 1400
    policy = TreePolicy(
        tree_policy_version=1,
        required_paths=(),
        forbidden_patterns=("blocked/**",),
    )
    selection = TreeSelection(
        repository=(tmp_path / "repository").resolve(),
        revision=revision,
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=(blocked,),
    )
    checked = CheckedTree(
        report=TreeReport(
            policy=policy,
            selection=selection,
            diagnostics=(_forbidden(blocked),),
        ),
        policy_path=(tmp_path / "tree-policy.toml").resolve(),
    )

    document = json.loads(render_tree_report(checked, TreeOutputFormat.JSON))

    assert document["revision"] == revision
    assert document["diagnostics"][0]["path"] == blocked


def test_text_report_has_exact_identity_header_bounded_findings_and_summary(
    tmp_path: Path,
) -> None:
    blocked = tuple(f"blocked/file-{index:02}.txt" for index in range(10))
    checked = _checked(
        tmp_path,
        paths=("README.md", *blocked),
        diagnostics=tuple(_forbidden(path) for path in blocked),
    )

    rendered = render_tree_report(checked, TreeOutputFormat.TEXT)

    assert rendered.startswith(
        f"Policy: {(tmp_path / 'tree-policy.toml').resolve()}\n"
        f"Repository: {(tmp_path / 'repository').resolve()}\n"
        "Revision: release-candidate\n"
        f"Commit: {'a' * 40}\n"
        f"Tree: {'b' * 40}\n"
    )
    assert rendered.count("FAILED  [tree.forbidden]") == MAX_TEXT_DIAGNOSTICS
    assert "blocked/file-07.txt" in rendered
    assert "blocked/file-08.txt" not in rendered
    assert "... 2 additional diagnostic(s) omitted" in rendered
    assert rendered.endswith("Tree policy: 11 tree entries; 0 missing required; 10 forbidden.")


def test_github_required_has_no_fake_file_and_forbidden_path_is_escaped(
    tmp_path: Path,
) -> None:
    blocked = "blocked/a%,:file.txt"
    diagnostics = (
        TreeDiagnostic(
            code=TREE_REQUIRED_CODE,
            message="required tracked path is missing",
            path="README.md",
        ),
        _forbidden(blocked, message="tracked path is forbidden%\nsecond line"),
    )
    checked = _checked(tmp_path, paths=(blocked,), diagnostics=diagnostics)

    required, forbidden, summary = render_tree_report(
        checked,
        TreeOutputFormat.GITHUB,
    ).splitlines()

    assert required.startswith("::error title=YAGA tree.required::")
    assert "file=" not in required
    assert forbidden.startswith(
        "::error file=blocked/a%25%2C%3Afile.txt,title=YAGA tree.forbidden::"
    )
    assert "%25" in forbidden
    assert "%0A" not in forbidden
    assert summary == ("YAGA tree policy: 1 tree entries; 1 missing required; 1 forbidden.")


def test_github_report_reserves_the_last_annotation_for_omitted_count(
    tmp_path: Path,
) -> None:
    blocked = tuple(f"blocked/file-{index:02}.txt" for index in range(40))
    checked = _checked(
        tmp_path,
        paths=("README.md", *blocked),
        diagnostics=tuple(_forbidden(path) for path in blocked),
    )

    lines = render_tree_report(checked, TreeOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "blocked/file-30.txt" in annotations[-2]
    assert "blocked/file-31.txt" not in "\n".join(annotations)
    assert annotations[-1] == (
        "::error title=YAGA tree policy::9 additional committed-tree violation(s) omitted"
    )
    assert lines[-1].startswith("YAGA tree policy:")


@pytest.mark.parametrize("output_format", list(TreeOutputFormat))
def test_operational_errors_are_structured_sanitized_and_bounded(
    output_format: TreeOutputFormat,
) -> None:
    error = ConfigurationError("bad%\x1b[31m\r\n" + "x" * 1000)

    rendered = render_tree_error(error, output_format)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    if output_format is TreeOutputFormat.JSON:
        document = json.loads(rendered)
        assert document == {
            "schema_version": 1,
            "kind": "tree_policy",
            "status": "error",
            "valid": False,
            "error": {
                "kind": "configuration",
                "message": document["error"]["message"],
            },
        }
        assert len(document["error"]["message"]) <= 500
    elif output_format is TreeOutputFormat.GITHUB:
        assert rendered.startswith("::error title=YAGA tree policy::")
        assert "%25" in rendered
        assert len(rendered.removeprefix("::error title=YAGA tree policy::")) <= 500
    else:
        assert rendered.startswith("YAGA configuration error: bad%?")
