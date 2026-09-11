"""Configurable policy checks for parsed Conventional Commit messages."""

from __future__ import annotations

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase

from yaga.commits.models import (
    BreakingMarkerPolicy,
    CasePolicy,
    CheckResult,
    CommitFooter,
    CommitPolicy,
    CommitTarget,
    Diagnostic,
    EndingPolicy,
    MergePolicy,
    PresencePolicy,
)
from yaga.commits.parser import (
    MAX_MESSAGE_BYTES,
    header_from,
    iter_footer_starts,
    normalize_message,
    parse_message,
)
from yaga.errors import InputError

_TERMINAL_PUNCTUATION = (".", "!", "?")
_LIST_ITEM = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")


def check_target(target: CommitTarget, policy: CommitPolicy) -> CheckResult:
    """Validate one selected message with deterministic diagnostic ordering."""
    return _check_target(target, policy, check_body=True)


def check_header(target: CommitTarget, policy: CommitPolicy) -> CheckResult:
    """Validate one standalone header without applying commit-body policy."""
    return _check_target(target, policy, check_body=False)


def _check_target(
    target: CommitTarget,
    policy: CommitPolicy,
    *,
    check_body: bool,
) -> CheckResult:
    normalized = normalize_message(target.message)
    header = header_from(normalized)
    try:
        message_bytes = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError("commit message is not valid UTF-8") from error
    if len(message_bytes) > MAX_MESSAGE_BYTES:
        return _failure(
            target,
            header,
            "message.size",
            f"message exceeds the hard {MAX_MESSAGE_BYTES}-byte limit",
        )

    if target.is_merge:
        if policy.merge_commits is MergePolicy.IGNORE:
            return CheckResult(target=target, header=header, skipped_reason="merge commit")
        if policy.merge_commits is MergePolicy.REJECT:
            return _failure(
                target,
                header,
                "merge.rejected",
                "merge commits are rejected by the configured policy",
            )

    if any(fnmatchcase(header, pattern) for pattern in policy.ignored_headers):
        return CheckResult(target=target, header=header, skipped_reason="ignored header")

    parsed = parse_message(normalized)
    if parsed is None:
        return _failure(
            target,
            header,
            "syntax.header",
            "expected <type>[optional scope][!]: <description>",
        )

    diagnostics: list[Diagnostic] = []
    if not parsed.separator_valid:
        diagnostics.append(
            Diagnostic(
                code="syntax.separator",
                message="body or footers must be separated from the header by a blank line",
                line=2,
            )
        )
    _check_case(diagnostics, parsed.commit_type, policy.type_case, component="type")
    if policy.allowed_types is not None and not _contains_casefold(
        policy.allowed_types, parsed.commit_type
    ):
        diagnostics.append(
            Diagnostic(
                code="type.allowed",
                message=f"type {parsed.commit_type!r} is not in allowed-types",
            )
        )

    scope_policy = _scope_policy_for_type(policy, parsed.commit_type)
    if parsed.scope is None:
        if scope_policy is PresencePolicy.REQUIRED:
            diagnostics.append(
                Diagnostic(code="scope.required", message="a scope is required by policy")
            )
    else:
        if scope_policy is PresencePolicy.FORBIDDEN:
            diagnostics.append(
                Diagnostic(code="scope.forbidden", message="scopes are forbidden by policy")
            )
        _check_case(diagnostics, parsed.scope, policy.scope_case, component="scope")
        if policy.allowed_scopes is not None and not _contains_casefold(
            policy.allowed_scopes, parsed.scope
        ):
            diagnostics.append(
                Diagnostic(
                    code="scope.allowed",
                    message=f"scope {parsed.scope!r} is not in allowed-scopes",
                )
            )

    if policy.header_max_length is not None and len(parsed.header) > policy.header_max_length:
        diagnostics.append(
            Diagnostic(
                code="header.length",
                message=(
                    f"header has {len(parsed.header)} characters; maximum is "
                    f"{policy.header_max_length}"
                ),
            )
        )
    description_length = len(parsed.description)
    if description_length < policy.description_min_length:
        diagnostics.append(
            Diagnostic(
                code="description.length",
                message=(
                    f"description has {description_length} characters; minimum is "
                    f"{policy.description_min_length}"
                ),
            )
        )
    if (
        policy.description_max_length is not None
        and description_length > policy.description_max_length
    ):
        diagnostics.append(
            Diagnostic(
                code="description.length",
                message=(
                    f"description has {description_length} characters; maximum is "
                    f"{policy.description_max_length}"
                ),
            )
        )
    ends_with_punctuation = parsed.description.endswith(_TERMINAL_PUNCTUATION)
    if policy.description_ending is EndingPolicy.FORBID and ends_with_punctuation:
        diagnostics.append(
            Diagnostic(
                code="description.ending",
                message="description must not end with '.', '!', or '?'",
            )
        )
    if policy.description_ending is EndingPolicy.REQUIRE and not ends_with_punctuation:
        diagnostics.append(
            Diagnostic(
                code="description.ending",
                message="description must end with '.', '!', or '?'",
            )
        )

    if check_body:
        if (
            policy.breaking_markers is BreakingMarkerPolicy.PAIRED
            and parsed.breaking_header != parsed.breaking_footer
        ):
            diagnostics.append(
                Diagnostic(
                    code="breaking.marker-pair",
                    message="breaking changes must use both ! and a BREAKING CHANGE footer",
                )
            )
        has_body = bool(parsed.body.strip())
        if policy.body_policy is PresencePolicy.REQUIRED and not has_body:
            diagnostics.append(
                Diagnostic(code="body.required", message="a body is required by policy")
            )
        if policy.body_policy is PresencePolicy.FORBIDDEN and has_body:
            diagnostics.append(
                Diagnostic(
                    code="body.forbidden",
                    message="a body is forbidden by policy",
                    line=parsed.body_start_line,
                )
            )
        if has_body and len(parsed.body.strip()) < policy.body_min_length:
            diagnostics.append(
                Diagnostic(
                    code="body.length",
                    message=(
                        f"body has {len(parsed.body.strip())} characters; minimum is "
                        f"{policy.body_min_length}"
                    ),
                    line=parsed.body_start_line,
                )
            )
        if has_body and policy.body_min_words:
            body_word_count = _word_count_below_minimum(parsed.body, policy.body_min_words)
            if body_word_count is not None:
                word_label = "word" if body_word_count == 1 else "words"
                diagnostics.append(
                    Diagnostic(
                        code="body.word-count",
                        message=(
                            f"body has {body_word_count} {word_label}; minimum is "
                            f"{policy.body_min_words}"
                        ),
                        line=parsed.body_start_line,
                    )
                )
        if policy.body_max_line_length is not None:
            for offset, line in enumerate(parsed.body_lines):
                if len(line) > policy.body_max_line_length:
                    diagnostics.append(
                        Diagnostic(
                            code="body.line-length",
                            message=(
                                f"body line has {len(line)} characters; maximum is "
                                f"{policy.body_max_line_length}"
                            ),
                            line=parsed.body_start_line + offset,
                        )
                    )
                    break
        if policy.body_max_consecutive_single_line_paragraphs is not None:
            count, first_line = _consecutive_single_line_prose_paragraphs(parsed.body_lines)
            if count > policy.body_max_consecutive_single_line_paragraphs:
                noun = "paragraph" if count == 1 else "paragraphs"
                diagnostics.append(
                    Diagnostic(
                        code="body.paragraph-format",
                        message=(
                            f"body has {count} consecutive single-line prose {noun}; maximum is "
                            f"{policy.body_max_consecutive_single_line_paragraphs}"
                        ),
                        line=parsed.body_start_line + first_line,
                    )
                )
        if policy.required_footer_tokens or policy.forbidden_footer_tokens:
            footers = (
                iter_footer_starts(
                    parsed.footer_lines,
                    start_line=parsed.footer_start_line,
                )
                if parsed.footer_start_line is not None
                else ()
            )
            _check_footer_tokens(diagnostics, footers, policy)

    return CheckResult(target=target, header=header, diagnostics=tuple(diagnostics))


