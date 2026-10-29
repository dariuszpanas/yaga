"""Pure basic-text and explicit-template description checks."""

from __future__ import annotations

import unicodedata

from yaga.descriptions.hygiene import check_hygiene
from yaga.descriptions.markdown import (
    Heading,
    MarkdownDocument,
    MarkdownLine,
    normalize_description,
    parse_markdown,
)
from yaga.descriptions.models import (
    MAX_DIAGNOSTICS,
    MAX_HEADING_CHARACTERS,
    MAX_TEMPLATE_HEADINGS,
    DescriptionDiagnostic,
    DescriptionPolicy,
    DescriptionReport,
)
from yaga.errors import ConfigurationError, InputError


def check_description(
    body: str, policy: DescriptionPolicy, *, template: str | None = None
) -> DescriptionReport:
    """Check one explicit description without performing I/O or spelling execution."""
    if not isinstance(policy, DescriptionPolicy):
        raise ValueError("description checker requires a DescriptionPolicy")
    text = normalize_description(body)
    document = parse_markdown(text)
    expected = _template(template, policy) if template is not None else None
    placeholders = frozenset(value.casefold() for value in policy.placeholders)
    diagnostics: list[DescriptionDiagnostic] = []
    diagnostic_count = 0

    def add(code: str, message: str, line: int = 1, column: int = 1) -> None:
        nonlocal diagnostic_count
        diagnostic_count += 1
        if len(diagnostics) < MAX_DIAGNOSTICS:
            diagnostics.append(DescriptionDiagnostic(code, message, line, column))

    words_by_line = tuple(_word_count(line, placeholders) for line in document.lines)
    word_count = sum(words_by_line)
    if word_count == 0:
        add("description.required", "description must contain meaningful prose beyond its headings")
    elif word_count < policy.min_words:
        add(
            "description.word-count",
            f"description has {word_count} prose words; at least {policy.min_words} are required",
        )
    if len(text) > policy.max_length:
        add(
            "description.length",
            f"description has {len(text)} characters; at most {policy.max_length} are allowed",
        )
    for number, message in document.issues:
        add("description.markdown", message, number)
    for heading in document.headings:
        if not heading.text:
            add("description.heading-empty", "heading text must not be empty", heading.line)
        if heading.level < policy.heading_min_level:
            add(
                "description.heading-level",
                f"H{heading.level} is not allowed; use H{policy.heading_min_level} or deeper",
                heading.line,
            )
        if len(heading.text) > policy.heading_max_length:
            add(
                "description.heading-length",
                f"heading has {len(heading.text)} characters; at most "
                f"{policy.heading_max_length} are allowed; move details into a paragraph",
                heading.line,
            )
    for tag in document.html_headings:
        add(
            "description.html-heading",
            "raw HTML heading tags are not allowed; use Markdown section headings",
            tag.line,
            tag.column,
        )
    for diagnostic in check_hygiene(document.markup):
        add(diagnostic.code, diagnostic.message, diagnostic.line, diagnostic.column)
    for line in document.lines:
        if line.heading is None and line.content.casefold() in placeholders:
            add(
                "description.placeholder",
                "replace the placeholder with description content",
                line.number,
            )
        if policy.require_checked_tasks and line.unchecked_task:
            add("description.unchecked-task", "description contains an unchecked task", line.number)
    if expected is not None:
        positions: list[int] = []
        for required in expected.headings:
            matched = tuple(
                heading for heading in document.headings if heading.identity == required.identity
            )
            label = f"{'#' * required.level} {required.text}"
            if not matched:
                add(
                    "description.section-required", f"required template heading is missing: {label}"
                )
                continue
            if len(matched) > 1:
                add(
                    "description.section-duplicate",
                    f"required template heading occurs more than once: {label}",
                    matched[1].line,
                )
            heading = matched[0]
            if positions and heading.line < positions[-1]:
                add(
                    "description.section-order",
                    f"template heading is out of order: {label}",
                    heading.line,
                )
            positions.append(heading.line)
            section = _section(document, heading)
            if not any(words_by_line[line.number - 1] for line in section):
                add(
                    "description.section-empty",
                    f"template section requires meaningful prose: {label}",
                    heading.line,
                )
                continue
            prompt = _section(expected, required)
            original = _content_identity(prompt, placeholders)
            if original and original == _content_identity(section, placeholders):
                add(
                    "description.template-unchanged",
                    f"replace the unchanged template content beneath: {label}",
                    heading.line,
                )
    return DescriptionReport(
        policy=policy,
        diagnostics=tuple(diagnostics),
        diagnostic_count=diagnostic_count,
        word_count=word_count,
        character_count=len(text),
        template_checked=expected is not None,
    )


def _template(template: str, policy: DescriptionPolicy) -> MarkdownDocument:
    try:
        text = normalize_description(template)
    except InputError as error:
        raise ConfigurationError(f"invalid description template: {error}") from error
    document = parse_markdown(text)
    if document.issues:
        raise ConfigurationError(
            "description template has an unclosed comment or fenced code block"
        )
    hygiene = next(check_hygiene(document.markup), None)
    if hygiene is not None:
        raise ConfigurationError(f"invalid description template: {hygiene.code}: {hygiene.message}")
    if document.html_headings:
        raise ConfigurationError(
            "description templates must use Markdown headings, not raw HTML heading tags"
        )
    if not 1 <= len(document.headings) <= MAX_TEMPLATE_HEADINGS:
        raise ConfigurationError("description template requires 1 through 64 Markdown headings")
    identities: set[tuple[int, str]] = set()
    for heading in document.headings:
        if (
            not heading.text
            or len(heading.text) > MAX_HEADING_CHARACTERS
            or any(unicodedata.category(character) in {"Cc", "Cs"} for character in heading.text)
        ):
            raise ConfigurationError(
                "template headings require 1 through 256 characters without controls"
            )
        if heading.identity in identities:
            raise ConfigurationError("description template contains duplicate heading identities")
        if heading.level < policy.heading_min_level:
            raise ConfigurationError(
                "description template heading conflicts with heading-min-level"
            )
        if len(heading.text) > policy.heading_max_length:
            raise ConfigurationError(
                "description template heading conflicts with heading-max-length"
            )
        identities.add(heading.identity)
    return document


def _word_count(line: MarkdownLine, placeholders: frozenset[str]) -> int:
    if line.unchecked_task or line.content.casefold() in placeholders:
        return 0
    return sum(any(character.isalnum() for character in word) for word in line.content.split())


def _section(document: MarkdownDocument, heading: Heading) -> tuple[MarkdownLine, ...]:
    end = next(
        (
            item.line - 1
            for item in document.headings
            if item.line > heading.line and item.level <= heading.level
        ),
        len(document.lines),
    )
    return document.lines[(heading.end_line or heading.line) : end]


def _content_identity(
    lines: tuple[MarkdownLine, ...], placeholders: frozenset[str]
) -> tuple[str, ...]:
    return tuple(
        word for line in lines if _word_count(line, placeholders) for word in line.content.split()
    )
