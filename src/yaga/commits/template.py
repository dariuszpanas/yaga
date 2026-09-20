"""Read-only, deliberately incomplete message templates derived from policy."""

from dataclasses import replace
from enum import StrEnum

from yaga.commits.checker import check_header
from yaga.commits.config_explain import explain_type
from yaga.commits.models import (
    CommitPolicy,
    CommitTarget,
    DiagnosticSeverity,
    MessageFormat,
    PresencePolicy,
)
from yaga.commits.parser import parse_message
from yaga.commits.policy import resolve_presence_policy
from yaga.errors import InputError


class TemplateComment(StrEnum):
    """Explicit supported Git editor comment characters."""

    HASH = "#"
    SEMICOLON = ";"


def message_template(
    policy: CommitPolicy,
    *,
    commit_type: str | None = None,
    scope: str | None = None,
    comment: TemplateComment = TemplateComment.HASH,
) -> str:
    """Return guidance and blank author fields; never invent a description or validation claim."""
    if not isinstance(comment, TemplateComment):
        raise InputError("unsupported template comment character")
    body_policy = policy.body_policy
    if policy.message_format is MessageFormat.PLAIN:
        if commit_type is not None or scope is not None:
            raise InputError("plain message templates do not accept --type or --scope")
        header = ""
    else:
        if commit_type is None:
            raise InputError("conventional message templates require an explicit --type")
        explain_type(policy, commit_type)
        if scope is not None and (not scope or len(scope) > 128):
            raise InputError("template scope must contain 1 to 128 characters")
        header = commit_type + (f"({scope})" if scope is not None else "") + ": "
        parsed = parse_message(header + "description")
        if parsed is None or parsed.scope != scope or parsed.commit_type != commit_type:
            raise InputError("template type or scope is not one safe component")
        checked = check_header(
            CommitTarget("template", header + "description"), replace(policy, ignored_headers=())
        )
        if any(
            d.severity is DiagnosticSeverity.ERROR
            and d.code.startswith(("syntax.", "type.", "scope."))
            for d in checked.diagnostics
        ):
            raise InputError("template type or scope does not satisfy configured component policy")
        body_policy, _ = resolve_presence_policy(
            policy.body_policy, policy.body_policy_by_type, commit_type
        )
    notes = [
        "Write your description on the first line; this incomplete template must not be committed as-is.",
        f"Comment character: {comment.value}. Match your Git editor cleanup setting or remove these lines.",
        f"Body policy: {body_policy.value}. Blank lines separate the header, prose, and final footers.",
    ]
    if policy.header_max_length is not None:
        notes.append(f"Header limit: {policy.header_max_length} {policy.length_unit.value} units.")
    if body_policy is not PresencePolicy.FORBIDDEN:
        if policy.body_min_words:
            notes.append(f"Prose requires at least {policy.body_min_words} words when present.")
        if policy.body_min_length:
            notes.append(
                f"Prose minimum: {policy.body_min_length} {policy.length_unit.value} units when present."
            )
    if policy.required_issue_prefixes:
        notes.append(
            "Include a real work-item reference using one prefix: "
            + ", ".join(policy.required_issue_prefixes)
        )
    for token, values in policy.footer_values:
        notes.append(f"Allowed values for {token}: " + " | ".join(values))
    if policy.warning_rules:
        notes.append("Warning-only rules: " + ", ".join(policy.warning_rules))
    notes.append(
        "Other configured rules still apply; validate the completed message before committing."
    )
    lines = [header, "", *(f"{comment.value} {note}" for note in notes), ""]
    seen: set[str] = set()
    for token in (*policy.required_colon_footer_tokens, *policy.required_footer_tokens):
        if token.casefold() not in seen:
            lines.append(f"{token}: ")
            seen.add(token.casefold())
    return "\n".join(lines) + "\n"
