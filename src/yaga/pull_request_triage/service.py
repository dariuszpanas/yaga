"""Explicit installed triage orchestration; offline evaluation never writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yaga.agent_review.remote import parse_pull_request_reference
from yaga.errors import InputError, YagaError, safe_error_text
from yaga.files import read_file_prefix
from yaga.pull_request_triage.core import MAX_SNAPSHOT_BYTES, decode_snapshot, evaluate
from yaga.pull_request_triage.policy import load_triage_policy
from yaga.pull_request_triage.remote import TriageClient, fetch_snapshot, publish_comment


def check_triage(
    *,
    policy_path: Path,
    file_path: Path | None = None,
    pull_request: str | None = None,
    github_repository: str | None = None,
    publish: bool = False,
) -> dict[str, Any]:
    if (file_path is None) == (pull_request is None):
        raise InputError("select exactly one triage source: --file or --pr")
    if github_repository is not None and pull_request is None:
        raise InputError("triage --repo requires --pr")
    if publish and pull_request is None:
        raise InputError("triage --publish-comment requires a live --pr")
    policy = load_triage_policy(policy_path)
    if pull_request is not None:
        reference = parse_pull_request_reference(pull_request, github_repository)
        client = TriageClient(reference)
        snapshot = fetch_snapshot(client, policy)
        report = evaluate(policy, snapshot)
        if publish:
            report, publication = publish_comment(client, policy, snapshot)
            report["published_comment"] = publication
    else:
        assert file_path is not None
        try:
            raw = read_file_prefix(file_path, maximum=MAX_SNAPSHOT_BYTES)
        except OSError:
            raise InputError("cannot read explicit triage snapshot") from None
        report = evaluate(policy, decode_snapshot(raw))
    return report


def render_triage(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        result = json.dumps(report, ensure_ascii=True, indent=2)
        if len(result.encode()) > MAX_SNAPSHOT_BYTES:
            raise InputError("triage report exceeds 1 MiB")
        return result
    admission = report["admission"]
    line = f"PR triage: {admission['decision']} ({admission['reason']}); {report['finding_count']} findings."
    if output_format == "github":
        return "::notice title=YAGA PR triage::" + line
    lines = [line, f"Head: {report['boundary']['head_sha']}"]
    for finding in report["findings"]:
        lines.append(
            f"- {finding['code']}: {safe_error_text(finding.get('path', finding.get('pull_request_number', '')), maximum=300)}"
        )
    return "\n".join(lines)


def render_triage_error(error: YagaError, output_format: str) -> str:
    message = safe_error_text(error, maximum=500)
    if output_format == "json":
        return json.dumps(
            {
                "schema_version": 1,
                "kind": "pull_request_triage",
                "status": "error",
                "valid": False,
                "error": {"kind": error.kind, "message": message},
            },
            ensure_ascii=True,
            indent=2,
        )
    if output_format == "github":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return "::error title=YAGA PR triage::" + escaped
    return f"YAGA triage {error.kind} error: {message}"
