"""Description structure, templates, and resource-limit regressions."""

from __future__ import annotations

from typing import Any

import pytest

from yaga.descriptions.checker import check_description
from yaga.descriptions.markdown import spelling_text
from yaga.descriptions.models import DescriptionPolicy
from yaga.errors import ConfigurationError, InputError


def _codes(body: str, *, template: str | None = None, **policy: Any) -> list[str]:
    report = check_description(body, DescriptionPolicy(**policy), template=template)
    return [item.code for item in report.diagnostics]


def test_plain_description_reports_counts() -> None:
    report = check_description(
        "Fix request validation.\n\nAdd regression tests.", DescriptionPolicy()
    )
    assert report.valid
    assert report.status == "passed"
    assert report.word_count == 6
    assert report.character_count == 46
    assert report.diagnostic_count == report.omitted_diagnostic_count == 0
    assert not report.template_checked


@pytest.mark.parametrize(
    "body",
    [
        "",
        " \n\t ",
        "## Summary\n\n## Testing",
        "<!-- Write a useful description here. -->",
        "```text\nwords in code\n```",
        "~~~\n## Ignored\n~~~",
        "---\n...\n🎉",
        "- [ ]\n- [x]",
        "- [ ] Add useful tests",
        "TODO\n- tbd",
    ],
)
def test_nonprose_does_not_fill_description_even_when_minimum_is_zero(body: str) -> None:
    assert "description.required" in _codes(body, min_words=0)


def test_words_are_unicode_whitespace_delimited_prose() -> None:
    body = "## Heading words\n<!-- ignored -->\nαβ\u2003字词\t123 🎉 ---\n- [x] Validated behavior"
    report = check_description(body, DescriptionPolicy(min_words=6))
    assert report.word_count == 5
    assert [item.code for item in report.diagnostics] == [
        "description.word-count",
        "description.tab",
    ]


def test_character_limit_uses_normalized_unicode_characters() -> None:
    report = check_description("字\r\n字\n字", DescriptionPolicy(max_length=4))
    assert report.character_count == 5
    assert [item.code for item in report.diagnostics] == ["description.length"]


@pytest.mark.parametrize(
    "body",
    ["x" * 65537, "字" * 21846, "\ud800", "hello\x00world", "hello\x1b[31m", "a\b"],
    ids=["byte-limit", "utf8-byte-limit", "surrogate", "nul", "escape", "backspace"],
)
def test_input_hard_bounds(body: str) -> None:
    with pytest.raises(InputError):
        check_description(body, DescriptionPolicy())


def test_exact_input_byte_boundary_is_accepted() -> None:
    assert check_description("a" * 65536, DescriptionPolicy()).valid


def test_placeholders_match_whole_lines_after_list_or_task_markers() -> None:
    body = "Changes are described.\nTODO\n- tbd\n1. TODO\n- [x] TBD\n- [ ] TODO"
    report = check_description(body, DescriptionPolicy())
    assert [item.line for item in report.diagnostics] == [2, 3, 4, 5, 6]
    assert {item.code for item in report.diagnostics} == {"description.placeholder"}
    assert report.word_count == 3


def test_placeholders_do_not_match_substrings_headings_comments_or_code() -> None:
    body = "## TODO\nFix TODO discovery.\n<!-- TODO -->\n```\nTBD\n```"
    assert check_description(body, DescriptionPolicy()).valid


def test_custom_placeholders_and_explicit_disable() -> None:
    assert "description.placeholder" in _codes("Replace this", placeholders=("replace this",))
    assert check_description("TODO", DescriptionPolicy(placeholders=())).valid


def test_unchecked_tasks_are_optionally_diagnosed_and_never_supply_prose() -> None:
    body = "- [ ] Untested change\n- [X] Verified path"
    report = check_description(body, DescriptionPolicy(require_checked_tasks=True))
    assert report.word_count == 2
    assert [(item.code, item.line) for item in report.diagnostics] == [
        ("description.unchecked-task", 1)
    ]
    assert check_description(body, DescriptionPolicy()).valid


def test_matching_template_headings_allow_new_content_and_extra_sections() -> None:
    template = "## Summary\nDescribe the change.\n## Testing\n<!-- Explain validation. -->"
    body = "## Summary\nFix request handling.\n### Details\nPreserve valid input.\n## Testing\nRun tests."
    report = check_description(body, DescriptionPolicy(), template=template)
    assert report.valid
    assert report.template_checked


def test_template_matching_is_exact_in_depth_and_case() -> None:
    body = "# Summary\nReal content\n## testing\nRun tests"
    assert _codes(body, template="## Summary\n## Testing") == [
        "description.heading-level",
        "description.section-required",
        "description.section-required",
    ]


def test_template_heading_order_duplicate_and_missing_are_independent() -> None:
    body = "## Testing\nRun tests\n## Summary\nReal changes\n## Testing\nOther tests"
    assert _codes(body, template="## Summary\n## Testing\n## Risks") == [
        "description.section-duplicate",
        "description.section-order",
        "description.section-required",
    ]


def test_each_required_section_needs_prose_not_unchecked_task_labels() -> None:
    body = "## Summary\n<!-- details -->\n### Details\n## Testing\n- [ ] Run tests\n## Extra\nReal content"
    report = check_description(body, DescriptionPolicy(), template="## Summary\n## Testing")
    assert [(item.code, item.line) for item in report.diagnostics] == [
        ("description.section-empty", 1),
        ("description.section-empty", 4),
    ]


def test_child_section_prose_fills_its_parent_but_next_peer_does_not() -> None:
    body = "## Summary\n### Details\nExplain changes\n## Testing\n### Results\nTests passed"
    assert check_description(body, DescriptionPolicy(), template="## Summary\n## Testing").valid


