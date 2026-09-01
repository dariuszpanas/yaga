"""Dependency-free runtime for the read-only commit-check Action."""

from __future__ import annotations

from pathlib import Path

from yaga.commits.github_event import check_pull_request
from yaga.commits.github_reporting import (
    PullRequestOutputFormat,
    render_pull_request_error,
    render_pull_request_report,
)
from yaga.errors import YagaError


def run_pull_request_action(
    event_file: str,
    repository_path: str,
    *,
    event_name: str,
    repository: str,
    repository_id: int,
    base_ref: str,
    head_ref: str,
) -> int:
    """Run the fixed GitHub annotation path used by the composite Action."""
    output_format = PullRequestOutputFormat.GITHUB
    try:
        report = check_pull_request(
            Path(event_file),
            Path(repository_path),
            expected_event_name=event_name,
            expected_repository=repository,
            expected_repository_id=repository_id,
            expected_base_ref=base_ref,
            expected_head_ref=head_ref,
        )
    except YagaError as error:
        print(render_pull_request_error(error, output_format))
        return 2
    print(render_pull_request_report(report, output_format))
    return 0 if report.valid else 1