def _consecutive_single_line_prose_paragraphs(body_lines: tuple[str, ...]) -> tuple[int, int]:
    """Find the longest run that looks like prose was split at a paragraph break."""
    longest = 0
    longest_start = 0
    run = 0
    run_start = 0
    previous_line: str | None = None
    paragraph_start = 0
    paragraph: list[str] = []
    for offset, line in enumerate((*body_lines, "")):
        if line:
            if not paragraph:
                paragraph_start = offset
            paragraph.append(line)
            continue
        if len(paragraph) == 1 and _is_prose_paragraph_line(paragraph[0]):
            line = paragraph[0]
            if run and _looks_like_sentence_continuation(previous_line, line):
                run += 1
            else:
                run = 1
                run_start = paragraph_start
            previous_line = line
            if run > longest:
                longest = run
                longest_start = run_start
        else:
            run = 0
            previous_line = None
        paragraph.clear()
    return longest, longest_start


def _looks_like_sentence_continuation(previous: str | None, current: str) -> bool:
    """Recognize a blank-line boundary that is likely an accidental sentence split."""
    if previous is None:
        return False
    previous_end = previous.rstrip()[-1:]
    current_start = current.lstrip()[:1]
    return previous_end not in ".!?" or current_start.islower()


def _is_prose_paragraph_line(line: str) -> bool:
    """Leave intentionally formatted list items out of paragraph-style checks."""
    return bool(line.strip()) and _LIST_ITEM.match(line.lstrip()) is None