def test_copied_template_prompts_fail_per_section_ignoring_comments_and_spaces() -> None:
    template = "## Summary\nDescribe this change.\n## Testing\nExplain validation."
    body = "## Summary\nDescribe   this change. <!-- Added noise -->\n## Testing\nNew validation performed."
    report = check_description(body, DescriptionPolicy(), template=template)
    assert [(item.code, item.line) for item in report.diagnostics] == [
        ("description.template-unchanged", 1)
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "Describe this\nchange.",
        "Describe\n\nthis change.",
        "Describe\n<!-- noise -->\nthis change.",
    ],
)
def test_rewrapped_template_prompts_are_unchanged(prompt: str) -> None:
    assert _codes(f"## Summary\n{prompt}", template="## Summary\nDescribe this change.") == [
        "description.template-unchanged"
    ]


def test_comment_only_template_prompts_do_not_trigger_unchanged() -> None:
    assert check_description(
        "## Summary\nA useful change",
        DescriptionPolicy(),
        template="## Summary\n<!-- Instructions -->",
    ).valid


@pytest.mark.parametrize(
    "template",
    [
        "",
        "No headings here",
        "## Summary\n## Summary",
        "##",
        "## ###",
        "## " + "a" * 257,
        "## \u202eHidden",
        "## Summary\n<!-- unclosed",
        "## Summary\n```\nunclosed",
        "\ud800",
        "## Summary\n\x00",
        "x" * 65537,
        "\n".join(f"## Heading {i}" for i in range(65)),
    ],
    ids=[
        "empty",
        "no-headings",
        "duplicate",
        "empty-heading",
        "hashes-only",
        "heading-limit",
        "control",
        "comment",
        "fence",
        "surrogate",
        "nul",
        "byte-limit",
        "count-limit",
    ],
)
def test_malformed_or_ambiguous_templates_are_configuration_errors(template: str) -> None:
    with pytest.raises(ConfigurationError):
        check_description("Useful description", DescriptionPolicy(), template=template)


def test_fenced_and_commented_headings_do_not_participate_in_structure() -> None:
    body = "<!--\n## Testing\n-->\n```markdown\n## Testing\n```\n## Summary\nReal content"
    assert _codes(body, template="## Summary\n## Testing") == ["description.section-required"]


def test_comments_inside_fences_are_not_open_comments() -> None:
    body = "```html\n<!-- not closed\n```\nReal prose."
    assert check_description(body, DescriptionPolicy()).valid


@pytest.mark.parametrize(
    "prose",
    [
        "Document the `<!--` marker.",
        "Document the ``<!-- with `nested` backticks`` marker.",
        "Document the ``<!-- with\na wrapped marker`` here.",
        "Document `字<!--` and <!-- hidden --> visible text.",
        "Document the \\` escape and `<!--` marker.",
    ],
)
def test_matched_backtick_spans_keep_comment_markers_literal(prose: str) -> None:
    body = f"## Summary\n{prose}\n## Validation\nTests passed."
    assert check_description(body, DescriptionPolicy(), template="## Summary\n## Validation").valid
    assert "<!--" in spelling_text(body)


def test_unmatched_backticks_do_not_hide_actual_comments() -> None:
    body = "A ` literal <!-- hidden --> and useful prose."
    report = check_description(body, DescriptionPolicy())
    assert report.word_count == 5
    assert [item.code for item in report.diagnostics] == ["description.backtick"]
    assert "hidden" not in spelling_text(body)


@pytest.mark.parametrize("boundary", ["", "## Validation", "```"])
def test_inline_code_cannot_cross_block_boundaries(boundary: str) -> None:
    body = f"A ` literal\n{boundary}\n<!-- hidden --> `\nReal content"
    assert "hidden" not in spelling_text(body)


def test_fence_info_comment_markers_do_not_open_comments() -> None:
    body = "## Summary\nA useful change.\n```text <!--\ncode\n```\n## Validation\nTests passed."
    assert check_description(body, DescriptionPolicy(), template="## Summary\n## Validation").valid


def test_short_fence_does_not_close_longer_fence() -> None:
    body = "````\n```\n## Summary\n````\nReal prose."
    assert _codes(body, template="## Summary") == ["description.section-required"]


@pytest.mark.parametrize("body", ["Real prose\n<!-- hidden", "Real prose\n```\ncode"])
def test_unclosed_constructs_are_findings_in_descriptions(body: str) -> None:
    assert _codes(body) == ["description.markdown"]


def test_optional_closing_hashes_and_indentation_have_atx_semantics() -> None:
    assert check_description(
        "   ## Summary ###\nUseful change",
        DescriptionPolicy(),
        template="## Summary",
    ).valid


def test_spelling_mask_preserves_line_numbers_and_utf8_offsets() -> None:
    body = "## Summary\nA <!-- 字 --> typo\n```\nmisspeling\n```\nVisible text"
    masked = spelling_text(body)
    assert len(masked.encode()) == len(body.encode())
    assert masked.count("\n") == body.count("\n")
    assert masked.splitlines()[1].index("typo") == len("A <!-- 字 --> ".encode())
    assert "misspeling" not in masked
    assert "## Summary" in masked


def test_diagnostic_storage_bound_retains_exact_total() -> None:
    report = check_description("Actual content\n" + "TODO\n" * 300, DescriptionPolicy())
    assert report.diagnostic_count == 300
    assert len(report.diagnostics) == 256
    assert report.omitted_diagnostic_count == 44
    assert not report.valid
    assert report.status == "failed"
