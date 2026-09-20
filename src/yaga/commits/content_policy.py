"""Bounded literal reference and footer-value checks for complete messages."""

import re

from yaga.commits.models import CommitPolicy, Diagnostic, ParsedCommit
from yaga.commits.parser import iter_footer_starts

_WRAPPERS = "()[]{}<>,.;:!?\"'"
_NUMBER = re.compile(r"[1-9][0-9]{0,19}\Z")


def has_issue_reference(message: str, prefixes: tuple[str, ...]) -> bool:
    """Match a whitespace token with optional punctuation wrappers and a positive ASCII ID."""
    # A bounded trie avoids rescanning the complete message for each configured prefix.
    trie: dict = {}
    for prefix in prefixes:
        node = trie
        for char in prefix:
            node = node.setdefault(char, {})
        node[None] = True
    for token in message.split():
        token = token.strip(_WRAPPERS)
        if len(token) > 84:  # 64-character prefix plus at most 20 decimal digits.
            continue
        node = trie
        for index, char in enumerate(token):
            node = node.get(char)
            if node is None:
                break
            if None in node and _NUMBER.fullmatch(token[index + 1 :]) is not None:
                return True
    return False


def check_content_policy(parsed: ParsedCommit, policy: CommitPolicy) -> tuple[Diagnostic, ...]:
    """Apply optional content constraints without interpreting references or identities."""
    diagnostics: list[Diagnostic] = []
    if policy.required_issue_prefixes and not has_issue_reference(
        parsed.message, policy.required_issue_prefixes
    ):
        diagnostics.append(
            Diagnostic(
                code="reference.required",
                message="a configured work-item prefix followed by a positive ASCII ID is required",
            )
        )
    if not policy.footer_values or parsed.footer_start_line is None:
        return tuple(diagnostics)
    starts = list(
        iter_footer_starts(
            parsed.footer_lines,
            start_line=parsed.footer_start_line,
            footer_syntax=policy.footer_syntax,
        )
    )
    allowed = {token.casefold(): values for token, values in policy.footer_values}
    reported: set[str] = set()
    for index, footer in enumerate(starts):
        folded = footer.token.casefold()
        if folded not in allowed or folded in reported:
            continue
        offset = footer.line - parsed.footer_start_line
        end = (
            starts[index + 1].line - parsed.footer_start_line
            if index + 1 < len(starts)
            else len(parsed.footer_lines)
        )
        first = parsed.footer_lines[offset]
        # The parser has already recognized the exact colon or hash separator.
        value_start = len(footer.token) + (1 if footer.separator == ":" else 2)
        value = "\n".join((first[value_start:], *parsed.footer_lines[offset + 1 : end])).strip()
        if value not in allowed[folded]:
            diagnostics.append(
                Diagnostic(
                    code="footer.value",
                    message=f"footer {footer.token!r} must use one of its configured exact values",
                    line=footer.line,
                )
            )
            reported.add(folded)
    return tuple(diagnostics)
