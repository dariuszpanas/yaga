"""Installed description-check orchestration with explicit local inputs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from yaga.commits.typos import check_typos
from yaga.descriptions.checker import check_description
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
    template_path: Path | None = None,
) -> CheckedDescription:
    """Check exactly one source; never discover policy, template, Git, or events."""
    if sum((file_path is not None, stdin, event_file is not None)) != 1:
        raise InputError("select exactly one description source: --file, --stdin, or --event-file")
    loaded = load_description_policy(policy_path)
    number = None
    if event_file is not None:
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
    template = read_description_file(template_path) if template_path is not None else None
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
