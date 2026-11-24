"""Qualify the Rust workspace, standalone CLI, Rust consumer API, and Python wheel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from check_build import ROOT, exercise_installed_wheel, validate_wheel


def executable(environment: dict[str, str], target: str, *selectors: str) -> str:
    artifacts = subprocess.check_output(
        ["cargo", "build", "--message-format=json-render-diagnostics", *selectors],
        cwd=ROOT,
        env=environment,
        text=True,
    )
    return next(
        item["executable"]
        for line in artifacts.splitlines()
        if (item := json.loads(line)).get("executable") and item["target"]["name"] == target
    )


def exercise_rust_consumer(directory: Path, environment: dict[str, str]) -> None:
    """Compile an independent Rust project against yaga-core and run it without Python."""
    project = directory / "rust-consumer"
    (project / "src").mkdir(parents=True)
    core_path = json.dumps((ROOT / "crates" / "yaga-core").as_posix(), ensure_ascii=False)
    manifest = project / "Cargo.toml"
    manifest.write_text(
        '[package]\nname = "yaga-consumer"\nversion = "0.1.0"\nedition = "2024"\n'
        f"[dependencies]\nyaga-core = {{ path = {core_path} }}\n",
        encoding="utf-8",
    )
    (project / "src/main.rs").write_text(
        r"""
use std::path::PathBuf;
use yaga_core::branch::check_branch;
use yaga_core::test_planning::{PlanDocument, Source, TestPolicy, plan_tests};

fn main() {
    let path = PathBuf::from(std::env::args_os().nth(1).unwrap());
    assert!(check_branch(&path, "feat/native").unwrap().report.valid());
    assert!(!check_branch(&path, "other/native").unwrap().report.valid());
    let policy = TestPolicy::parse(
        b"test-policy-version=1\n[[suites]]\nid='unit'\nbinding='cargo'\n[profiles.full]\nmode='full'\n"
    ).unwrap();
    let selection = plan_tests(&policy, "full", None).unwrap();
    let source = Source {
        base_commit: None, comparison_commit: None, head_commit: None,
        execution_commit: "1".repeat(40), execution_tree: "2".repeat(40),
    };
    let plan = PlanDocument::new(&"a".repeat(64), None, source, selection).unwrap();
    assert_eq!(plan.plan_digest, "f114a9dbe65a4ae2ada42815694ae5b62432b8467eb769f47c77659ccfa813ff");
}
""",
        encoding="utf-8",
    )
    shutil.copyfile(ROOT / "Cargo.lock", project / "Cargo.lock")
    build_env = environment | {"CARGO_NET_OFFLINE": "true"}
    build_env.setdefault("CARGO_TARGET_DIR", str(ROOT / "target"))
    binary = executable(build_env, "yaga-consumer", "--manifest-path", str(manifest))
    policy = project / "branch.toml"
    policy.write_text(
        'branch-policy-version = 1\nallowed-patterns = ["feat/**"]\n', encoding="utf-8"
    )
    subprocess.run(
        [binary, str(policy)], cwd=project, env=environment | {"PATH": str(project)}, check=True
    )


def main() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the native wheel build")
    environment = dict(os.environ)
    for args in (
        ["fmt", "--all", "--check"],
        ["clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
        ["test", "--workspace", "--locked"],
    ):
        subprocess.run(["cargo", *args], cwd=ROOT, env=environment, check=True)
    binary = executable(environment, "yaga", "--release", "--locked", "--package", "yaga-cli")
    with tempfile.TemporaryDirectory(prefix="yaga-native-") as temporary:
        root = Path(temporary)
        exercise_rust_consumer(root, environment)
        subprocess.run(
            [uv, "build", "--wheel", "--no-build-isolation", "--out-dir", str(root)],
            cwd=ROOT,
            env=environment | {"YAGA_PREBUILT_BINARY": binary},
            check=True,
        )
        (wheel,) = root.glob("*-py3-none-*.whl")
        validate_wheel(wheel)
        exercise_installed_wheel(uv, root, wheel)
        subprocess.run(
            [sys.executable, "-m", "pytest", str(ROOT / "tests")],
            cwd=ROOT,
            env=environment | {"YAGA_TEST_NATIVE_BINARY": binary},
            check=True,
        )


if __name__ == "__main__":
    main()
