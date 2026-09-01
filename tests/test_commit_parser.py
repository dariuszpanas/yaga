"""Tests for Conventional Commit message structure parsing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from yaga.commits.models import CommitFooter, ParsedCommit
from yaga.commits.parser import (
    header_from,
    iter_footer_starts,
    normalize_message,
    parse_message,
)


def footer_starts(parsed: ParsedCommit) -> list[CommitFooter]:
    """Materialize footer starts only when a test needs to inspect them."""
    if parsed.footer_start_line is None:
        return []
    return list(
        iter_footer_starts(
            parsed.footer_lines,
            start_line=parsed.footer_start_line,
        )
    )


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
    assert [(footer.token, footer.separator, footer.line) for footer in footer_starts(parsed)] == [
        ("BREAKING CHANGE", ":", 5),
        ("Refs", "#", 7),
    ]


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
    assert [(footer.token, footer.separator, footer.line) for footer in footer_starts(parsed)] == [
        ("Reviewed-by", ":", 5),
        ("Refs", "#", 7),
    ]


def test_footer_value_may_continue_across_blank_line_delimited_paragraphs() -> None:
    parsed = parse_message(
        "feat!: replace the old command\n\n"
        "BREAKING CHANGE: use the new command\n\n"
        "The compatibility alias remains for one release.\n\n"
        "Remove the alias after downstream users migrate."
    )

    assert parsed is not None
    assert parsed.body == ""
    assert parsed.footer_lines == (
        "BREAKING CHANGE: use the new command",
        "",
        "The compatibility alias remains for one release.",
        "",
        "Remove the alias after downstream users migrate.",
    )
    assert parsed.breaking_footer


def test_subsequent_footer_token_terminates_a_blank_line_continued_value() -> None:
    parsed = parse_message(
        "feat: preserve footer boundaries\n\n"
        "Keep this paragraph as the body.\n\n"
        "Reviewed-by: Example Maintainer\n\n"
        "The review covered the compatibility path.\n\n"
        "BREAKING CHANGE: remove the old command\n\n"
        "Use the replacement command instead."
    )

    assert parsed is not None
    assert parsed.body == "Keep this paragraph as the body."
    assert parsed.footer_lines == (
        "Reviewed-by: Example Maintainer",
        "",
        "The review covered the compatibility path.",
        "",
        "BREAKING CHANGE: remove the old command",
        "",
        "Use the replacement command instead.",
    )
    assert parsed.breaking_footer


def test_subsequent_footer_token_needs_no_blank_line_boundary() -> None:
    parsed = parse_message(
        "fix: locate every footer token\n\n"
        "Keep this paragraph as the body.\n\n"
        "Reviewed-by: Example Maintainer\n"
        "This line remains part of the review value.\n"
        "Refs #42\n"
        "BREAKING-CHANGE: replace the old command"
    )

    assert parsed is not None
    assert parsed.body == "Keep this paragraph as the body."
    assert [(footer.token, footer.separator, footer.line) for footer in footer_starts(parsed)] == [
        ("Reviewed-by", ":", 5),
        ("Refs", "#", 7),
        ("BREAKING-CHANGE", ":", 8),
    ]
    assert parsed.breaking_footer


def test_footer_like_paragraph_start_deterministically_begins_the_footer_suffix() -> None:
    parsed = parse_message(
        "docs: explain the boundary\n\n"
        "Keep this paragraph as prose.\n\n"
        "Notes: this token starts footer parsing\n\n"
        "Even this blank-separated paragraph remains in the footer value."
    )

    assert parsed is not None
    assert parsed.body == "Keep this paragraph as prose."
    assert parsed.footer_lines == (
        "Notes: this token starts footer parsing",
        "",
        "Even this blank-separated paragraph remains in the footer value.",
    )


def test_trailer_like_line_inside_body_paragraph_does_not_start_footers() -> None:
    parsed = parse_message(
        "fix(cli): keep body paragraphs intact\n\nExplain why the change is needed.\nRefs #42\n"
    )

    assert parsed is not None
    assert parsed.body == "Explain why the change is needed.\nRefs #42"
    assert parsed.footer_lines == ()
    assert parsed.footer_start_line is None
    assert footer_starts(parsed) == []


@pytest.mark.parametrize(
    "line",
    [
        "Token:  two spaces",
        "Token:value without a space",
        "Token # 42",
        "Token#42",
        "Token_: invalid token character",
    ],
)
def test_footer_start_uses_exact_git_style_grammar(line: str) -> None:
    parsed = parse_message(f"docs: retain prose\n\n{line}")

    assert parsed is not None
    assert parsed.body == line
    assert parsed.footer_start_line is None
    assert footer_starts(parsed) == []


def test_breaking_change_hash_separator_is_a_nonbreaking_footer() -> None:
    parsed = parse_message("docs: retain grammar\n\nBREAKING CHANGE #42")

    assert parsed is not None
    assert [(footer.token, footer.separator, footer.line) for footer in footer_starts(parsed)] == [
        ("BREAKING CHANGE", "#", 3),
    ]
    assert not parsed.breaking_footer


def test_footer_locations_account_for_a_missing_header_separator() -> None:
    parsed = parse_message("fix: retain structure\nRefs #42\nReviewed-by: Maintainer")

    assert parsed is not None
    assert not parsed.separator_valid
    assert [(footer.token, footer.separator, footer.line) for footer in footer_starts(parsed)] == [
        ("Refs", "#", 2),
        ("Reviewed-by", ":", 3),
    ]


def test_footer_start_iterator_does_not_precollect_or_read_past_a_yield() -> None:
    consumed: list[str] = []

    def lines() -> Iterator[str]:
        consumed.append("first")
        yield "Refs #42"
        consumed.append("second")
        yield "Reviewed-by: Maintainer"

    starts = iter_footer_starts(lines(), start_line=11)

    assert consumed == []
    assert next(starts) == CommitFooter(token="Refs", separator="#", line=11)
    assert consumed == ["first"]


def test_parsed_commit_does_not_retain_footer_start_objects() -> None:
    parsed = parse_message(
        "fix: keep parser memory bounded\n\n"
        + "\n".join(f"Token-{index}: value" for index in range(4096))
    )

    assert parsed is not None
    assert parsed.footer_start_line == 3
    assert len(parsed.footer_lines) == 4096
    assert not hasattr(parsed, "footers")


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
