"""Unit tests for scripts/action-runtime.cjs and scripts/check_action_distribution.cjs."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.native_release import MANIFEST, TARGETS, asset_name, project_version

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts/action-runtime.cjs"
CHECK_DIST = ROOT / "scripts/check_action_distribution.cjs"
HOST_TARGET = (
    ("aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64")
    + "-"
    + {"win32": "pc-windows-msvc", "darwin": "apple-darwin"}.get(sys.platform, "unknown-linux-musl")
)


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for action-runtime tests")
    return node


def _build_release_dir(
    directory: Path,
    *,
    host_binary: bytes | None = None,
    version: str | None = None,
) -> tuple[str, dict[str, bytes]]:
    directory.mkdir(parents=True, exist_ok=True)
    release_version = version or project_version()
    payloads: dict[str, bytes] = {}
    records = [f"yaga-native-v1 {release_version}"]
    for target in TARGETS:
        name = asset_name(target)
        data = (
            host_binary
            if host_binary is not None and target == HOST_TARGET
            else f"stub-{target}".encode()
        )
        (directory / name).write_bytes(data)
        payloads[name] = data
        records.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    manifest_bytes = ("\n".join(records) + "\n").encode("ascii")
    (directory / MANIFEST).write_bytes(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest(), payloads


def _run_node(
    script: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_node(), str(script), *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for trustedLocal tests")
    clean_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    clean_env["GIT_CONFIG_NOSYSTEM"] = "1"
    clean_env["GIT_CONFIG_GLOBAL"] = os.devnull
    clean_env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.check_output(
        [git, "-C", str(repo), *args],
        env=clean_env,
        text=True,
        timeout=30,
    ).strip()


def test_stage_and_distribution_verify_manifest_and_assets(tmp_path: Path) -> None:
    release = tmp_path / "release"
    pin, payloads = _build_release_dir(release)

    staged = tmp_path / "staged-bundle"
    ok = _run_node(RUNTIME, ["stage", str(release), str(staged), pin])
    assert ok.returncode == 0, ok.stderr
    assert (staged / ".gitattributes").read_text(encoding="utf-8") == "* -text\n"
    assert (staged / "manifest.sha256").read_text(encoding="utf-8") == f"{pin}\n"
    for name, data in payloads.items():
        assert (staged / name).read_bytes() == data

    # Existing destination is rejected.
    dup = _run_node(RUNTIME, ["stage", str(release), str(staged), pin])
    assert dup.returncode == 2
    assert "already exists" in dup.stderr

    # Distribution layout includes action.yml, LICENSE, scripts/action-runtime.cjs, and action-native/.
    dist = tmp_path / "action-dist"
    dist_ok = _run_node(RUNTIME, ["distribution", str(release), str(dist), pin])
    assert dist_ok.returncode == 0, dist_ok.stderr
    assert (dist / "action.yml").read_bytes() == (ROOT / "action.yml").read_bytes()
    assert (dist / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert (dist / "scripts/action-runtime.cjs").read_bytes() == RUNTIME.read_bytes()
    assert (dist / "action-native/manifest.sha256").read_text(encoding="utf-8") == f"{pin}\n"

    # Mismatched manifest SHA-256 pin is rejected.
    bad_pin = _run_node(RUNTIME, ["stage", str(release), str(tmp_path / "bad-pin"), "0" * 64])
    assert bad_pin.returncode == 2
    assert "manifest digest mismatch" in bad_pin.stderr

    # Tampered binary payload is rejected.
    (release / asset_name(HOST_TARGET)).write_bytes(b"tampered")
    bad_bin = _run_node(RUNTIME, ["stage", str(release), str(tmp_path / "bad-bin"), pin])
    assert bad_bin.returncode == 2
    assert "executable digest mismatch" in bad_bin.stderr


def test_runtime_copies_verified_bundle_and_enforces_security_boundaries(tmp_path: Path) -> None:
    release = tmp_path / "release"
    pin, payloads = _build_release_dir(release)
    dist = tmp_path / "action-dist"
    assert _run_node(RUNTIME, ["distribution", str(release), str(dist), pin]).returncode == 0

    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner-temp"
    workspace.mkdir()
    runner_temp.mkdir()
    output = tmp_path / "github-output"

    base_env = dict(
        os.environ,
        GITHUB_ACTION_PATH=str(dist),
        GITHUB_WORKSPACE=str(workspace),
        RUNNER_TEMP=str(runner_temp),
        GITHUB_OUTPUT=str(output),
        YAGA_SETUP_PRIVILEGED="0",
    )

    ok = _run_node(dist / "scripts/action-runtime.cjs", [], env=base_env)
    assert ok.returncode == 0, ok.stderr
    fields = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    copied = Path(fields["binary"])
    assert copied.resolve().is_relative_to(runner_temp.resolve())
    assert copied.read_bytes() == payloads[asset_name(HOST_TARGET)]
    assert fields["start-ms"].isdigit()

    # Reject RUNNER_TEMP inside GITHUB_WORKSPACE.
    nested_temp = workspace / "nested-temp"
    nested_temp.mkdir()
    out_nested = tmp_path / "out-nested"
    bad_temp = _run_node(
        dist / "scripts/action-runtime.cjs",
        [],
        env=base_env | {"RUNNER_TEMP": str(nested_temp), "GITHUB_OUTPUT": str(out_nested)},
    )
    assert bad_temp.returncode == 2
    assert "outside the consumer workspace" in bad_temp.stderr
    assert "exit-code=2" in out_nested.read_text(encoding="utf-8")

    # Reject privileged non-local Action without a 40-char immutable commit SHA.
    out_priv = tmp_path / "out-priv"
    bad_ref = _run_node(
        dist / "scripts/action-runtime.cjs",
        [],
        env=base_env
        | {
            "YAGA_SETUP_PRIVILEGED": "1",
            "YAGA_SETUP_ACTION_REF": "v0.6.0",
            "GITHUB_OUTPUT": str(out_priv),
        },
    )
    assert bad_ref.returncode == 2
    assert "immutable full Action commit" in bad_ref.stderr
    assert "exit-code=2" in out_priv.read_text(encoding="utf-8")

    # Reject tampered binary in action-native/.
    (dist / "action-native" / asset_name(HOST_TARGET)).write_bytes(b"corrupt")
    out_corrupt = tmp_path / "out-corrupt"
    bad_hash = _run_node(
        dist / "scripts/action-runtime.cjs",
        [],
        env=base_env | {"GITHUB_OUTPUT": str(out_corrupt)},
    )
    assert bad_hash.returncode == 2
    assert "executable digest mismatch" in bad_hash.stderr


def test_runtime_trusted_local_requires_committed_default_branch_bundle(tmp_path: Path) -> None:
    release = tmp_path / "release"
    pin, payloads = _build_release_dir(release)

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "--initial-branch=main", "--template=")
    _run_git(repo, "config", "user.name", "YAGA Tests")
    _run_git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "scripts").mkdir()
    shutil.copyfile(RUNTIME, repo / "scripts/action-runtime.cjs")
    _run_git(repo, "add", "scripts/action-runtime.cjs")
    _run_git(repo, "commit", "--no-verify", "--no-gpg-sign", "-m", "chore: initial runtime")
    uncommitted_sha = _run_git(repo, "rev-parse", "HEAD")

    # Stage action-native/ without committing it yet.
    assert (
        _run_node(RUNTIME, ["stage", str(release), str(repo / "action-native"), pin]).returncode
        == 0
    )

    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"repository": {"full_name": "owner/yaga", "default_branch": "main"}}),
        encoding="utf-8",
    )
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()

    def run_trusted(sha: str, *, event_name: str = "pull_request_target") -> tuple[int, str, Path]:
        output = tmp_path / f"out-{sha[:8]}-{event_name}"
        env = dict(
            os.environ,
            GITHUB_ACTION_PATH=str(repo),
            GITHUB_WORKSPACE=str(repo),
            RUNNER_TEMP=str(runner_temp),
            GITHUB_OUTPUT=str(output),
            GITHUB_EVENT_NAME=event_name,
            GITHUB_EVENT_PATH=str(event_path),
            GITHUB_REPOSITORY="owner/yaga",
            GITHUB_REF="refs/heads/main",
            GITHUB_SHA=sha,
            YAGA_SETUP_PRIVILEGED="1",
        )
        res = _run_node(repo / "scripts/action-runtime.cjs", [], env=env)
        return res.returncode, res.stderr, output

    # Untrusted event is rejected even when action == workspace.
    code, err, _ = run_trusted(uncommitted_sha, event_name="pull_request")
    assert code == 2
    assert "trusted default-branch checkout" in err

    # Uncommitted action-native/ is rejected in privileged local mode.
    code, err, _ = run_trusted(uncommitted_sha)
    assert code == 2
    assert "uncommitted native distribution" in err

    # Once committed at GITHUB_SHA, trusted local mode verifies the committed manifest pin.
    _run_git(repo, "add", "action-native")
    _run_git(repo, "commit", "--no-verify", "--no-gpg-sign", "-m", "chore: commit native bundle")
    committed_sha = _run_git(repo, "rev-parse", "HEAD")

    # Mismatched GITHUB_SHA vs HEAD is rejected.
    code, err, _ = run_trusted(uncommitted_sha)
    assert code == 2
    assert "must match the runner commit" in err

    # Exact committed HEAD passes and extracts the verified binary.
    code, err, out = run_trusted(committed_sha)
    assert code == 0, err
    fields = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    assert Path(fields["binary"]).read_bytes() == payloads[asset_name(HOST_TARGET)]


def test_check_action_distribution_verifies_staged_binary_and_rejects_drift(
    tmp_path: Path, native_action_binary: Path
) -> None:
    release = tmp_path / "release"
    pin, _ = _build_release_dir(release, host_binary=native_action_binary.read_bytes())
    dist = tmp_path / "dist"
    assert _run_node(RUNTIME, ["distribution", str(release), str(dist), pin]).returncode == 0

    source_files = ["action.yml", "LICENSE", "scripts/action-runtime.cjs"]
    listing = "".join(
        f"{hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}  {name}\n"
        for name in source_files
    )
    source_files_sha = hashlib.sha256(listing.encode("utf-8")).hexdigest()
    source_commit = "a" * 40

    ok = _run_node(
        CHECK_DIST,
        [str(dist), HOST_TARGET, source_commit, pin, source_files_sha],
    )
    assert ok.returncode == 0, ok.stderr
    report = json.loads(ok.stdout)
    assert report["target"] == HOST_TARGET
    assert report["manifest_sha256"] == pin
    assert report["branch_exits"] == [0, 1]

    # Wrong source-files SHA-256 fails verification.
    bad = _run_node(
        CHECK_DIST,
        [str(dist), HOST_TARGET, source_commit, pin, "0" * 64],
    )
    assert bad.returncode != 0
    assert "unexpected staged Action source files" in bad.stderr