def _check_case(
    diagnostics: list[Diagnostic], value: str, policy: CasePolicy, *, component: str
) -> None:
    if policy is CasePolicy.LOWER and value != value.lower():
        diagnostics.append(
            Diagnostic(
                code=f"{component}.case",
                message=f"{component} {value!r} must be lowercase",
            )
        )
    elif policy is CasePolicy.UPPER and value != value.upper():
        diagnostics.append(
            Diagnostic(
                code=f"{component}.case",
                message=f"{component} {value!r} must be uppercase",
            )
        )


def _contains_casefold(values: tuple[str, ...], candidate: str) -> bool:
    folded = candidate.casefold()
    return any(value.casefold() == folded for value in values)


def _scope_policy_for_type(policy: CommitPolicy, commit_type: str) -> PresencePolicy:
    """Resolve one case-insensitive per-type override or the global fallback."""
    if not policy.scope_policy_by_type:
        return policy.scope_policy
    folded_type = commit_type.casefold()
    for configured_type, override in policy.scope_policy_by_type:
        if configured_type.casefold() == folded_type:
            return override
    return policy.scope_policy


def _check_footer_tokens(
    diagnostics: list[Diagnostic],
    footers: Iterable[CommitFooter],
    policy: CommitPolicy,
) -> None:
    """Apply bounded presence policies after the existing body diagnostics."""
    required = {token.casefold(): token for token in policy.required_footer_tokens}
    forbidden = {token.casefold(): token for token in policy.forbidden_footer_tokens}
    present_required: set[str] = set()
    first_forbidden: tuple[int, str] | None = None

    for footer in footers:
        token = footer.token
        folded = token.casefold()
        if folded in required:
            present_required.add(folded)
        if first_forbidden is None and folded in forbidden:
            first_forbidden = (footer.line, forbidden[folded])

    missing = [token for folded, token in required.items() if folded not in present_required]
    if missing:
        remaining = len(missing) - 1
        suffix = ""
        if remaining:
            noun = "token" if remaining == 1 else "tokens"
            suffix = f"; {remaining} additional required footer {noun} missing"
        diagnostics.append(
            Diagnostic(
                code="footer.required",
                message=f"required footer token {missing[0]!r} is missing{suffix}",
            )
        )
    if first_forbidden is not None:
        line, token = first_forbidden
        diagnostics.append(
            Diagnostic(
                code="footer.forbidden",
                message=f"footer token {token!r} is forbidden by policy",
                line=line,
            )
        )


def _word_count_below_minimum(value: str, minimum: int) -> int | None:
    """Return the exact word count only when it is below ``minimum``."""
    count = 0
    token_has_alphanumeric = False
    for character in value:
        if character.isspace():
            token_has_alphanumeric = False
        elif not token_has_alphanumeric and character.isalnum():
            token_has_alphanumeric = True
            count += 1
            if count >= minimum:
                return None
    return count


def _failure(target: CommitTarget, header: str, code: str, message: str) -> CheckResult:
    return CheckResult(
        target=target,
        header=header,
        diagnostics=(Diagnostic(code=code, message=message),),
    )
