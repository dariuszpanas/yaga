"""Rendered heading mistakes must not satisfy PR description quality policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.descriptions.checker import check_description
from yaga.descriptions.models import DescriptionPolicy
from yaga.descriptions.policy import load_description_policy
from yaga.errors import ConfigurationError


def _codes(body: str, policy: DescriptionPolicy | None = None) -> set[str]:
    return {
        finding.code
        for finding in check_description(body, policy or DescriptionPolicy()).diagnostics
    }


@pytest.mark.parametrize("tail", ["", "\n\nDone."], ids=["heading-only", "token-prose"])
def test_massive_h1_is_rejected_even_with_a_token_paragraph(tail: str) -> None:
    body = "# " + "Long generated explanation " * 50 + tail
    codes = _codes(body)

    assert {"description.heading-level", "description.heading-length"} <= codes
    assert ("description.required" in codes) is (not tail)


def test_lower_heading_level_cannot_bypass_the_heading_length_limit() -> None:
    assert _codes("## " + "Long generated explanation " * 50 + "\n\nDone.") == {
        "description.heading-length"
    }


def test_normal_sections_and_a_plain_description_remain_valid() -> None:
    for body in (
        "Explain the changed behavior and how it was tested.",
        "## Summary\nFix the request parser.\n\n## Validation\nRun the parser tests.",
    ):
        assert check_description(body, DescriptionPolicy()).valid


def test_h1_can_be_explicitly_allowed_without_disabling_other_rules() -> None:
    policy = DescriptionPolicy(heading_min_level=1)

    assert check_description("# Summary\nExplain the change.", policy).valid
    assert "description.required" in _codes("# Summary", policy)
    assert _codes("# " + "x" * 121 + "\n\nExplanation.", policy) == {"description.heading-length"}


def test_heading_limit_counts_title_characters_without_markers() -> None:
    policy = DescriptionPolicy(heading_max_length=4)

    assert check_description("  ## 字字字字 ###\nUseful prose.", policy).valid
    report = check_description("  ## 字字字字字 ###\nUseful prose.", policy)
    assert [(item.code, item.line) for item in report.diagnostics] == [
        ("description.heading-length", 1)
    ]


def test_empty_markdown_heading_has_its_own_diagnostic() -> None:
    assert "description.heading-empty" in _codes("##\n\nUseful prose.")


def test_many_heading_findings_retain_exact_omitted_counts() -> None:
    report = check_description("# Heading\n" * 300 + "\nUseful prose.", DescriptionPolicy())

    assert report.diagnostic_count == 300
    assert len(report.diagnostics) == 256
    assert report.omitted_diagnostic_count == 44
    assert {item.code for item in report.diagnostics} == {"description.heading-level"}


@pytest.mark.parametrize("underline", ["===", "---"], ids=["h1", "h2"])
def test_setext_heading_text_is_not_description_prose(underline: str) -> None:
    report = check_description("Generated heading\n" + underline, DescriptionPolicy())

    assert report.word_count == 0
    assert "description.required" in {item.code for item in report.diagnostics}
    if underline == "===":
        assert "description.heading-level" in {item.code for item in report.diagnostics}


def test_multiline_setext_heading_does_not_pass_with_a_token_paragraph() -> None:
    body = ("Generated explanation on this line\n" * 8) + "===\n\nDone."
    report = check_description(body, DescriptionPolicy())

    assert report.word_count == 1
    assert {"description.heading-level", "description.heading-length"} <= {
        item.code for item in report.diagnostics
    }


def test_setext_h2_sections_can_match_an_atx_template() -> None:
    body = "Summary\n---\nExplain the request change.\n\nValidation\n---\nRan the tests."
    report = check_description(body, DescriptionPolicy(), template="## Summary\n\n## Validation")

    assert report.valid
    assert report.word_count == 7


def test_thematic_break_after_blank_line_does_not_turn_prose_into_a_heading() -> None:
    report = check_description("Explain the change.\n\n---\n\nRan the tests.", DescriptionPolicy())

    assert report.valid
    assert report.word_count == 6


@pytest.mark.parametrize(
    "body",
    [
        "<h1>Generated explanation fills the entire description.</h1>",
        '<H1 class="title">Generated explanation fills the entire description.</H1>',
        '<h2\n class="title">Generated explanation fills the entire description.</h2>',
        '<h3 title="a > b">Generated explanation.</h3>',
        "<details><summary>Details</summary><h4>Generated explanation.</h4></details>",
        "<h5>Unclosed generated heading",
        "</h6>Useful prose.",
    ],
    ids=[
        "h1",
        "uppercase-attributes",
        "multiline",
        "quoted-angle",
        "nested",
        "unclosed",
        "orphan-close",
    ],
)
def test_raw_html_heading_tags_are_rejected(body: str) -> None:
    assert "description.html-heading" in _codes(body)


@pytest.mark.parametrize(
    "body",
    [
        "Document `<h1>literal</h1>` in the guide.",
        "Document ``<h1>literal ` code</h1>`` in the guide.",
        "Document \\<h1>literal markup in the guide.",
        "<!-- <h1>Hidden heading</h1> -->\nUseful prose.",
        "```html\n<h1>Example markup</h1>\n```\nUseful prose.",
        "<details>\n<summary>Evidence</summary>\n\nTests passed.\n</details>",
    ],
    ids=["inline", "backtick-run", "escaped", "comment", "fence", "details-summary"],
)
def test_literal_or_ordinary_markup_does_not_trigger_html_heading_findings(body: str) -> None:
    assert "description.html-heading" not in _codes(body)


def test_a_whole_fenced_markdown_response_still_requires_real_description_prose() -> None:
    body = "```markdown\n## Summary\nUseful explanation.\n## Validation\nTests passed.\n```"
    assert "description.required" in _codes(body)


@pytest.mark.parametrize(
    ("policy", "template"),
    [
        (DescriptionPolicy(), "# Summary"),
        (DescriptionPolicy(), "## " + "x" * 121),
        (DescriptionPolicy(heading_min_level=3), "## Summary"),
        (DescriptionPolicy(), "Summary\n===\n"),
        (DescriptionPolicy(), "## Summary\n<h1>Instructions</h1>"),
    ],
    ids=["h1", "long-title", "minimum-level", "setext-h1", "html-heading"],
)
def test_templates_cannot_require_forbidden_heading_structure(
    policy: DescriptionPolicy, template: str
) -> None:
    with pytest.raises(ConfigurationError):
        check_description("Useful explanation.", policy, template=template)


def test_heading_policy_overrides_apply_to_both_template_and_description() -> None:
    report = check_description(
        "# Summary\nExplain the change.",
        DescriptionPolicy(heading_min_level=1, heading_max_length=7),
        template="# Summary",
    )

    assert report.valid


@pytest.mark.parametrize(
    "setting",
    [
        "heading-min-level = 0",
        "heading-min-level = 7",
        "heading-min-level = true",
        "heading-min-level = 2.0",
        'heading-min-level = "2"',
        "heading-min-level = []",
        "heading-max-length = 0",
        "heading-max-length = 257",
        "heading-max-length = false",
        "heading-max-length = 120.0",
        'heading-max-length = "120"',
        "heading-max-length = []",
    ],
)
def test_heading_policy_rejects_wrong_types_and_out_of_range_values(
    tmp_path: Path, setting: str
) -> None:
    path = tmp_path / "description-policy.toml"
    path.write_text("description-policy-version = 1\n" + setting + "\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_description_policy(path)


def test_heading_configuration_round_trips_exact_limits(tmp_path: Path) -> None:
    path = tmp_path / "description-policy.toml"
    path.write_text(
        "description-policy-version = 1\nheading-min-level = 6\nheading-max-length = 256\n",
        encoding="utf-8",
    )

    loaded = load_description_policy(path)
    assert loaded.policy.heading_min_level == 6
    assert loaded.policy.heading_max_length == 256
    assert check_description("###### " + "x" * 256 + "\n\nUseful prose.", loaded.policy).valid
