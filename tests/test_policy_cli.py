"""Tests for yaga policy init, interactive checkbox selector, and yaga policy check."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.policies.init_templates import existing_policy_status
from yaga.policies.schemas import POLICY_SPECS, PolicyKind
from yaga.policies.selector import (
    KEY_ALL,
    KEY_CANCEL,
    KEY_DOWN,
    KEY_ENTER,
    KEY_NOOP,
    KEY_SPACE,
    KEY_UP,
    _classify_common_char,
    select_policies_interactively,
)

runner = CliRunner()


@pytest.mark.parametrize("kind", list(PolicyKind))
def test_policy_init_each_kind_stamps_latest_version_and_passes_strict_audit(
    tmp_path: Path,
    kind: PolicyKind,
) -> None:
    spec = POLICY_SPECS[kind]
    init_res = runner.invoke(
        app,
        ["policy", "init", kind.value, "--repo", str(tmp_path), "--format", "json"],
    )
    assert init_res.exit_code == 0, init_res.output
    payload = json.loads(init_res.stdout)
    assert payload["schema_version"] == 1
    assert payload["dry_run"] is False
    assert payload["created"] == 1
    target = tmp_path / spec.default_relpath
    assert target.is_file()
    assert payload["policies"][0]["kind"] == kind.value
    assert payload["policies"][0]["schema_version"] == spec.current_version_int
    assert payload["policies"][0]["status"] == "created"

    check_res = runner.invoke(
        app,
        ["policy", "check", str(target), "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert check_res.exit_code == 0, check_res.output
    audit = json.loads(check_res.stdout)
    assert audit["valid"] is True
    assert audit["checked"] == 1
    assert audit["current"] == 1
    assert audit["deprecated"] == 0
    assert audit["invalid"] == 0
    assert audit["policies"][0]["kind"] == kind.value
    assert audit["policies"][0]["source_version"] == spec.current_version_int
    assert audit["policies"][0]["current_version"] == spec.current_version_int


def test_policy_init_all_wires_committed_tree_policies_into_repo_plan(tmp_path: Path) -> None:
    init_res = runner.invoke(app, ["policy", "init", "--all", "--repo", str(tmp_path)])
    assert init_res.exit_code == 0, init_res.output
    assert init_res.stdout.count("CREATED") == len(PolicyKind)

    plan_doc = tomllib.loads((tmp_path / ".yaga/checks/ci.toml").read_text(encoding="utf-8"))
    assert plan_doc["plan-version"] == 2
    assert set(plan_doc["checks"]) >= {"mode", "path", "size", "tree"}
    assert plan_doc["mode-policy"] == ".yaga/mode-policy.toml"
    assert plan_doc["path-policy"] == ".yaga/path-policy.toml"
    assert plan_doc["size-policy"] == ".yaga/size-policy.toml"
    assert plan_doc["tree-policy"] == ".yaga/tree-policy.toml"

    check_res = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert check_res.exit_code == 0, check_res.output
    audit = json.loads(check_res.stdout)
    assert audit["checked"] == len(PolicyKind)
    assert audit["current"] == len(PolicyKind)
    assert audit["deprecated"] == 0
    assert audit["invalid"] == 0


def test_policy_init_dry_run_skip_existing_and_overwrite_protection(tmp_path: Path) -> None:
    dry_res = runner.invoke(
        app,
        ["policy", "init", "branch", "tree", "--dry-run", "--repo", str(tmp_path)],
    )
    assert dry_res.exit_code == 0, dry_res.output
    assert "dry-run (no files created)" in dry_res.stdout
    assert dry_res.stdout.count("PREVIEW") == 2
    assert not (tmp_path / ".yaga").exists()

    first = runner.invoke(app, ["policy", "init", "branch", "--repo", str(tmp_path)])
    assert first.exit_code == 0, first.output

    conflict = runner.invoke(app, ["policy", "init", "branch", "tree", "--repo", str(tmp_path)])
    assert conflict.exit_code == 2
    assert "already exists" in conflict.stderr
    assert not (tmp_path / ".yaga/tree-policy.toml").exists()

    skipped = runner.invoke(
        app,
        [
            "policy",
            "init",
            "branch",
            "tree",
            "--skip-existing",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
        ],
    )
    assert skipped.exit_code == 0, skipped.output
    doc = json.loads(skipped.stdout)
    assert doc["created"] == 1
    assert doc["skipped"] == 1
    assert (tmp_path / ".yaga/tree-policy.toml").is_file()


def test_policy_init_non_tty_and_conflicting_flags_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("yaga.commands.policy.is_interactive_terminal", lambda: False)
    no_args = runner.invoke(app, ["policy", "init", "--repo", str(tmp_path)])
    assert no_args.exit_code == 2
    assert "select one or more policy names, pass --all, or run interactively" in no_args.stderr

    mixed = runner.invoke(app, ["policy", "init", "branch", "--all", "--repo", str(tmp_path)])
    assert mixed.exit_code == 2
    assert "choose either explicit policy names or --all" in mixed.stderr


def test_interactive_checkbox_selector_navigation_all_and_cancel(tmp_path: Path) -> None:
    (tmp_path / ".yaga.toml").write_text("config-version = 1\n", encoding="utf-8")
    existing = existing_policy_status(tmp_path)
    assert existing[PolicyKind.COMMIT] is not None
    assert existing[PolicyKind.REPO] is None

    # Initial cursor lands on first non-existing item (REPO at index 1).
    # Toggle REPO, move down to BRANCH, toggle BRANCH, confirm.
    keys = iter([KEY_SPACE, KEY_DOWN, KEY_SPACE, KEY_NOOP, KEY_ENTER])
    buffer = io.StringIO()
    chosen = select_policies_interactively(existing, read_key=lambda: next(keys), output=buffer)
    assert chosen == (PolicyKind.REPO, PolicyKind.BRANCH)
    rendered = buffer.getvalue()
    assert "already exists: .yaga.toml" in rendered
    assert "[x]" in rendered

    # 'all' selects every non-existing policy, pressing 'all' again clears them.
    all_then_clear = iter([KEY_ALL, KEY_ENTER])
    all_selected = select_policies_interactively(
        existing,
        read_key=lambda: next(all_then_clear),
        output=io.StringIO(),
    )
    assert all_selected is not None
    assert PolicyKind.COMMIT not in all_selected
    assert len(all_selected) == len(PolicyKind) - 1

    toggle_clear = iter([KEY_ALL, KEY_ALL, KEY_UP, KEY_ENTER])
    cleared = select_policies_interactively(
        existing,
        read_key=lambda: next(toggle_clear),
        output=io.StringIO(),
    )
    assert cleared == ()

    cancelled = select_policies_interactively(
        existing,
        read_key=lambda: KEY_CANCEL,
        output=io.StringIO(),
    )
    assert cancelled is None

    assert _classify_common_char("k") == KEY_UP
    assert _classify_common_char("j") == KEY_DOWN
    assert _classify_common_char(" ") == KEY_SPACE
    assert _classify_common_char("a") == KEY_ALL
    assert _classify_common_char("\r") == KEY_ENTER
    assert _classify_common_char("q") == KEY_CANCEL
    assert _classify_common_char("z") == KEY_NOOP


def test_policy_init_interactive_cli_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("yaga.commands.policy.is_interactive_terminal", lambda: True)

    monkeypatch.setattr(
        "yaga.commands.policy.select_policies_interactively",
        lambda _existing: (PolicyKind.BRANCH, PolicyKind.PATH),
    )
    ok_res = runner.invoke(app, ["policy", "init", "--repo", str(tmp_path)])
    assert ok_res.exit_code == 0, ok_res.output
    assert (tmp_path / ".yaga/branch-policy.toml").is_file()
    assert (tmp_path / ".yaga/path-policy.toml").is_file()

    monkeypatch.setattr(
        "yaga.commands.policy.select_policies_interactively", lambda _existing: None
    )
    cancel_res = runner.invoke(app, ["policy", "init", "--repo", str(tmp_path)])
    assert cancel_res.exit_code == 0
    assert "cancelled" in cancel_res.stdout

    monkeypatch.setattr("yaga.commands.policy.select_policies_interactively", lambda _existing: ())
    empty_res = runner.invoke(app, ["policy", "init", "--repo", str(tmp_path)])
    assert empty_res.exit_code == 0
    assert "No policies selected" in empty_res.stdout


def test_policy_check_detects_deprecated_plan_v1_and_enforces_strict(tmp_path: Path) -> None:
    plan = tmp_path / ".yaga/checks/ci.toml"
    plan.parent.mkdir(parents=True)
    plan.write_text('plan-version = 1\nchecks = ["workflow"]\n', encoding="utf-8")

    advisory = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--format", "github"],
    )
    assert advisory.exit_code == 0, advisory.output
    assert "::warning title=YAGA policy,file=" in advisory.stdout
    assert "deprecated" in advisory.stdout

    strict_gh = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "github"],
    )
    assert strict_gh.exit_code == 1
    assert "::error title=YAGA policy,file=" in strict_gh.stdout

    strict_json = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert strict_json.exit_code == 1
    doc = json.loads(strict_json.stdout)
    assert doc["valid"] is False
    assert doc["deprecated"] == 1
    assert doc["policies"][0]["status"] == "deprecated"
    assert doc["policies"][0]["source_version"] == 1
    assert doc["policies"][0]["current_version"] == 2


@pytest.mark.parametrize(
    ("relpath", "content"),
    [
        (".yaga.toml", "config-version = 99\n"),
        (".yaga/checks/ci.toml", 'plan-version = 99\nchecks = ["workflow"]\n'),
        (".yaga/branch-policy.toml", 'branch-policy-version = 99\nallowed-patterns = ["main"]\n'),
        (
            ".yaga/change-policy.toml",
            'change-policy-version = 99\n[[rules]]\nname = "r"\nwhen-any = ["a"]\nrequire-any = ["b"]\n',
        ),
        (".yaga/description-policy.toml", "description-policy-version = 99\n"),
        (
            ".yaga/mode-policy.toml",
            'mode-policy-version = 99\ndefault-allowed-modes = ["regular"]\n',
        ),
        (".yaga/path-policy.toml", 'path-policy-version = 99\nprofile = "windows-compatible-v1"\n'),
        (".yaga/size-policy.toml", "size-policy-version = 99\ndefault-max-blob-bytes = 1024\n"),
        (".yaga/tree-policy.toml", 'tree-policy-version = 99\nrequired-paths = ["README.md"]\n'),
        (
            ".yaga/agent-review-policy.toml",
            '[agent-review]\nversion = 99\nrequired = ["codex"]\n[agent-review.agents.codex]\npreset = "codex"\n',
        ),
        (
            ".yaga/test-policy.toml",
            'test-policy-version = 99\n[[suites]]\nid = "u"\nbinding = "pytest"\n[profiles.full]\nmode = "full"\n',
        ),
    ],
)
def test_future_schema_versions_emit_upgrade_hint_and_exit_2(
    tmp_path: Path,
    relpath: str,
    content: str,
) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    res = runner.invoke(app, ["policy", "check", str(target), "--repo", str(tmp_path)])
    assert res.exit_code == 2
    assert "newer schema version 99" in res.stderr
    assert "update YAGA using its installation manager" in res.stderr


def test_required_version_floor_and_coexisting_commit_and_agent_review_sections(
    tmp_path: Path,
) -> None:
    yaga_toml = tmp_path / ".yaga.toml"
    yaga_toml.write_text(
        "config-version = 1\n"
        'required-version = ">=0.1.0"\n\n'
        "[commit]\n"
        'body-policy = "optional"\n\n'
        "[agent-review]\n"
        "version = 1\n"
        'required-version = ">=0.1.0"\n'
        'required = ["codex"]\n\n'
        "[agent-review.agents.codex]\n"
        'preset = "codex"\n',
        encoding="utf-8",
    )
    ok_res = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert ok_res.exit_code == 0, ok_res.output
    doc = json.loads(ok_res.stdout)
    assert doc["checked"] == 2
    assert {item["kind"] for item in doc["policies"]} == {"commit", "agent-review"}

    branch_policy = tmp_path / ".yaga/branch-policy.toml"
    branch_policy.parent.mkdir(parents=True)
    branch_policy.write_text(
        'branch-policy-version = 1\nrequired-version = ">=99.0.0"\nallowed-patterns = ["main"]\n',
        encoding="utf-8",
    )
    unmet = runner.invoke(app, ["policy", "check", str(branch_policy), "--repo", str(tmp_path)])
    assert unmet.exit_code == 2
    assert "does not satisfy required-version" in unmet.stderr

    # Frozen plan v1 must continue rejecting required-version as an unknown key.
    plan_v1 = tmp_path / ".yaga/checks/v1.toml"
    plan_v1.parent.mkdir(parents=True)
    plan_v1.write_text(
        'plan-version = 1\nrequired-version = ">=0.1.0"\nchecks = ["workflow"]\n',
        encoding="utf-8",
    )
    v1_res = runner.invoke(app, ["policy", "check", str(plan_v1), "--repo", str(tmp_path)])
    assert v1_res.exit_code == 2
    assert "unknown repository check plan key(s)" in v1_res.stderr
    assert "required-version" in v1_res.stderr


def test_cli_action_policy_mode_executes_end_to_end(tmp_path: Path) -> None:
    init_res = runner.invoke(app, ["policy", "init", "branch", "--repo", str(tmp_path)])
    assert init_res.exit_code == 0

    completed = subprocess.run(
        [sys.executable, "-m", "yaga.cli_action"],
        env={
            **os.environ,
            "GITHUB_WORKSPACE": str(tmp_path),
            "YAGA_INPUT_CHECK": "policy",
            "YAGA_NON_CLI_INPUTS": "0",
            "YAGA_HAS_GITHUB_TOKEN": "0",
            "YAGA_GH_TOKEN": "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 current, 0 deprecated, 0 invalid" in completed.stdout


def test_policy_check_terminates_on_in_repo_directory_cycle_in_yaga_dir(tmp_path: Path) -> None:
    yaga_dir = tmp_path / ".yaga"
    empty_dir = yaga_dir / "empty"
    empty_dir.mkdir(parents=True)
    (yaga_dir / "branch-policy.toml").write_text(
        'branch-policy-version = 1\nallowed-patterns = ["main"]\n',
        encoding="utf-8",
    )
    loop_path = empty_dir / "loop"
    try:
        loop_path.symlink_to(yaga_dir, target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(loop_path), str(yaga_dir)],
                check=True,
                capture_output=True,
            )
        else:
            raise

    res = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["checked"] == 1
    assert doc["current"] == 1
    assert doc["invalid"] == 0


def test_policy_check_reports_malformed_pyproject_instead_of_skipping_it(
    tmp_path: Path,
) -> None:
    yaga_dir = tmp_path / ".yaga"
    yaga_dir.mkdir()
    (yaga_dir / "branch-policy.toml").write_text(
        'branch-policy-version = 1\nallowed-patterns = ["main"]\n',
        encoding="utf-8",
    )
    pyproject = tmp_path / "pyproject.toml"

    # Valid pyproject.toml without [tool.yaga] is ignored during repository discovery.
    pyproject.write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    clean = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["checked"] == 1

    # Malformed pyproject.toml alongside valid .yaga policies must fail closed as invalid.
    pyproject.write_text("[tool.yaga\nconfig-version = 1\n", encoding="utf-8")
    broken = runner.invoke(
        app,
        ["policy", "check", "--repo", str(tmp_path), "--strict", "--format", "json"],
    )
    assert broken.exit_code == 2
    broken_doc = json.loads(broken.stderr)
    assert broken_doc["checked"] == 2
    assert broken_doc["current"] == 1
    assert broken_doc["invalid"] == 1
    invalid_entries = [item for item in broken_doc["policies"] if item["status"] == "invalid"]
    assert len(invalid_entries) == 1
    assert invalid_entries[0]["kind"] == "commit"
    assert "invalid UTF-8 TOML" in invalid_entries[0]["message"]

    # Invalid UTF-8 pyproject.toml alone must also report invalid rather than "no policy files".
    solo_repo = tmp_path / "solo"
    solo_repo.mkdir()
    (solo_repo / "pyproject.toml").write_bytes(b"\xff\xfe\x00invalid")
    solo_res = runner.invoke(app, ["policy", "check", "--repo", str(solo_repo)])
    assert solo_res.exit_code == 2
    assert "invalid UTF-8 TOML" in solo_res.stderr


def test_policy_init_preserves_pyproject_when_commit_policy_fails_validation(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.yaga]\nrequired-version = ">=99.0.0"\n\n[tool.yaga.commit]\nbody-policy = "required"\n',
        encoding="utf-8",
    )

    existing = existing_policy_status(tmp_path)
    assert existing[PolicyKind.COMMIT] == pyproject

    buffer = io.StringIO()
    chosen = select_policies_interactively(existing, read_key=lambda: KEY_CANCEL, output=buffer)
    assert chosen is None
    assert "already exists: pyproject.toml" in buffer.getvalue()

    conflict = runner.invoke(
        app,
        ["policy", "init", "commit", "--repo", str(tmp_path), "--format", "json"],
    )
    assert conflict.exit_code == 2
    err_doc = json.loads(conflict.stderr)
    assert err_doc["kind"] == "policy_init"
    assert "commit configuration already applies" in err_doc["error"]["message"]
    assert not (tmp_path / ".yaga.toml").exists()

    skipped = runner.invoke(
        app,
        [
            "policy",
            "init",
            "commit",
            "branch",
            "--skip-existing",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
        ],
    )
    assert skipped.exit_code == 0, skipped.output
    skipped_doc = json.loads(skipped.stdout)
    assert skipped_doc["skipped"] == 1
    assert skipped_doc["created"] == 1
    assert not (tmp_path / ".yaga.toml").exists()
    assert (tmp_path / ".yaga/branch-policy.toml").is_file()

    # Subsequent policy check still sees the original pyproject.toml failure unshadowed.
    audit_res = runner.invoke(app, ["policy", "check", "--repo", str(tmp_path)])
    assert audit_res.exit_code == 2
    assert "does not satisfy required-version" in audit_res.stderr


def test_policy_init_fails_closed_on_malformed_pyproject_when_initializing_commit(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.yaga\nconfig-version = 1\n", encoding="utf-8")

    # Initializing a non-commit policy does not touch or shadow pyproject.toml.
    branch_ok = runner.invoke(app, ["policy", "init", "branch", "--repo", str(tmp_path)])
    assert branch_ok.exit_code == 0, branch_ok.output
    assert (tmp_path / ".yaga/branch-policy.toml").is_file()
    assert not (tmp_path / ".yaga.toml").exists()

    # Initializing commit (even with --skip-existing) fails closed instead of shadowing pyproject.toml.
    for extra_flags in ([], ["--skip-existing"]):
        commit_res = runner.invoke(
            app,
            ["policy", "init", "commit", *extra_flags, "--repo", str(tmp_path)],
        )
        assert commit_res.exit_code == 2
        assert "invalid TOML" in commit_res.stderr
        assert not (tmp_path / ".yaga.toml").exists()
