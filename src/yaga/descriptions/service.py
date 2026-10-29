"""Installed description-check orchestration with explicit offline or gh inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from yaga.commits.typos import check_typos
from yaga.descriptions.checker import check_description
from yaga.descriptions.github import fetch_pull_request_description, parse_pull_request_reference
from yaga.descriptions.markdown import spelling_text
from yaga.descriptions.models import MAX_DIAGNOSTICS, DescriptionDiagnostic, DescriptionReport
from yaga.descriptions.policy import load_description_policy
from yaga.descriptions.sources import (
    load_description_event,
    read_description_file,
    read_description_stdin,
)
from yaga.errors import InputError


@dataclass(frozen=True, slots=True)
class CheckedDescription:
    """A description result and its explicit input labels, without body text."""

    report: DescriptionReport
    policy_path: Path
    source: str
    template_path: Path | None
    pull_request_number: int | None


def check_description_source(
    *,
    policy_path: Path,
    file_path: Path | None = None,
    stdin: bool = False,
    event_file: Path | None = None,
    pull_request: str | None = None,
    github_repository: str | None = None,
    template_path: Path | None = None,
) -> CheckedDescription:
    """Check exactly one source; never discover policy, template, Git, or events."""
    if sum((file_path is not None, stdin, event_file is not None, pull_request is not None)) != 1:
        raise InputError(
            "select exactly one description source: --file, --stdin, --pr, or --event-file"
        )
    if github_repository is not None and pull_request is None:
        raise InputError("--repo requires a numeric --pr source")
    reference = (
        parse_pull_request_reference(pull_request, github_repository)
        if pull_request is not None
        else None
    )
    loaded = load_description_policy(policy_path)
    template = read_description_file(template_path) if template_path is not None else None
    number = None
    if reference is not None:
        fetched = fetch_pull_request_description(reference)
        body = fetched.body
        number = fetched.number
        source = fetched.url
    elif event_file is not None:
        event = load_description_event(event_file)
        body = event.body
        number = event.number
        source = str(event_file)
    elif file_path is not None:
        body = read_description_file(file_path)
        source = str(file_path)
    else:
        body = read_description_stdin()
        source = "stdin"
    report = check_description(body, loaded.policy, template=template)
    if loaded.policy.typos == "check":
        # Always isolate Typos configuration from caller-controlled working directories.
        # The shared adapter owns process-tree, time, output and executable failures.
        spelling = spelling_text(body)
        findings = check_typos(spelling, config_isolated=True)
        spelling_lines = spelling.split("\n")
        if any(
            not 1 <= item.line <= len(spelling_lines)
            or not 1 <= item.column <= len(spelling_lines[item.line - 1].encode("utf-8")) + 1
            for item in findings
        ):
            raise InputError("typos returned a location outside the description")
        diagnostics = tuple(
            DescriptionDiagnostic(
                code="description.typo", message=item.message, line=item.line, column=item.column
            )
            for item in findings
        )
        report = replace(
            report,
            diagnostics=(*report.diagnostics, *diagnostics)[:MAX_DIAGNOSTICS],
            diagnostic_count=report.diagnostic_count + len(diagnostics),
        )
    return CheckedDescription(
        report=report,
        policy_path=loaded.path,
        source=source,
        template_path=template_path,
        pull_request_number=number,
    )
