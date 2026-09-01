"""CLI contract tests for the standalone workflow-security provider."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app

runner = CliRunner()
RECOMMENDED_V1_RULES = [
    "permissions.explicit",
    "permissions.top_level_write",
    "permissions.write_all",
    "secrets.inherit",
    "checkout.untrusted_ref",
]
RECOMMENDED_V2_RULES = [*RECOMMENDED_V1_RULES, "checkout.persist_credentials"]


def unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _write_workflow(
    repository: Path,
    content: str,
    *,
    relative_path: str = ".github/workflows/ci.yml",
) -> Path:
    workflow = repository.joinpath(*relative_path.split("/"))
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(content, encoding="utf-8")
    return workflow


def test_workflow_security_help_exposes_command_and_closed_options() -> None:
    group = runner.invoke(app, ["workflow", "--help"])
    command = runner.invoke(app, ["workflow", "security", "--help"])

    assert group.exit_code == 0
    assert "security" in group.stdout
    assert command.exit_code == 0
    command_help = unstyle(command.stdout)
    assert "Enforce a bounded GitHub Actions trust policy" in command_help
    assert ".github/workflows" in command_help
    assert "--repo" in command_help
    assert "--profile" in command_help
    assert "recommended-v1" in command_help
    assert "recommended-v2" in command_help
    assert "--rule" in command_help
    assert "Repeat explicitly" in command_help
    assert "--format" in command_help
    for output_format in ("text", "json", "github"):
        assert output_format in command_help


def test_workflow_security_defaults_to_recommended_v1_and_passes_json(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path, "permissions: {}\njobs: {}\n")

    result = runner.invoke(
        app,
        ["workflow", "security", "--repo", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "kind": "github_workflow_security",
        "profile": "recommended-v1",
        "rules": RECOMMENDED_V1_RULES,
        "valid": True,
        "checked": 1,
        "passed": 1,
        "failed": 0,
        "diagnostics": 0,
        "workflows": [
            {
                "path": ".github/workflows/ci.yml",
                "status": "passed",
                "valid": True,
                "diagnostics": [],
            }
        ],
    }


def test_workflow_security_accepts_recommended_v2_and_reports_its_rules(
    tmp_path: Path,
) -> None:
    _write_workflow(
        tmp_path,
        "permissions: {}\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          persist-credentials: false\n",
    )

    result = runner.invoke(
        app,
        [
            "workflow",
            "security",
            "--repo",
            str(tmp_path),
            "--profile",
            "recommended-v2",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["profile"] == "recommended-v2"
    assert document["rules"] == RECOMMENDED_V2_RULES
    assert document["valid"] is True


def test_workflow_security_v2_finding_has_stable_github_annotation(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "permissions: {}\njobs:\n  check:\n    steps:\n      - uses: actions/checkout@v4\n",
    )

    result = runner.invoke(
        app,
        [
            "workflow",
            "security",
            "--repo",
            str(tmp_path),
            "--profile",
            "recommended-v2",
            "--format",
            "github",
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        (
            "::error file=.github/workflows/ci.yml,line=5,col=15,"
            "title=YAGA security.checkout.persist_credentials::"
            "actions/checkout must disable persisted credentials"
        ),
        "YAGA security checked 1 workflow file(s): 0 passed, 1 failed; 1 diagnostic(s).",
    ]


def test_workflow_security_repeatable_rules_are_an_exact_custom_selection(
    tmp_path: Path,
) -> None:
    relative_path = "selected/security.yml"
    _write_workflow(
        tmp_path,
        "permissions: write-all\njobs: {}\n",
        relative_path=relative_path,
    )

    result = runner.invoke(
        app,
        [
            "workflow",
            "security",
            relative_path,
            "--repo",
            str(tmp_path),
            "--rule",
            "secrets.inherit",
            "--rule",
            "permissions.explicit",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["profile"] == "custom"
    assert document["rules"] == ["permissions.explicit", "secrets.inherit"]
    assert document["valid"] is True
    assert document["workflows"] == [
        {
            "path": relative_path,
            "status": "passed",
            "valid": True,
            "diagnostics": [],
        }
    ]


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        (
            ["--profile", "recommended-v1", "--rule", "permissions.explicit"],
            "choose either a workflow security profile or explicit rules",
        ),
        (
            ["--rule", "permissions.EXPLICIT"],
            "unknown workflow security rule: permissions.EXPLICIT",
        ),
    ],
)
def test_workflow_security_selection_errors_use_exit_two_and_json_stderr(
    tmp_path: Path,
    selection: list[str],
    message: str,
) -> None:
    result = runner.invoke(
        app,
        [
            "workflow",
            "security",
            "--repo",
            str(tmp_path),
            *selection,
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "schema_version": 1,
        "error": {"kind": "input", "message": message},
    }


def test_workflow_security_finding_uses_exit_one_and_text_stdout(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "jobs: {}\n")

    result = runner.invoke(app, ["workflow", "security", "--repo", str(tmp_path)])

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        (
            "Profile: recommended-v1; rules: permissions.explicit, "
            "permissions.top_level_write, permissions.write_all, secrets.inherit, "
            "checkout.untrusted_ref."
        ),
        "FAILED  .github/workflows/ci.yml  1 diagnostic(s)",
        (
            "         [security.permissions.explicit] line 1, column 1: "
            "workflow must declare top-level permissions"
        ),
        "Checked 1 workflow file(s): 0 passed, 1 failed; 1 diagnostic(s).",
    ]


def test_workflow_security_github_output_preserves_annotation_contract(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path, "jobs: {}\n")

    result = runner.invoke(
        app,
        ["workflow", "security", "--repo", str(tmp_path), "--format", "github"],
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        (
            "::error file=.github/workflows/ci.yml,line=1,col=1,"
            "title=YAGA security.permissions.explicit::"
            "workflow must declare top-level permissions"
        ),
        ("YAGA security checked 1 workflow file(s): 0 passed, 1 failed; 1 diagnostic(s)."),
    ]
