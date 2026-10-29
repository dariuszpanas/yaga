"""Source-located checks for hidden text and damaged Markdown serialization."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator

from yaga.descriptions.models import DescriptionDiagnostic

_ESCAPED_BREAK = re.compile(r"\\r\\n|\\n\\n")
_BACKTICKS = re.compile(r"`+")
# Emoji tag sequences have a visible black-flag base and a terminating cancel tag.
_EMOJI_TAG_SEQUENCE = re.compile("\U0001f3f4[\U000e0020-\U000e007e]{1,32}\U000e007f")


def check_hygiene(markup: str) -> Iterator[DescriptionDiagnostic]:
    """Inspect the parser's code/comment/literal-masked text in bounded work.

    Masked spans retain UTF-8 byte lengths and LF positions. Unicode joining
    characters, combining marks, variation selectors and emoji tags in a flag
    sequence retain their legitimate shaping roles.
    """
    for number, line in enumerate(markup.split("\n"), start=1):
        emoji_tags = {
            index
            for match in _EMOJI_TAG_SEQUENCE.finditer(line)
            for index in range(match.start() + 1, match.end())
        }
        columns = [1]
        for index, character in enumerate(line):
            column = columns[-1]
            columns.append(column + len(character.encode("utf-8")))
            if character == "\t":
                yield DescriptionDiagnostic(
                    "description.tab",
                    "unexpected U+0009 TAB in prose; use spaces or put literal data in code",
                    number,
                    column,
                )
            elif (
                (
                    unicodedata.category(character) == "Cf"
                    and character not in {"\u200c", "\u200d"}
                    and index not in emoji_tags
                )
                or unicodedata.category(character) in {"Zl", "Zp"}
                or character == "\u034f"
            ):
                name = unicodedata.name(character, "UNNAMED CHARACTER")
                yield DescriptionDiagnostic(
                    "description.invisible-character",
                    f"unexpected U+{ord(character):04X} {name}; remove hidden formatting from prose",
                    number,
                    column,
                )
        for match in _ESCAPED_BREAK.finditer(line):
            yield DescriptionDiagnostic(
                "description.escaped-newline",
                "literal escaped line breaks in prose; write real newlines or quote the sequence as code",
                number,
                columns[match.start()],
            )
        for match in _BACKTICKS.finditer(line):
            yield DescriptionDiagnostic(
                "description.backtick",
                "unmatched inline-code backtick delimiter; close it or escape a literal backtick",
                number,
                columns[match.start()],
            )
