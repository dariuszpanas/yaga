"""A bounded line-oriented Markdown subset for description structure.

ATX and top-level Setext headings, HTML heading tags, comments, code blocks,
backtick spans, list prefixes, and task markers have structural meaning.
This deliberately does not implement a complete Markdown renderer.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from html.parser import HTMLParser

from yaga.descriptions.models import MAX_DESCRIPTION_BYTES
from yaga.errors import InputError

_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_CLOSING_HASHES = re.compile(r"[ \t]+#+[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^(?:[-+*]|[0-9]{1,9}[.)])[ \t]+(.*)$")
_TASK = re.compile(r"^\[([ xX])\](?:[ \t]+(.*)|[ \t]*)$")
_BACKTICKS = re.compile(r"`+")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_THEMATIC = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})$")
_CONTAINER = re.compile(r"^ {0,3}(?:>|(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$))")
_HTML_BLOCK = re.compile(
    r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|"
    r"colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|"
    r"frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|"
    r"noframes|ol|optgroup|option|p|param|pre|script|search|section|style|summary|table|tbody|"
    r"td|textarea|tfoot|th|thead|title|tr|track|ul)(?:[ \t/>]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Heading:
    """One Markdown heading; lines use one-based input coordinates."""

    level: int
    text: str
    line: int
    end_line: int | None = None

    @property
    def identity(self) -> tuple[int, str]:
        """Return the exact, case-sensitive heading identity."""
        return self.level, self.text


@dataclass(frozen=True, slots=True)
class MarkdownLine:
    """Visible text and prose contribution from a single input line."""

    number: int
    visible: str
    content: str
    heading: Heading | None = None
    unchecked_task: bool = False


@dataclass(frozen=True, slots=True)
class HtmlHeading:
    """One raw HTML heading tag, including closing tags."""

    name: str
    line: int
    column: int
    closing: bool = False


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    """Line-oriented structure with masked spelling input and parse issues."""

    lines: tuple[MarkdownLine, ...]
    headings: tuple[Heading, ...]
    spelling: str
    issues: tuple[tuple[int, str], ...]
    html_headings: tuple[HtmlHeading, ...] = ()
    markup: str = ""


def normalize_description(text: str) -> str:
    """Validate the hard UTF-8 boundary and normalize only line endings."""
    if not isinstance(text, str):
        raise InputError("description must be a string")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError("description is not valid UTF-8") from error
    if len(encoded) > MAX_DESCRIPTION_BYTES:
        raise InputError(f"description exceeds the hard {MAX_DESCRIPTION_BYTES}-byte limit")
    for index, character in enumerate(text):
        bare_cr = character == "\r" and text[index + 1 : index + 2] != "\n"
        if bare_cr or (
            unicodedata.category(character) == "Cc" and character not in {"\t", "\r", "\n"}
        ):
            line = text.count("\n", 0, index) + 1
            detail = (
                "bare carriage return; use LF or CRLF"
                if bare_cr
                else "unsupported control character"
            )
            raise InputError(
                f"description contains U+{ord(character):04X} at line {line}: {detail}"
            )
    return text.replace("\r\n", "\n")


def parse_markdown(text: str) -> MarkdownDocument:
    """Parse a validated, newline-normalized description in linear input work."""
    lines: list[MarkdownLine] = []
    headings: list[Heading] = []
    spelling: list[str] = []
    html_input: list[str] = []
    prose_lines: list[bool] = []
    comment_line: int | None = None
    fence_line: int | None = None
    fence_character = ""
    fence_size = 0
    raw_lines = text.split("\n")
    starts: list[int] = []
    position = 0
    for raw in raw_lines:
        starts.append(position)
        position += len(raw) + 1
    inline_closers = _inline_closers(raw_lines, starts)
    inline_end = 0
    indented_code = False
    paragraph_open = False
    html_block = False
    for number, (raw, start) in enumerate(zip(raw_lines, starts, strict=True), start=1):
        code_line = False
        has_indent = _indent_width(raw) >= 4
        if not raw.strip() and indented_code:
            code_line = True
        elif has_indent and (indented_code or not paragraph_open) and not html_block:
            code_line = comment_line is None and fence_line is None
            indented_code = code_line
        else:
            indented_code = False
        if fence_line is not None:
            match = _FENCE.fullmatch(raw)
            if (
                match is not None
                and match[1][0] == fence_character
                and len(match[1]) >= fence_size
                and not match[2].strip()
            ):
                fence_line = None
            visible = _mask(raw)
            html_visible = visible
        elif code_line:
            visible = _mask(raw)
            html_visible = visible
        elif comment_line is None and (match := _opening_fence(raw)) is not None:
            fence_character = match[0]
            fence_size = match[1]
            fence_line = number
            visible = _mask(raw)
            html_visible = visible
        else:
            visible, html_visible, comment_line, inline_end = _without_comments(
                raw, number, comment_line, start, inline_end, inline_closers
            )
        spelling.append(visible)
        html_input.append(html_visible)
        prose_lines.append(bool(visible.strip()) and not code_line)
        if not visible.strip():
            paragraph_open = False
            html_block = False
        elif _HTML_BLOCK.match(visible) is not None:
            html_block = True
            paragraph_open = False
        elif html_block or _CONTAINER.match(visible) is not None:
            paragraph_open = False
        elif _SETEXT.fullmatch(visible) is not None and paragraph_open:
            paragraph_open = False
        elif _THEMATIC.fullmatch(visible) is not None:
            paragraph_open = False
        else:
            paragraph_open = True
        match = _HEADING.fullmatch(visible)
        if match is not None:
            paragraph_open = False
            title = _CLOSING_HASHES.sub("", match[2] or "").strip()
            if title and set(title) == {"#"}:
                title = ""
            heading = Heading(level=len(match[1]), text=title, line=number)
            headings.append(heading)
            lines.append(MarkdownLine(number, visible, "", heading))
            continue
        content = visible.strip()
        list_match = _LIST.fullmatch(content)
        unchecked = False
        if list_match is not None:
            content = list_match[1].strip()
            task = _TASK.fullmatch(content)
            if task is not None:
                content = (task[2] or "").strip()
                unchecked = task[1] == " "
        lines.append(MarkdownLine(number, visible, content, unchecked_task=unchecked))
    issues: list[tuple[int, str]] = []
    if comment_line is not None:
        issues.append((comment_line, "HTML comment is not closed"))
    if fence_line is not None:
        issues.append((fence_line, "fenced code block is not closed"))
    lines, headings = _setext_headings(lines, prose_lines)
    markup = "\n".join(html_input)
    return MarkdownDocument(
        tuple(lines),
        tuple(headings),
        "\n".join(spelling),
        tuple(issues),
        _html_headings(markup),
        markup,
    )


def spelling_text(body: str) -> str:
    """Mask comments and fenced blocks, preserving newlines and UTF-8 byte offsets."""
    return parse_markdown(normalize_description(body)).spelling


def _mask(value: str) -> str:
    return " " * len(value.encode("utf-8"))


def _indent_width(value: str) -> int:
    width = 0
    for character in value:
        if character == " ":
            width += 1
        elif character == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _opening_fence(raw: str) -> tuple[str, int] | None:
    match = _FENCE.fullmatch(raw)
    if match is None or (match[1][0] == "`" and "`" in match[2]):
        return None
    return match[1][0], len(match[1])


def _inline_closers(raw_lines: list[str], starts: list[int]) -> dict[int, int]:
    """Index next exact backtick runs once, bounded by prose block boundaries.

    A span may cross adjacent prose lines, but never blank lines, headings,
    fences, thematic breaks, container markers, or raw HTML block boundaries.
    Runs inside a consumed span are never parsed again.
    """
    closers: dict[int, int] = {}
    following: dict[int, int] = {}
    for raw, start in reversed(tuple(zip(raw_lines, starts, strict=True))):
        boundary = (
            not raw.strip()
            or _HEADING.fullmatch(raw) is not None
            or _opening_fence(raw) is not None
            or _SETEXT.fullmatch(raw) is not None
            or _THEMATIC.fullmatch(raw) is not None
            or _CONTAINER.match(raw) is not None
            or _HTML_BLOCK.match(raw) is not None
            or raw.lstrip(" ").startswith(("<!--", "<?", "<!"))
        )
        if boundary:
            following.clear()
        for match in reversed(tuple(_BACKTICKS.finditer(raw))):
            size = len(match[0])
            if size in following:
                closers[start + match.start()] = following[size]
            following[size] = start + match.end()
        if boundary:
            following.clear()
    return closers


def _without_comments(
    raw: str,
    number: int,
    comment_line: int | None,
    start: int,
    inline_end: int,
    inline_closers: dict[int, int],
) -> tuple[str, str, int | None, int]:
    parts: list[str] = []
    html_parts: list[str] = []
    offset = 0
    while offset < len(raw):
        if start + offset < inline_end:
            end = min(inline_end - start, len(raw))
            parts.append(raw[offset:end])
            html_parts.append(_mask(raw[offset:end]))
            offset = end
        elif comment_line is not None:
            end = raw.find("-->", offset)
            if end < 0:
                parts.append(_mask(raw[offset:]))
                html_parts.append(_mask(raw[offset:]))
                break
            parts.append(_mask(raw[offset : end + 3]))
            html_parts.append(_mask(raw[offset : end + 3]))
            offset = end + 3
            comment_line = None
        elif raw.startswith("<!--", offset):
            comment_line = number
        elif raw[offset] == "\\" and raw[offset + 1 : offset + 2] in {"\\", "`", "<"}:
            parts.append(raw[offset : offset + 2])
            html_parts.append(_mask(raw[offset : offset + 2]))
            offset += 2
        elif raw[offset] == "`":
            end = offset + 1
            while end < len(raw) and raw[end] == "`":
                end += 1
            inline_end = inline_closers.get(start + offset, 0)
            if inline_end <= start + offset:
                parts.append(raw[offset:end])
                html_parts.append(raw[offset:end])
                offset = end
        else:
            parts.append(raw[offset])
            html_parts.append(raw[offset])
            offset += 1
    return "".join(parts), "".join(html_parts), comment_line, inline_end


def _setext_headings(
    lines: list[MarkdownLine], prose_lines: list[bool]
) -> tuple[list[MarkdownLine], list[Heading]]:
    """Collect top-level paragraph-plus-underline headings in one forward pass."""
    paragraph: list[int] = []
    container = False
    for index, line in enumerate(lines):
        visible = line.visible
        if not visible.strip():
            paragraph.clear()
            container = False
            continue
        if line.heading is not None:
            paragraph.clear()
            container = False
            continue
        underline = _SETEXT.fullmatch(visible)
        if underline is not None:
            if paragraph and not container:
                first = paragraph[0]
                title = " ".join(lines[item].visible.strip() for item in paragraph)
                heading = Heading(
                    level=1 if underline[1][0] == "=" else 2,
                    text=title,
                    line=lines[first].number,
                    end_line=line.number,
                )
                for item in paragraph:
                    lines[item] = replace(
                        lines[item], content="", heading=heading if item == first else None
                    )
                lines[index] = replace(line, content="")
            paragraph.clear()
            continue
        if _CONTAINER.match(visible) is not None or _HTML_BLOCK.match(visible) is not None:
            paragraph.clear()
            container = True
            continue
        if container or not prose_lines[index] or _THEMATIC.fullmatch(visible) is not None:
            paragraph.clear()
            continue
        paragraph.append(index)
    return lines, [line.heading for line in lines if line.heading is not None]


class _HeadingTagParser(HTMLParser):
    """Inspect a code/comment-masked stream without rendering or fetching HTML."""

    def __init__(self, text: str, first_line: int) -> None:
        super().__init__(convert_charrefs=False)
        self.first_line = first_line
        self.byte_columns: list[list[int]] = []
        for line in text.split("\n"):
            columns = [0]
            for character in line:
                columns.append(columns[-1] + len(character.encode("utf-8")))
            self.byte_columns.append(columns)
        self.headings: list[HtmlHeading] = []

    def set_cdata_mode(self, elem: str, *, escapable: bool = False) -> None:
        """Inspect tags inside script/style instead of treating them as opaque data."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._heading(tag, closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._heading(tag, closing=False)

    def handle_endtag(self, tag: str) -> None:
        self._heading(tag, closing=True)

    def _heading(self, tag: str, *, closing: bool) -> None:
        if tag not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return
        line, character_column = self.getpos()
        column = self.byte_columns[line - 1][character_column] + 1
        self.headings.append(HtmlHeading(tag, line + self.first_line - 1, column, closing))


def _html_headings(text: str) -> tuple[HtmlHeading, ...]:
    """Stop unfinished tags at paragraph breaks before scanning the next block."""
    headings: list[HtmlHeading] = []
    block: list[str] = []
    first_line = 1
    for number, line in enumerate((*text.split("\n"), ""), start=1):
        if line.strip():
            if not block:
                first_line = number
            block.append(line)
            continue
        if block:
            source = "\n".join(block)
            parser = _HeadingTagParser(source, first_line)
            parser.feed(source)
            parser.close()
            headings.extend(parser.headings)
            block.clear()
    return tuple(headings)
