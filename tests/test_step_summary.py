"""Unit and stdlib-isolation tests for GitHub Step Summary Markdown rendering."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from yaga.step_summary import MAX_STEP_SUMMARY_BYTES, render_step_summary
from yaga.version_requirement import VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_repo_step_summary_renders_provider_table_and_collapses_group_logs() -> None:
    report_lines = [
        "::group::YAGA repository check: commit (passed)",
        "PASSED  d333d3778f13  feat(ci): dogfood YAGA Checks",
        "Checked 1 commit(s): 1 passed, 0 failed, 0 skipped.",
        "Config: /repo/pyproject.toml",
        "::endgroup::",
        "::group::YAGA repository check: mode (passed)",
        "Policy: /repo/.yaga/mode-policy.toml",
        "Mode policy: 439 entry(s); regular 439, executable 0, symlink 0, gitlink 0; 0 finding(s).",
        "::endgroup::",
        "::group::YAGA repository check: tree (failed)",
        "Policy: /repo/.yaga/tree-policy.toml",
        "FAILED [tree.required] missing required path 'LICENSE'",
        "Tree policy: 438 tree entries; 1 missing required; 0 forbidden.",
        "::endgroup::",
        "::error title=YAGA tree.required::missing required path 'LICENSE'",
        "YAGA repository checks: 3 selected; 2 passed, 1 failed, 0 errored.",
    ]

    markdown = render_step_summary(
        mode="repo",
        status_code=1,
        report_lines=report_lines,
        error_lines=[],
    )

    assert "## `>_` YAGA Checks · `repo`" in markdown
    assert (
        f"**Status:** Findings reported (exit `1`) · **Version:** `{VERSION}` · "
        "**Summary:** 3 selected; 2 passed, 1 failed, 0 errored."
    ) in markdown
    assert "::group::" not in markdown
    assert "::endgroup::" not in markdown
    assert "| Provider | Result | Highlights |" in markdown
    assert (
        "| `commit` | **Passed** | Checked 1 commit(s): 1 passed, 0 failed, 0 skipped. |"
        in markdown
    )
    assert (
        "| `mode` | **Passed** | "
        "439 entry(s); regular 439, executable 0, symlink 0, gitlink 0; 0 finding(s). |"
    ) in markdown
    assert (
        "| `tree` | **Failed** | 438 tree entries; 1 missing required; 0 forbidden. |" in markdown
    )
    assert "| Level | Rule / Scope | Location | Message |" in markdown
    assert "| **Error** | `tree.required` | — | missing required path 'LICENSE' |" in markdown
    assert "<details>" in markdown
    assert "<summary><b>Detailed provider output (3)</b></summary>" in markdown
    assert "[commit · passed]" in markdown


def test_findings_table_decodes_workflow_escapes_and_formats_locations() -> None:
    report_lines = [
        "::error file=.github/workflows/ci.yml,line=12,col=4,title=YAGA workflow%3A%2Ccheck::"
        "unpinned reference%0Ause a 40-char SHA%25",
        "::warning file=README.md,line=5,title=YAGA docs::advisory note",
        "Checked 1 workflow file(s): 0 passed, 1 failed; 1 diagnostic(s).",
    ]

    markdown = render_step_summary(
        mode="workflow",
        status_code=1,
        report_lines=report_lines,
        error_lines=[],
    )

    assert (
        "| **Error** | `workflow:,check` | `.github/workflows/ci.yml:12:4` | unpinned reference · use a 40-char SHA% |"
        in markdown
    )
    assert "| **Warning** | `docs` | `README.md:5` | advisory note |" in markdown


def test_notice_and_single_error_become_compact_summary_headlines() -> None:
    review_md = render_step_summary(
        mode="pr-review",
        status_code=0,
        report_lines=[
            "::notice title=YAGA Agent review::Agent review result: passed; 0 blocking failure(s)."
        ],
        error_lines=[],
    )
    assert "**Status:** Passed (exit `0`)" in review_md
    assert "**Summary:** Agent review result: passed; 0 blocking failure(s)." in review_md
    assert "| Level |" not in review_md

    err_md = render_step_summary(
        mode="commit",
        status_code=2,
        report_lines=[],
        error_lines=[
            "::error title=YAGA action::YAGA input error: check 'commit' accepts only 'check' and 'trusted-config'"
        ],
    )
    assert "**Status:** Error (exit `2`)" in err_md
    assert "accepts only 'check' and 'trusted-config'" in err_md
    assert "| Level |" not in err_md


def test_step_summary_sanitizes_untrusted_markdown_html_and_bounds_bytes() -> None:
    report_lines = [
        "::group::YAGA repository check: branch (failed)",
        "raw <script>alert(1)</script> & \x1b[31mansi\x1b[0m",
        "line-" + ("x" * 90_000),
        "::endgroup::",
        "::error title=YAGA branch.allowed::branch 'feat/*|evil|[link](https://example.invalid)`cmd`' rejected",
        "YAGA branch policy: failed for 'feat/**' & <img src=x> [x](https://example.invalid).",
    ]

    markdown = render_step_summary(
        mode="../bad mode",
        status_code=1,
        report_lines=report_lines,
        error_lines=[],
    )

    encoded = markdown.encode("utf-8")
    assert len(encoded) <= MAX_STEP_SUMMARY_BYTES
    assert "## `>_` YAGA Checks · `action`" in markdown
    assert "<script>" not in markdown
    assert "<img" not in markdown
    assert "\x1b" not in markdown
    assert "\\|evil\\|" in markdown
    assert "\\[link\\]" in markdown
    assert "&#96;cmd&#96;" in markdown
    assert markdown.endswith("</pre>\n\n</details>\n")


def test_step_summary_cli_runs_under_stdlib_only_no_site_packages(tmp_path: Path) -> None:
    report_file = tmp_path / "report.txt"
    error_file = tmp_path / "error.txt"
    report_file.write_text(
        "YAGA branch policy: passed; branch 'feat/clean' matched 'feat/*'.\n",
        encoding="utf-8",
    )
    error_file.write_text("", encoding="utf-8")

    env = {key: val for key, val in os.environ.items() if not key.upper().startswith("PYTHON")}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUTF8"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-m",
            "yaga.step_summary",
            "--mode",
            "branch",
            "--status",
            "0",
            "--report",
            str(report_file),
            "--error",
            str(error_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert "## `>_` YAGA Checks · `branch`" in completed.stdout
    assert "matched 'feat/\\*'" in completed.stdout
