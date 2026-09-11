"""Operational errors at the installed Agent review receipt boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app


@pytest.mark.parametrize("output_format", ["json", "github"])
@pytest.mark.parametrize("malformed", ["deep-json", "unpaired-surrogate"])
def test_malformed_receipt_preserves_operational_exit_and_report_format(
    tmp_path: Path,
    output_format: str,
    malformed: str,
) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text(
        "[agent-review]\n"
        "version = 1\n"
        "required = ['security']\n"
        "[agent-review.agents.security]\n"
        "preset = 'codex'\n"
        "instruction = 'Review security.'\n",
        encoding="utf-8",
    )
    receipt = tmp_path / "results.json"
    raw = (
        "[" * 50_000 + "0" + "]" * 50_000
        if malformed == "deep-json"
        else json.dumps(
            {
                "version": 1,
                "plan_digest": "0" * 64,
                "results": [{"lens": "security", "outcome": "passed", "summary": "\ud800"}],
            }
        )
    )
    receipt.write_text(raw, encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "gate",
            "agent-review",
            "policy",
            "evaluate",
            "--file",
            str(policy),
            "--results",
            str(receipt),
            "--format",
            output_format,
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    if output_format == "json":
        document = json.loads(result.stderr)
        assert document["schema_version"] == 1
        assert document["error"]["kind"] == "configuration"
        assert "Agent review result" in document["error"]["message"]
    else:
        assert result.stderr.startswith("::error title=YAGA Agent review::Agent review result")
        assert len(result.stderr.splitlines()) == 1
