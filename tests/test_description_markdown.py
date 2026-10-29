"""Markdown heading structure and literal-markup boundary regressions."""

from __future__ import annotations

import pytest

from yaga.descriptions.checker import check_description
from yaga.descriptions.markdown import normalize_description, parse_markdown
from yaga.descriptions.models import DescriptionPolicy


@pytest.mark.parametrize("underline,level", [("=", 1), ("===", 1), ("-", 2), ("---", 2)])
def test_setext_heading_consumes_the_complete_paragraph(underline: str, level: int) -> None:
    document = parse_markdown(f"A heading\nwith two lines\n{underline}\n\nBody prose.")
    assert len(document.headings) == 1
    heading = document.headings[0]
    assert (heading.level, heading.text, heading.line, heading.end_line) == (
        level,
        "A heading with two lines",
        1,
        3,
    )
    assert all(not line.content for line in document.lines[:3])
    assert document.lines[4].content == "Body prose."


def test_setext_headings_preserve_order_with_atx_headings() -> None:
    document = parse_markdown("## First\nProse.\n\nSecond\n---\nBody.\n\n### Third\nMore prose.")
    assert [(heading.text, heading.line) for heading in document.headings] == [
        ("First", 1),
        ("Second", 4),
        ("Third", 8),
    ]


@pytest.mark.parametrize(
    "body",
    [
        "Paragraph\n\n---",
        "---",
        "===",
        "Paragraph\n- - -",
        "Paragraph\n***",
        "- item\n  continuation\n---",
        "> quote\ncontinuation\n---",
        "<div>\nSome text\n---\n</div>",
        "    Code text\n    ---",
        "\tCode text\n\t===",
        "```\nHeading\n===\n```",
        "<!--\nHeading\n===\n-->",
    ],
)
def test_block_boundaries_do_not_invent_top_level_setext_headings(body: str) -> None:
    assert parse_markdown(body).headings == ()


def test_setext_paragraph_resumes_after_a_container_blank_line() -> None:
    document = parse_markdown("- item\n  continuation\n\nSummary\n---\nBody.")
    assert [(heading.level, heading.text, heading.line) for heading in document.headings] == [
        (2, "Summary", 4)
    ]


@pytest.mark.parametrize(
    "body",
    [
        "`<h1>literal</h1>`",
        "``<h1>literal</h1>``",
        "`<h1>\nliteral</h1>`",
        "\\<h1>literal\\</h1>",
        "<!-- <h1>literal</h1> -->",
        "```html\n<h1>literal</h1>\n```",
        "~~~html\n<h1>literal</h1>\n~~~",
        "    <h1>literal</h1>",
        "\t<h1>literal</h1>",
        "  \t<h1>literal</h1>",
        "## Example\n    <h1>literal</h1>\n\nActual prose.",
        "```\ncode\n```\n    <h1>literal</h1>\n\nActual prose.",
        "Heading\n---\n    <h1>literal</h1>\n\nActual prose.",
        '<a title="<h1>">link</a>',
        "<details><summary>Details</summary>Useful text.</details>",
        "&lt;h1&gt;literal&lt;/h1&gt;",
        "<h7>Not a heading</h7>",
    ],
)
def test_raw_heading_detection_ignores_literal_code_comments_and_attributes(body: str) -> None:
    assert parse_markdown(body).html_headings == ()


def test_raw_html_headings_keep_tag_identity_and_original_utf8_byte_locations() -> None:
    body = '字 <!-- Hidden 字 --> <H2\n class="title">Content</H2>\n</h6>\n<h3 />'
    document = parse_markdown(body)
    assert [(tag.name, tag.line, tag.column, tag.closing) for tag in document.html_headings] == [
        ("h2", 1, len("字 <!-- Hidden 字 --> ".encode()) + 1, False),
        ("h2", 2, len(b' class="title">Content') + 1, True),
        ("h6", 3, 1, True),
        ("h3", 4, 1, False),
    ]


def test_headings_inside_real_html_wrappers_are_detected() -> None:
    document = parse_markdown(
        '<details>\n<summary>Details</summary>\n<H1 class="x">Text</H1>\n</details>'
    )
    assert [tag.name for tag in document.html_headings] == ["h1", "h1"]


@pytest.mark.parametrize(
    "body",
    [
        '<a title="\n\n<h1>Actual heading</h1>',
        "<script><h1>Actual heading</h1></script>",
        "<style><h1>Actual heading</h1></style>",
        "<details>\n    <h1>Actual heading</h1>\n</details>",
    ],
)
def test_other_html_cannot_hide_real_heading_tags(body: str) -> None:
    document = parse_markdown(body)
    assert [tag.name for tag in document.html_headings] == ["h1", "h1"]


def test_indentation_after_prose_remains_a_paragraph_continuation() -> None:
    document = parse_markdown("Prose starts here\n    <h1>Still paragraph markup</h1>")
    assert [tag.name for tag in document.html_headings] == ["h1", "h1"]


def test_spelling_mask_keeps_utf8_offsets_while_excluding_indented_code() -> None:
    body = "Prose.\n\n    字 code\n\nNext text."
    document = parse_markdown(body)
    assert len(document.spelling.encode()) == len(body.encode())
    assert not document.lines[2].content
    assert document.spelling.splitlines()[4] == "Next text."


def test_crlf_normalization_preserves_setext_source_lines() -> None:
    document = parse_markdown(normalize_description("Summary\r\n---\r\nBody.\r\n"))
    assert document.headings[0].line == 1
    assert document.headings[0].end_line == 2


@pytest.mark.parametrize(
    "body",
    [
        "Explain `unclosed code\n- \u200bhidden ` item.",
        "Explain `unclosed code\n> \u200bhidden ` quote.",
        "- Explain `unclosed code\n- \u200bhidden ` item.",
        "Explain `unclosed code\n1. \u200bhidden ` item.",
        "Explain `unclosed code\n***\n\u200bhidden ` prose.",
        "Explain `unclosed code\n_ _ _\n\u200bhidden ` prose.",
        "Explain `unclosed code\n<h1>\u200bhidden ` heading.</h1>",
        "Explain `unclosed code\n<div>\u200bhidden ` text.</div>",
        "Explain `unclosed code\n<script>\u200bhidden ` text.</script>",
        "Explain `unclosed code\n<style>\u200bhidden ` text.</style>",
    ],
    ids=[
        "list",
        "quote",
        "adjacent-items",
        "ordered-list",
        "stars",
        "underscores",
        "html-heading",
        "html-block",
        "script",
        "style",
    ],
)
def test_inline_spans_do_not_hide_prose_across_markdown_blocks(body: str) -> None:
    document = parse_markdown(body)
    assert "\u200b" in document.markup
    report = check_description(body, DescriptionPolicy())
    codes = [item.code for item in report.diagnostics]
    assert "description.invisible-character" in codes
    assert codes.count("description.backtick") == 2


def test_inline_spans_still_protect_literals_across_adjacent_prose_lines() -> None:
    body = "Explain `literal data\n\u200bwith hidden text` safely."
    document = parse_markdown(body)
    assert "\u200b" not in document.markup
    assert check_description(body, DescriptionPolicy()).valid
