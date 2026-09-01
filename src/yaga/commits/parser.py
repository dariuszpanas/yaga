"""Pure parser for Conventional Commits 1.0.0 message structure."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator

from yaga.commits.models import CommitFooter, ParsedCommit

MAX_MESSAGE_BYTES = 1024 * 1024

_HEADER = re.compile(
    r"^(?P<type>[^\s()!:]+)(?:\((?P<scope>[^()\r\n]+)\))?"
    r"(?P<breaking>!)?: (?P<description>\S(?:.*\S)?)$"
)
_FOOTER_START = re.compile(
    r"^(?P<token>BREAKING CHANGE|[A-Za-z0-9-]+)"
    r"(?P<separator>: | #)\S"
)
_BREAKING_TOKENS = frozenset({"BREAKING CHANGE", "BREAKING-CHANGE"})


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
    body_lines, footer_lines, footer_start = _split_body_and_footers(content)
    footer_start_line = content_start + footer_start + 1 if footer_start is not None else None
    breaking_footer = footer_start_line is not None and any(
        footer.token in _BREAKING_TOKENS and footer.separator == ":"
        for footer in iter_footer_starts(
            footer_lines,
            start_line=footer_start_line,
        )
    )
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
        footer_start_line=footer_start_line,
    )


def _split_body_and_footers(
    lines: list[str],
) -> tuple[list[str], list[str], int | None]:
    """Split the first boundary-delimited footer token and its complete suffix."""
    footer_start: int | None = None
    at_paragraph_start = True
    for index, line in enumerate(lines):
        if at_paragraph_start and _FOOTER_START.match(line) is not None:
            footer_start = index
            break
        at_paragraph_start = line == ""
    if footer_start is None:
        return lines, [], None

    body = lines[:footer_start]
    while body and body[-1] == "":
        body.pop()
    # Footer values may contain newlines, including empty lines. Once a valid
    # boundary token starts the footer block, its entire remaining suffix is
    # therefore footer content; a later valid token terminates the preceding
    # value rather than returning to body parsing.
    return body, lines[footer_start:], footer_start


def iter_footer_starts(
    lines: Iterable[str],
    *,
    start_line: int,
) -> Iterator[CommitFooter]:
    """Yield exact footer token starts with absolute one-based source lines."""
    for offset, line in enumerate(lines):
        match = _FOOTER_START.match(line)
        if match is None:
            continue
        separator = ":" if match.group("separator") == ": " else "#"
        yield CommitFooter(
            token=match.group("token"),
            separator=separator,
            line=start_line + offset,
        )


def _is_component_token(value: str) -> bool:
    """Reject whitespace and invisible control characters in type/scope tokens."""
    return bool(value) and not any(
        character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    )
