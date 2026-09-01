"""Tests for Conventional Commit message structure parsing."""

from __future__ import annotations

import pytest

from yaga.commits.parser import header_from, normalize_message, parse_message


def test_parser_retains_all_header_fields() -> None:
    parsed = parse_message("feat(parser)!: accept complete messages")

    assert parsed is not None
    assert parsed.commit_type == "feat"
    assert parsed.scope == "parser"
    assert parsed.description == "accept complete messages"
    assert parsed.breaking_header
    assert parsed.breaking


def test_parser_splits_body_from_git_style_footers() -> None:
    parsed = parse_message(
        "fix(cli): preserve multiline messages\n\n"
        "Keep the body available to policy rules.\n\n"
        "BREAKING CHANGE: replace the old command\n"
        " continuation text\n"
        "Refs #42\n"
    )

    assert parsed is not None
    assert parsed.body == "Keep the body available to policy rules."
    assert parsed.footer_lines == (
        "BREAKING CHANGE: replace the old command",
        " continuation text",
        "Refs #42",
    )
    assert parsed.breaking_footer
    assert parsed.breaking


def test_parser_accepts_unindented_multiline_footer_values() -> None:
    parsed = parse_message(
        "fix(cli): preserve footer values\n\n"
        "Keep this paragraph as the body.\n\n"
        "Reviewed-by: Example Maintainer\n"
        "This value continues without indentation.\n"
        "Refs #42\n"
    )

    assert parsed is not None
    assert parsed.body == "Keep this paragraph as the body."
    assert parsed.footer_lines == (
        "Reviewed-by: Example Maintainer",
        "This value continues without indentation.",
        "Refs #42",
    )


def test_trailer_like_line_inside_body_paragraph_does_not_start_footers() -> None:
    parsed = parse_message(
        "fix(cli): keep body paragraphs intact\n\nExplain why the change is needed.\nRefs #42\n"
    )

    assert parsed is not None
    assert parsed.body == "Explain why the change is needed.\nRefs #42"
    assert parsed.footer_lines == ()


def test_footer_only_message_has_no_body() -> None:
    parsed = parse_message("feat!: replace the API\n\nBREAKING-CHANGE: use the CLI")

    assert parsed is not None
    assert parsed.body == ""
    assert parsed.footer_lines == ("BREAKING-CHANGE: use the CLI",)


def test_lowercase_breaking_footer_is_ordinary_body() -> None:
    parsed = parse_message("feat: add a mode\n\nbreaking change: not a reserved footer")

    assert parsed is not None
    assert parsed.body == "breaking change: not a reserved footer"
    assert not parsed.breaking


def test_crlf_is_normalized_without_trimming_the_header() -> None:
    message = "fix(cli): accept Windows files\r\n\r\nBody text.\r\n"
    parsed = parse_message(message)

    assert normalize_message(message) == "fix(cli): accept Windows files\n\nBody text."
    assert header_from(message) == "fix(cli): accept Windows files"
    assert parsed is not None
    assert parsed.body == "Body text."


def test_missing_body_separator_is_preserved_as_structure_state() -> None:
    parsed = parse_message("fix: retain the header\nBody without a separator")

    assert parsed is not None
    assert not parsed.separator_valid
    assert parsed.body_start_line == 2
    assert parsed.body == "Body without a separator"


@pytest.mark.parametrize(
    "message",
    [
        "",
        "fix",
        "fix:no space",
        "fix: ",
        "fix(): empty scope",
        "fix( ): whitespace-only scope",
        "fix(api client): whitespace-bearing scope",
        "fi\u202ex: format control in type",
        "fix(api\u200b): format control in scope",
        "fi\x00x: control in type",
        "fix(api\x00): control in scope",
        "fix(scope) !: misplaced marker",
        " fix: leading whitespace",
        "fix: trailing whitespace ",
    ],
)
def test_malformed_headers_are_rejected(message: str) -> None:
    assert parse_message(message) is None
