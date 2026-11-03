"""Dependency-free GitHub Step Summary Markdown renderer for YAGA Action runs."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from yaga.version_requirement import VERSION

MAX_SUMMARY_INPUT_BYTES = 262_144
MAX_STEP_SUMMARY_BYTES = 65_536
MAX_DISPLAY_FINDINGS = 50
MAX_CELL_CHARS = 240
MAX_SUMMARY_LINE_CHARS = 300

_GROUP_PREFIX = "::group::"
_ENDGROUP_LINE = "::endgroup::"
_REPO_GROUP_RE = re.compile(r"^YAGA repository check:\s*(\S+)\s*\(([^)]+)\)\s*$")
_COMMAND_RE = re.compile(r"^::(error|warning|notice)(?:\s+([^:]*))?::(.*)$")
_MODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

_SUMMARY_PREFIXES = (
    "YAGA repository checks: ",
    "YAGA branch policy: ",
    "YAGA change policy: ",
    "PR description: ",
)
_HIGHLIGHT_PREFIXES = (
    "Mode policy: ",
    "Path policy: ",
    "Size policy: ",
    "Tree policy: ",
)


@dataclass(frozen=True, slots=True)
class ProviderSummaryRow:
    """One provider section parsed from an aggregate ``repo check`` report."""

    provider: str
    result: str
    highlight: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FindingRow:
    """One decoded GitHub workflow annotation."""

    level: str
    scope: str
    location: str
    message: str


def _strip_controls(value: str, *, keep_newlines: bool = False) -> str:
    cleaned: list[str] = []
    for char in value:
        code = ord(char)
        if keep_newlines and char == "\n":
            cleaned.append("\n")
        elif char in {"\r", "\n", "\t"}:
            cleaned.append(" " if not keep_newlines else "")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            continue
        else:
            cleaned.append(char)
    return "".join(cleaned)


def _unescape_workflow_value(value: str, *, is_property: bool = False) -> str:
    text = value.replace("%0D", "").replace("%0d", "")
    text = text.replace("%0A", " · ").replace("%0a", " · ")
    if is_property:
        text = text.replace("%3A", ":").replace("%3a", ":")
        text = text.replace("%2C", ",").replace("%2c", ",")
    return text.replace("%25", "%")


def _escape_markdown_inline(
    value: str,
    *,
    maximum: int,
    escape_pipes: bool,
    inside_code_span: bool = False,
) -> str:
    collapsed = " ".join(_strip_controls(value).split())
    if len(collapsed) > maximum:
        collapsed = collapsed[: maximum - 1].rstrip() + "…"
    escaped = (
        collapsed.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "&#96;")
    )
    if not inside_code_span:
        escaped = (
            escaped.replace("\\", "\\\\")
            .replace("*", "\\*")
            .replace("_", "\\_")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace("~", "\\~")
        )
    if escape_pipes:
        escaped = escaped.replace("|", "\\|")
    return escaped


def _escape_table_cell(value: str, *, maximum: int = MAX_CELL_CHARS) -> str:
    return _escape_markdown_inline(value, maximum=maximum, escape_pipes=True)


def _escape_code_cell(value: str, *, maximum: int = 96) -> str:
    inner = _escape_markdown_inline(
        value,
        maximum=maximum,
        escape_pipes=True,
        inside_code_span=True,
    )
    return f"`{inner}`"


def _escape_inline_text(value: str, *, maximum: int = MAX_SUMMARY_LINE_CHARS) -> str:
    return _escape_markdown_inline(value, maximum=maximum, escape_pipes=False)


def _escape_pre_line(value: str) -> str:
    return _strip_controls(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _read_bounded_lines(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    with path.open("rb") as handle:
        raw = handle.read(MAX_SUMMARY_INPUT_BYTES)
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text.splitlines()


def _parse_properties(raw_props: str | None) -> dict[str, str]:
    if not raw_props:
        return {}
    props: dict[str, str] = {}
    for item in raw_props.split(","):
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            props[clean_key] = _unescape_workflow_value(val.strip(), is_property=True)
    return props


def _extract_provider_highlight(lines: tuple[str, ...]) -> str:
    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return "—"
    for candidate in reversed(non_empty):
        if candidate.startswith("Config: "):
            continue
        for prefix in _HIGHLIGHT_PREFIXES:
            if candidate.startswith(prefix):
                return candidate[len(prefix) :]
        return candidate
    return non_empty[-1]


def _format_result_badge(raw_result: str) -> str:
    normalized = raw_result.strip().lower()
    if normalized == "passed":
        return "**Passed**"
    if normalized == "failed":
        return "**Failed**"
    if normalized == "error":
        return "**Error**"
    if normalized == "skipped":
        return "Skipped"
    return f"**{_escape_table_cell(raw_result, maximum=32)}**"


def _parse_streams(
    report_lines: list[str], error_lines: list[str]
) -> tuple[list[ProviderSummaryRow], list[FindingRow], list[str], list[str]]:
    providers: list[ProviderSummaryRow] = []
    findings: list[FindingRow] = []
    notices: list[str] = []
    plain_lines: list[str] = []

    in_group = False
    group_title = ""
    group_buf: list[str] = []

    for line in (*report_lines, *error_lines):
        if line.startswith(_GROUP_PREFIX):
            if in_group:
                providers.append(_build_provider_row(group_title, group_buf))
            in_group = True
            group_title = line[len(_GROUP_PREFIX) :].strip()
            group_buf = []
            continue
        if in_group and line.strip() == _ENDGROUP_LINE:
            providers.append(_build_provider_row(group_title, group_buf))
            in_group = False
            group_title = ""
            group_buf = []
            continue
        if in_group:
            group_buf.append(line)
            continue

        cmd_match = _COMMAND_RE.match(line)
        if cmd_match is not None:
            level, raw_props, raw_msg = cmd_match.groups()
            props = _parse_properties(raw_props)
            message = _unescape_workflow_value(raw_msg.strip())
            if level == "notice":
                if message:
                    notices.append(message)
                continue
            raw_title = props.get("title", "").strip()
            if raw_title.startswith("YAGA "):
                raw_title = raw_title[5:].strip()
            scope = raw_title or "policy"
            file_prop = props.get("file", "").strip()
            line_prop = props.get("line", "").strip()
            col_prop = props.get("col", "").strip()
            if file_prop and line_prop and col_prop:
                location = f"{file_prop}:{line_prop}:{col_prop}"
            elif file_prop and line_prop:
                location = f"{file_prop}:{line_prop}"
            elif file_prop:
                location = file_prop
            elif line_prop:
                location = f"line {line_prop}"
            else:
                location = "—"
            findings.append(
                FindingRow(
                    level="Error" if level == "error" else "Warning",
                    scope=scope,
                    location=location,
                    message=message,
                )
            )
            continue

        stripped = line.strip()
        if stripped:
            plain_lines.append(stripped)

    if in_group:
        providers.append(_build_provider_row(group_title, group_buf))

    return providers, findings, notices, plain_lines


def _build_provider_row(title: str, lines: list[str]) -> ProviderSummaryRow:
    body = tuple(lines)
    match = _REPO_GROUP_RE.match(title)
    if match is not None:
        provider, result = match.groups()
    else:
        provider, result = title or "check", "info"
    return ProviderSummaryRow(
        provider=provider,
        result=result,
        highlight=_extract_provider_highlight(body),
        lines=body,
    )


def _status_label(status_code: int, *, mode: str) -> str:
    if status_code == 0:
        return "Passed"
    if status_code == 1:
        return "Failed" if mode == "gate" else "Findings reported"
    return "Error"


def _select_headline(
    plain_lines: list[str],
    notices: list[str],
    findings: list[FindingRow],
) -> tuple[str | None, list[str]]:
    if plain_lines:
        headline_raw = plain_lines[0]
        remaining = list(plain_lines[1:])
        for index, candidate in enumerate(plain_lines):
            if candidate.startswith((*_SUMMARY_PREFIXES, "YAGA checked pull request ")):
                headline_raw = candidate
                remaining = plain_lines[:index] + plain_lines[index + 1 :]
                break
        for prefix in _SUMMARY_PREFIXES:
            if headline_raw.startswith(prefix):
                headline_raw = headline_raw[len(prefix) :]
                break
        return headline_raw, remaining
    if notices:
        return notices[-1], []
    if len(findings) == 1:
        return findings[0].message, []
    return None, []


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[: max(0, max_bytes)].decode("utf-8", errors="ignore")
    return clipped.rstrip()


def render_step_summary(
    *,
    mode: str,
    status_code: int,
    report_lines: list[str],
    error_lines: list[str],
) -> str:
    """Render a bounded, sanitized GitHub Step Summary Markdown card."""
    safe_mode = mode if _MODE_RE.fullmatch(mode) else "action"
    status_text = _status_label(status_code, mode=safe_mode)
    providers, findings, notices, plain_lines = _parse_streams(report_lines, error_lines)
    headline, extra_lines = _select_headline(plain_lines, notices, findings)

    parts: list[str] = [f"## `>_` YAGA Checks · `{safe_mode}`", ""]
    status_line = f"**Status:** {status_text} (exit `{status_code}`) · **Version:** `{VERSION}`"
    if headline:
        status_line += f" · **Summary:** {_escape_inline_text(headline)}"
    parts.append(status_line)
    parts.append("")

    if providers:
        parts.extend(
            (
                "| Provider | Result | Highlights |",
                "| :--- | :--- | :--- |",
            )
        )
        for row in providers:
            provider_cell = _escape_code_cell(row.provider, maximum=40)
            result_cell = _format_result_badge(row.result)
            highlight_cell = _escape_table_cell(row.highlight)
            parts.append(f"| {provider_cell} | {result_cell} | {highlight_cell} |")
        parts.append("")

    if findings and not (len(findings) == 1 and not providers and not plain_lines):
        visible_findings = findings[:MAX_DISPLAY_FINDINGS]
        parts.extend(
            (
                "| Level | Rule / Scope | Location | Message |",
                "| :--- | :--- | :--- | :--- |",
            )
        )
        for item in visible_findings:
            level_cell = f"**{item.level}**"
            scope_cell = _escape_code_cell(item.scope, maximum=64)
            loc_cell = "—" if item.location == "—" else _escape_code_cell(item.location, maximum=96)
            msg_cell = _escape_table_cell(item.message)
            parts.append(f"| {level_cell} | {scope_cell} | {loc_cell} | {msg_cell} |")
        omitted_count = len(findings) - len(visible_findings)
        if omitted_count > 0:
            parts.append("")
            parts.append(f"*{omitted_count} additional finding(s) omitted from summary table.*")
        parts.append("")

    detail_blocks: list[str] = []
    if providers:
        for row in providers:
            header = _escape_pre_line(f"[{row.provider} · {row.result}]")
            body = "\n".join(_escape_pre_line(line) for line in row.lines)
            detail_blocks.append(f"{header}\n{body}" if body else header)
    if extra_lines:
        detail_blocks.append("\n".join(_escape_pre_line(line) for line in extra_lines))

    base_markdown = "\n".join(parts)
    if detail_blocks:
        summary_title = (
            f"Detailed provider output ({len(providers)})" if providers else "Additional details"
        )
        open_attr = " open" if (status_code != 0 and not providers and not findings) else ""
        prefix = (
            f"{base_markdown}\n<details{open_attr}>\n"
            f"<summary><b>{summary_title}</b></summary>\n\n<pre>\n"
        )
        suffix = "\n</pre>\n\n</details>\n"
        budget = MAX_STEP_SUMMARY_BYTES - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
        if budget > 64:
            joined_details = "\n\n".join(detail_blocks)
            clipped_details = _truncate_utf8(joined_details, budget)
            return f"{prefix}{clipped_details}{suffix}"

    return _truncate_utf8(base_markdown + "\n", MAX_STEP_SUMMARY_BYTES)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint writing bounded Step Summary Markdown to stdout."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(prog="yaga.step_summary", add_help=False)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--status", required=True, type=int)
    parser.add_argument("--report", default="")
    parser.add_argument("--error", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    report_path = Path(args.report) if args.report else None
    error_path = Path(args.error) if args.error else None
    markdown = render_step_summary(
        mode=args.mode,
        status_code=args.status,
        report_lines=_read_bounded_lines(report_path),
        error_lines=_read_bounded_lines(error_path),
    )
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
