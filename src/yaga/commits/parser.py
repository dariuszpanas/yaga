"""Pure parser for Conventional Commits 1.0.0 message structure."""

from __future__ import annotations

import re
import unicodedata

from yaga.commits.models import ParsedCommit

MAX_MESSAGE_BYTES = 1024 * 1024

_HEADER = re.compile(
    r"^(?P<type>[^\s()!:]+)(?:\((?P<scope>[^()\r\n]+)\))?"
    r"(?P<breaking>!)?: (?P<description>\S(?:.*\S)?)$"
)
_FOOTER_START = re.compile(r"^(?:BREAKING CHANGE|[A-Za-z0-9-]+)(?:: | #)\S")
_BREAKING_FOOTER = re.compile(r"^BREAKING(?: CHANGE|-CHANGE): \S")


def normalize_message(message: str) -> str:
    """Normalize line endings while preserving meaningful whitespace."""
    return message.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def header_from(message: str) -> str:
    """Return the first normalized line without silently trimming it."""
    normalized = normalize_message(message)
    return normalized.split("\n", 1)[0] if normalized else ""


def parse_message(message: str) -> ParsedCommit | None:
    """Parse a message, returning ``None`` when its header is malformed."""
    normalized = normalize_message(message)
    lines = normalized.split("\n") if normalized else [""]
    header = lines[0]
    match = _HEADER.fullmatch(header)
    if match is None:
        return None
    commit_type = match.group("type")
    scope = match.group("scope")
    if not _is_component_token(commit_type) or (
        scope is not None and not _is_component_token(scope)
    ):
        return None

    separator_valid = len(lines) == 1 or lines[1] == ""
    content_start = 2 if len(lines) > 1 and lines[1] == "" else 1
    content = lines[content_start:]
    while content and content[-1] == "":
        content.pop()
    body_lines, footer_lines = _split_body_and_footers(content)
    breaking_footer = any(_BREAKING_FOOTER.match(line) for line in footer_lines)
    breaking_header = match.group("breaking") == "!"

    return ParsedCommit(
        message=normalized,
        header=header,
        commit_type=commit_type,
        scope=scope,
        description=match.group("description"),
        breaking=breaking_header or breaking_footer,
        breaking_header=breaking_header,
        breaking_footer=breaking_footer,
        separator_valid=separator_valid,
        body="\n".join(body_lines).strip("\n"),
        body_lines=tuple(body_lines),
        body_start_line=content_start + 1,
        footer_lines=tuple(footer_lines),
    )


def _split_body_and_footers(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split a final Git-trailer-style footer block from free-form body text."""
    if not lines:
        return [], []
    paragraph_start = 0
    for index, line in enumerate(lines):
        if line == "":
            paragraph_start = index + 1
    candidate = lines[paragraph_start:]
    if not _is_footer_block(candidate):
        return lines, []
    body = lines[:paragraph_start]
    while body and body[-1] == "":
        body.pop()
    return body, candidate


def _is_footer_block(lines: list[str]) -> bool:
    # The Conventional Commits grammar allows footer values to contain newlines
    # without prescribing indentation. The final paragraph is therefore a footer
    # block when its first line starts a footer; later lines are either additional
    # footers or continuation text for a preceding value.
    return bool(lines and _FOOTER_START.match(lines[0]) is not None)


def _is_component_token(value: str) -> bool:
    """Reject whitespace and invisible control characters in type/scope tokens."""
    return bool(value) and not any(
        character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    )
