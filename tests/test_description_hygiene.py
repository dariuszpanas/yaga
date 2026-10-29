"""Description transport and prose hygiene regressions from escaped-text failures."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.descriptions.checker import check_description
from yaga.descriptions.models import DescriptionPolicy
from yaga.errors import ConfigurationError, InputError

runner = CliRunner()
COMMAND = ["pull-request", "description", "check"]


def _codes(body: str) -> set[str]:
    return {item.code for item in check_description(body, DescriptionPolicy()).diagnostics}


@pytest.mark.parametrize(
    ("damaged", "codepoint"),
    [
        ("\x00module", "U+0000"),
        ("\x07ctive", "U+0007"),
        ("\x08ackend", "U+0008"),
        ("\x0boltage", "U+000B"),
        ("\x0creshness", "U+000C"),
        ("\registry", "U+000D"),
        ("\x1b[31mred", "U+001B"),
    ],
    ids=["nul", "bell", "backspace", "vertical-tab", "form-feed", "bare-cr", "escape"],
)
def test_destructive_control_errors_identify_the_character_and_original_line(
    damaged: str, codepoint: str
) -> None:
    with pytest.raises(InputError) as failure:
        check_description("Explain the change.\nUse " + damaged + " safely.", DescriptionPolicy())

    assert codepoint in str(failure.value)
    assert "line 2" in str(failure.value)


def test_destructive_controls_in_code_are_still_input_errors() -> None:
    with pytest.raises(InputError, match="U\\+0008"):
        check_description("Useful prose.\n```text\n\x08ackend\n```", DescriptionPolicy())


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_standard_line_endings_remain_valid(newline: str) -> None:
    body = newline.join(("## Summary", "Explain the change.", "", "## Validation", "Tests passed."))
    report = check_description(body, DescriptionPolicy())

    assert report.valid
    assert report.word_count == 5


@pytest.mark.parametrize(
    "character",
    ["\ufeff", "\u200b", "\u2060", "\u202e", "\u2066", "\u034f", "\u2028", "\u2029"],
    ids=[
        "bom",
        "zero-width-space",
        "word-joiner",
        "rtl-override",
        "ltr-isolate",
        "grapheme-joiner",
        "line-separator",
        "paragraph-separator",
    ],
)
def test_invisible_prose_characters_have_explicit_source_located_findings(character: str) -> None:
    report = check_description("## Summary\nVisible" + character + "text.", DescriptionPolicy())
    findings = [
        item for item in report.diagnostics if item.code == "description.invisible-character"
    ]

    assert findings
    assert findings[0].line == 2
    assert f"U+{ord(character):04X}" in findings[0].message
    assert unicodedata.name(character) in findings[0].message.upper()


def test_a_leading_bom_in_a_pr_body_is_not_silently_ignored() -> None:
    assert "description.invisible-character" in _codes("\ufeff## Summary\nExplain the change.")


def test_hygiene_coordinates_preserve_utf8_columns_after_masked_code() -> None:
    prefix = "Code `字` café"
    report = check_description("## Summary\n" + prefix + "\u200b.", DescriptionPolicy())

    assert len(report.diagnostics) == 1
    finding = report.diagnostics[0]
    assert finding.code == "description.invisible-character"
    assert finding.line == 2
    assert finding.column == len(prefix.encode("utf-8")) + 1


def test_hygiene_findings_keep_exact_counts_beyond_the_storage_limit() -> None:
    report = check_description("Explain " + "\u200b" * 300, DescriptionPolicy())

    assert report.diagnostic_count == 300
    assert len(report.diagnostics) == 256
    assert report.omitted_diagnostic_count == 44
    assert {item.code for item in report.diagnostics} == {"description.invisible-character"}


@pytest.mark.parametrize(
    "body",
    [
        "Explain cafe\u0301 behavior.",
        "Explain the العَرَبِيَّة example.",
        "Explain می\u200cروم and क्\u200dष joining.",
        "Support the 👩\u200d💻 example.",
        "Support the \U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f flag.",
    ],
    ids=["combining-accent", "arabic", "joining", "emoji-zwj", "emoji-tags"],
)
def test_legitimate_combining_marks_joiners_and_emoji_are_preserved(body: str) -> None:
    assert check_description(body, DescriptionPolicy()).valid


@pytest.mark.parametrize("heading", ["Summary 👩\u200d💻", "می\u200cروم"])
def test_template_heading_identity_preserves_legitimate_joiners(heading: str) -> None:
    report = check_description(
        f"## {heading}\nExplain the changed behavior.",
        DescriptionPolicy(),
        template=f"## {heading}\n<!-- Describe the change. -->",
    )

    assert report.valid


def test_standalone_invisible_emoji_tags_are_not_valid_flag_sequences() -> None:
    assert "description.invisible-character" in _codes("Explain \U000e0067the change.")


def test_a_tab_in_prose_is_reported_without_rewriting_the_description() -> None:
    body = "## Summary\nSet \ttl_seconds for the request."
    report = check_description(body, DescriptionPolicy())
    findings = [item for item in report.diagnostics if item.code == "description.tab"]

    assert findings
    assert findings[0].line == 2
    assert report.character_count == len(body)


@pytest.mark.parametrize(
    "body",
    [
        "Explain the change.\\n\\n## Validation\\nTests passed.",
        "Explain the change.\\r\\nTests passed.",
    ],
    ids=["double-escaped-newline", "escaped-crlf"],
)
def test_serialized_line_breaks_in_prose_are_diagnosed(body: str) -> None:
    assert "description.escaped-newline" in _codes(body)


@pytest.mark.parametrize(
    "body",
    [
        r"Explain C:\new\notes and C:\registry\notes paths.",
        r"Write \\n\\n to show literal separators.",
        r"Mention a single \n escape in prose.",
    ],
    ids=["windows-paths", "escaped-backslashes", "single-escape"],
)
def test_ordinary_paths_and_explicit_escape_examples_are_not_serialized_newlines(body: str) -> None:
    assert "description.escaped-newline" not in _codes(body)


@pytest.mark.parametrize(
    "body",
    [
        "Explain `parser changes.",
        "Explain ``parser` changes.",
        "Explain parser changes.\nLeave a stray ` marker.",
    ],
    ids=["single-run", "mismatched-runs", "second-line"],
)
def test_unmatched_backtick_runs_are_reported(body: str) -> None:
    assert "description.backtick" in _codes(body)


@pytest.mark.parametrize(
    "body",
    [
        "Explain `matched code` changes.",
        "Explain ``matched ` code`` changes.",
        r"Explain an escaped \` marker.",
        "Explain `code across\nadjacent prose lines` clearly.",
    ],
    ids=["matched", "longer-run", "escaped", "multiline"],
)
def test_matched_and_escaped_backticks_remain_valid(body: str) -> None:
    assert check_description(body, DescriptionPolicy()).valid


@pytest.mark.parametrize("wrapper", ["inline", "fenced", "comment"])
@pytest.mark.parametrize(
    "content",
    ["\u200b", "\t", r"\n\n", r"\r\n"],
    ids=["invisible", "tab", "escaped-lf", "escaped-crlf"],
)
def test_code_and_comments_do_not_produce_prose_hygiene_findings(
    wrapper: str, content: str
) -> None:
    if wrapper == "inline":
        body = "Explain the `" + content + "` code example."
    elif wrapper == "fenced":
        body = "Explain the code example.\n```text\n" + content + "\n```"
    else:
        body = "Explain the change.\n<!-- " + content + " -->"

    report = check_description(body, DescriptionPolicy())
    assert not {
        "description.invisible-character",
        "description.tab",
        "description.escaped-newline",
        "description.backtick",
    } & {item.code for item in report.diagnostics}


@pytest.mark.parametrize(
    "instruction",
    ["Replace \u200bthis.", "Replace\tthis.", r"Replace this.\n\nAdd details.", "Replace `this."],
    ids=["invisible", "tab", "serialized-newline", "backtick"],
)
def test_template_visible_prose_must_pass_hygiene_policy(instruction: str) -> None:
    with pytest.raises(ConfigurationError):
        check_description(
            "## Summary\nExplain the changed behavior.",
            DescriptionPolicy(),
            template="## Summary\n" + instruction,
        )


def _policy(tmp_path: Path) -> Path:
    path = tmp_path / "description-policy.toml"
    path.write_text("description-policy-version = 1\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "codepoint"),
    [("Useful prose.\n\x08ackend", "U+0008"), ("Useful prose.\n\registry", "U+000D")],
    ids=["backspace", "bare-cr"],
)
def test_cli_transport_corruption_returns_error_two(
    tmp_path: Path, body: str, codepoint: str
) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8", newline="")
    result = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(_policy(tmp_path)),
            "--file",
            str(body_file),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["kind"] == "input"
    assert codepoint in error["message"]
    assert "line 2" in error["message"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("Explain \u200bthe change.", "description.invisible-character"),
        ("Explain\tthe change.", "description.tab"),
        (r"Explain the change.\n\nTests passed.", "description.escaped-newline"),
        ("Explain `the change.", "description.backtick"),
    ],
    ids=["invisible", "tab", "serialized-newlines", "backtick"],
)
def test_cli_prose_hygiene_findings_return_one(tmp_path: Path, body: str, code: str) -> None:
    result = runner.invoke(
        app,
        [*COMMAND, "--policy", str(_policy(tmp_path)), "--stdin", "--format", "json"],
        input=body,
    )

    assert result.exit_code == 1, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert code in {item["code"] for item in report["diagnostics"]}
