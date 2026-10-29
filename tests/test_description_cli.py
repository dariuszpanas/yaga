"""Installed description CLI and service boundary regression tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.descriptions.service import check_description_source
from yaga.errors import InputError
from yaga.git.runtime import ProcessResult

runner = CliRunner()
LOCAL = ["pull-request", "description", "check"]
GITHUB = ["github", "pull-request", "description", "check"]


def _unstyle(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)


def _policy(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "description-policy.toml"
    path.write_text("description-policy-version = 1\n" + extra, encoding="utf-8")
    return path


def _body(tmp_path: Path, text: str = "Explain the change and its validation.\n") -> Path:
    path = tmp_path / "body.md"
    path.write_text(text, encoding="utf-8")
    return path


def _event(tmp_path: Path, body: object = "Explain the change and its validation.") -> Path:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "action": "edited",
                "number": 23,
                "pull_request": {
                    "number": 23,
                    "state": "open",
                    "draft": False,
                    "body": body,
                    "user": {"login": "dependabot[bot]", "type": "Bot", "id": 12},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("command", [LOCAL, GITHUB])
def test_description_help_exposes_explicit_inputs_without_tokens_or_revisions(
    command: list[str],
) -> None:
    result = runner.invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    help_text = _unstyle(result.stdout)
    for option in ("--policy", "--template", "--format", "--quiet"):
        assert option in help_text
    for unsupported in ("--token", "--revision", "--config"):
        assert unsupported not in help_text
    if command == LOCAL:
        assert "--file" in help_text
        assert "--stdin" in help_text
        assert "--pr" in help_text
        assert "--repo" in help_text
        assert "--event-file" not in help_text
    else:
        assert "--event-file" in help_text
        assert "--stdin" not in help_text
        assert "--pr" not in help_text
        assert "--repo" not in help_text


@pytest.mark.parametrize("command", [LOCAL, GITHUB])
@pytest.mark.parametrize("color", [False, True])
def test_description_cli_requires_explicit_policy(command: list[str], color: bool) -> None:
    result = runner.invoke(
        app,
        command,
        color=color,
        env={"NO_COLOR": None, "TERM": "xterm-256color", "FORCE_COLOR": "1"} if color else None,
    )

    assert result.exit_code == 2
    if color:
        assert "\x1b[" in result.stderr
    error_text = _unstyle(result.stderr)
    assert "Missing option" in error_text
    assert "--policy" in error_text


def test_local_description_runs_without_git_or_discovered_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path)
    body = _body(tmp_path)
    (tmp_path / ".yaga.toml").write_text("not valid TOML", encoding="utf-8")
    (tmp_path / ".git").write_text("not a Git repository", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "unselected-event.json"))

    def unexpected_process(*_args: object, **_kwargs: object) -> None:
        pytest.fail("description checks with typos=skip must not start processes")

    monkeypatch.setattr("subprocess.run", unexpected_process)
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--file", str(body), "--format", "json"]
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["kind"] == "pull_request_description"
    assert document["status"] == "passed"
    assert document["valid"] is True
    assert document["word_count"] == 6
    assert document["character_count"] == len(body.read_text(encoding="utf-8"))
    assert document["diagnostic_count"] == 0
    assert document["omitted_diagnostic_count"] == 0
    assert document["diagnostics"] == []
    assert document["pull_request_number"] is None
    assert document["template_path"] is None
    assert document["policy_path"] == str(policy.resolve())
    assert "Explain the change" not in result.stdout


def test_stdin_and_file_checks_have_equivalent_findings(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "min-words = 5\n")
    text = "Short explanation.\n"
    body = _body(tmp_path, text)
    file_result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--file", str(body), "--format", "json"]
    )
    stdin_result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"], input=text
    )

    assert file_result.exit_code == stdin_result.exit_code == 1
    file_document = json.loads(file_result.stdout)
    stdin_document = json.loads(stdin_result.stdout)
    assert file_document["diagnostics"] == stdin_document["diagnostics"]
    assert file_document["word_count"] == stdin_document["word_count"] == 2
    assert file_document["diagnostics"][0]["code"] == "description.word-count"


@pytest.mark.parametrize("selectors", [[], ["--file", "body.md", "--stdin"]])
def test_local_description_requires_exactly_one_source(
    tmp_path: Path, selectors: list[str]
) -> None:
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(_policy(tmp_path)), *selectors, "--format", "json"]
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["kind"] == "input"


@pytest.mark.parametrize(
    ("file_path", "stdin", "event_file"),
    [
        (None, False, None),
        (Path("missing.md"), True, None),
        (Path("missing.md"), False, Path("missing.json")),
        (None, True, Path("missing.json")),
    ],
)
def test_service_rejects_ambiguous_sources_before_loading_inputs(
    file_path: Path | None,
    stdin: bool,
    event_file: Path | None,
) -> None:
    with pytest.raises(InputError, match="exactly one"):
        check_description_source(
            policy_path=Path("also-missing.toml"),
            file_path=file_path,
            stdin=stdin,
            event_file=event_file,
        )


@pytest.mark.parametrize("body", ["Useful explanation.", ""])
@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_quiet_suppresses_reports_but_preserves_exit_status(
    tmp_path: Path, body: str, output_format: str
) -> None:
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path)),
            "--stdin",
            "--format",
            output_format,
            "--quiet",
        ],
        input=body,
    )

    assert result.exit_code == (0 if body else 1)
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_operational_failures_remain_visible_when_quiet(tmp_path: Path, output_format: str) -> None:
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path)),
            "--file",
            str(tmp_path / "missing.md"),
            "--format",
            output_format,
            "--quiet",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr
    if output_format == "json":
        document = json.loads(result.stderr)
        assert document["kind"] == "pull_request_description"
        assert document["status"] == "error"
        assert document["valid"] is False
        assert document["error"]["kind"] == "input"
    elif output_format == "github":
        assert result.stderr.startswith("::error ")


def test_malformed_policy_is_an_operational_error(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "misspelled-setting = true\n")
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"], input="Valid prose."
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["kind"] == "configuration"


def test_explicit_template_applies_section_policy(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    template = tmp_path / "template.md"
    template.write_text("## Summary\n<!-- Explain. -->\n## Testing\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(policy),
            "--stdin",
            "--template",
            str(template),
            "--format",
            "json",
        ],
        input="## Summary\nImplemented the requested validation.\n",
    )

    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    assert document["template_path"] == str(template.resolve())
    assert any(item["code"] == "description.section-required" for item in document["diagnostics"])


def test_local_check_does_not_discover_github_template(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    template_directory = tmp_path / ".github"
    template_directory.mkdir()
    (template_directory / "pull_request_template.md").write_text(
        "Invalid template", encoding="utf-8"
    )
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"], input="Useful prose."
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["template_path"] is None


@pytest.mark.parametrize("body", [None, "", "## Summary\n<!-- Instructions only. -->\n"])
def test_event_body_is_checked_independently_of_commit_author_exemptions(
    tmp_path: Path, body: object
) -> None:
    policy = _policy(tmp_path)
    (tmp_path / ".yaga.toml").write_text(
        'config-version = 1\n[commit]\ndependabot-pull-requests = "skip"\n', encoding="utf-8"
    )
    result = runner.invoke(
        app,
        [
            *GITHUB,
            "--policy",
            str(policy),
            "--event-file",
            str(_event(tmp_path, body)),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    assert document["pull_request_number"] == 23
    assert any(item["code"] == "description.required" for item in document["diagnostics"])


def test_event_description_passes_without_a_checkout(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            *GITHUB,
            "--policy",
            str(_policy(tmp_path)),
            "--event-file",
            str(_event(tmp_path)),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "passed"
    assert document["pull_request_number"] == 23


def test_spelling_is_isolated_and_preserves_original_line_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _policy(tmp_path, 'typos = "check"\n')
    body = "\n<!-- teh hidden -->\n## Summary\n\nteh change.\n\n```\nteh_code\n```\n"
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "trusted-typos")

    def run(arguments: list[str], **kwargs: object) -> ProcessResult:
        calls.append((arguments, kwargs))
        output = (
            b'{"type":"typo","line_num":5,"byte_offset":0,"typo":"teh","corrections":["the"]}\n'
        )
        return ProcessResult(2, output, b"", False, False, False)

    monkeypatch.setattr("yaga.commits.typos.run_bounded_process", run)
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"], input=body
    )

    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    assert document["diagnostics"][0]["code"] == "description.typo"
    assert document["diagnostics"][0]["line"] == 5
    assert document["diagnostics"][0]["column"] == 1
    assert document["diagnostic_count"] == 1
    assert len(calls) == 1
    arguments, kwargs = calls[0]
    assert "--isolated" in arguments
    assert "teh" not in arguments
    spelling_input = kwargs["stdin_data"]
    assert isinstance(spelling_input, bytes)
    assert b"teh hidden" not in spelling_input
    assert b"teh_code" not in spelling_input
    assert spelling_input.decode().splitlines()[4] == "teh change."


def test_requested_spelling_dependency_failure_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: None)
    policy = _policy(tmp_path, 'typos = "check"\n')
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"], input="Valid prose."
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["kind"] == "input"


@pytest.mark.parametrize(("line", "offset"), [(2, 0), (1, 100), (65537, 0)])
def test_spelling_locations_must_belong_to_selected_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: int, offset: int
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "trusted-typos")
    output = json.dumps(
        {
            "type": "typo",
            "line_num": line,
            "byte_offset": offset,
            "typo": "teh",
            "corrections": ["the"],
        }
    ).encode()
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(2, output, b"", False, False, False),
    )
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path, 'typos = "check"\n')),
            "--stdin",
            "--format",
            "json",
        ],
        input="teh change.",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["kind"] == "input"


def test_github_annotations_escape_template_heading_content(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    template = tmp_path / "template.md"
    template.write_text("## Validation 100%\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(policy),
            "--stdin",
            "--template",
            str(template),
            "--format",
            "github",
        ],
        input="Useful prose.\n::warning::body text must not become an annotation\n",
    )

    assert result.exit_code == 1, result.stderr
    assert "100%25" in result.stdout
    assert "\n::warning::" not in result.stdout
    assert "\x1b" not in result.stdout


def test_omitted_diagnostic_count_remains_exact_in_json(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "require-checked-tasks = true\n")
    body = "A description.\n" + "- [ ] pending task\n" * 300
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"], input=body
    )

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["diagnostic_count"] == 300
    assert len(document["diagnostics"]) == 256
    assert document["omitted_diagnostic_count"] == 44


def test_github_annotations_have_a_separate_limit_and_exact_omitted_count(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "require-checked-tasks = true\n")
    result = runner.invoke(
        app,
        [*LOCAL, "--policy", str(policy), "--stdin", "--format", "github"],
        input="A description.\n" + "- [ ] pending task\n" * 300,
    )

    assert result.exit_code == 1
    assert result.stdout.count("::error ") == 50
    assert "250 additional finding(s) omitted" in result.stdout


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_spelling_findings_cannot_inject_terminal_or_github_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "trusted-typos")
    output = json.dumps(
        {
            "type": "typo",
            "line_num": 1,
            "byte_offset": 0,
            "typo": "teh",
            "corrections": ["\x1b[31mthe\n::warning::forged%"],
        }
    ).encode()
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(2, output, b"", False, False, False),
    )
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path, 'typos = "check"\n')),
            "--stdin",
            "--format",
            output_format,
        ],
        input="teh change.",
    )

    assert result.exit_code == 1, result.stderr
    assert "\x1b" not in result.stdout
    assert "\n::warning::" not in result.stdout
    if output_format == "json":
        message = json.loads(result.stdout)["diagnostics"][0]["message"]
        assert "\n" not in message
        assert "\x1b" not in message
    elif output_format == "github":
        assert "forged%25" in result.stdout


@pytest.mark.parametrize("source", ["stdin", "file", "event"])
@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ("# " + "Generated explanation " * 20, "description.heading-length"),
        ("# " + "Generated explanation " * 20 + "\n\nDone.", "description.heading-length"),
        ("Generated explanation\n===", "description.heading-level"),
        (
            "<h1>Generated explanation fills the entire description.</h1>",
            "description.html-heading",
        ),
    ],
    ids=["massive-h1", "massive-h1-token-prose", "setext-h1", "html-h1"],
)
def test_description_sources_reject_heading_mistakes_without_a_template(
    tmp_path: Path, source: str, body: str, expected_code: str
) -> None:
    policy = _policy(tmp_path)
    if source == "event":
        command = [*GITHUB, "--event-file", str(_event(tmp_path, body))]
    elif source == "file":
        command = [*LOCAL, "--file", str(_body(tmp_path, body))]
    else:
        command = [*LOCAL, "--stdin"]
    result = runner.invoke(
        app,
        [*command, "--policy", str(policy), "--format", "json"],
        input=body if source == "stdin" else None,
    )

    assert result.exit_code == 1, result.stderr
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["kind"] == "pull_request_description"
    assert document["schema_version"] == 1
    assert document["status"] == "failed"
    assert expected_code in {item["code"] for item in document["diagnostics"]}
    assert document["pull_request_number"] == (23 if source == "event" else None)


def test_local_description_honors_explicit_heading_style_overrides(tmp_path: Path) -> None:
    policy = _policy(tmp_path, "heading-min-level = 1\nheading-max-length = 7\n")
    result = runner.invoke(
        app,
        [*LOCAL, "--policy", str(policy), "--stdin", "--format", "json"],
        input="# Summary\nExplain the request changes.",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["diagnostics"] == []


def test_oversized_heading_report_is_source_located_and_does_not_echo_body(tmp_path: Path) -> None:
    heading = "UNIQUE_HEADING_CONTENT_" + "x" * 1000
    result = runner.invoke(
        app,
        [*LOCAL, "--policy", str(_policy(tmp_path)), "--stdin", "--format", "json"],
        input="\n## " + heading + "\n\nExplain the behavior.",
    )

    assert result.exit_code == 1, result.stderr
    document = json.loads(result.stdout)
    finding = next(
        item for item in document["diagnostics"] if item["code"] == "description.heading-length"
    )
    assert finding["line"] == 2
    assert "UNIQUE_HEADING_CONTENT_" not in result.stdout


def test_template_heading_style_conflict_is_a_configuration_error(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Summary\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path)),
            "--stdin",
            "--template",
            str(template),
            "--format",
            "json",
        ],
        input="## Summary\nExplain the change.",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["kind"] == "configuration"
