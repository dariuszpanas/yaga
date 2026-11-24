"""Build and smoke-test the zero-dependency Python wheel and source distribution."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.utils import parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_WHEEL_PYTHON = {
    "yaga/__init__.py",
    "yaga/__main__.py",
    "yaga/_update_worker.py",
    "yaga/entrypoint.py",
    "yaga/self_update.py",
}


def run_smoke_git(git: str, consumer: Path, arguments: list[str]) -> str:
    """Run Git in an isolated consumer fixture without inheriting ambient GIT_* state."""
    clean_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    clean_env["GIT_CONFIG_NOSYSTEM"] = "1"
    clean_env["GIT_CONFIG_GLOBAL"] = os.devnull
    clean_env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        [git, "-C", str(consumer), *arguments],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def validate_wheel(wheel_path: Path) -> None:
    """Verify the wheel is a py3-none-<platform> zero-dependency binary launcher."""
    _, _, _, tags = parse_wheel_filename(wheel_path.name)
    if len(tags) != 1:
        raise SystemExit(f"expected single-tag platform wheel, got {wheel_path.name}")
    tag = next(iter(tags))
    if tag.interpreter != "py3" or tag.abi != "none" or tag.platform == "any":
        raise SystemExit(f"expected py3-none-<platform> wheel tag, got {tag}")

    windows = tag.platform.startswith("win")
    binary_member = "yaga/_bin/yaga.exe" if windows else "yaga/_bin/yaga"

    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        if binary_member not in names:
            raise SystemExit(f"wheel missing bundled executable {binary_member}")
        if any(name.startswith("yaga/_native.") for name in names):
            raise SystemExit("wheel must not bundle a CPython _native extension")
        info = archive.getinfo(binary_member)
        if not 0 < info.file_size <= 128 * 1024 * 1024:
            raise SystemExit(f"unexpected bundled executable size: {info.file_size}")
        if not windows and not (info.external_attr >> 16 & stat.S_IXUSR):
            raise SystemExit("bundled Unix executable lacks execute permission")

        py_files = {n for n in names if n.startswith("yaga/") and n.endswith((".py", ".pyi"))}
        if py_files != EXPECTED_WHEEL_PYTHON:
            raise SystemExit(
                f"unexpected Python files in wheel: {sorted(py_files ^ EXPECTED_WHEEL_PYTHON)}"
            )

        (metadata_member,) = [n for n in names if n.endswith(".dist-info/METADATA")]
        metadata = BytesParser().parsebytes(archive.read(metadata_member))
        requires = metadata.get_all("Requires-Dist") or []
        if requires:
            raise SystemExit(f"wheel must have zero runtime dependencies, found: {requires}")


def validate_sdist(sdist_path: Path) -> None:
    """Verify the source distribution excludes build/cache artifacts and includes Cargo sources."""
    forbidden_parts = {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        "dist",
        "site",
    }
    with tarfile.open(sdist_path, "r:gz") as archive:
        members = archive.getnames()
        for member in members:
            parts = set(Path(member).parts[1:])
            bad = parts & forbidden_parts
            if bad:
                raise SystemExit(f"sdist contains forbidden artifact path: {member}")
        relative = {"/".join(Path(m).parts[1:]) for m in members}
        for required in (
            "Cargo.toml",
            "Cargo.lock",
            "pyproject.toml",
            "action.yml",
            "crates/yaga-cli/Cargo.toml",
            "crates/yaga-core/Cargo.toml",
            "src/yaga/entrypoint.py",
        ):
            if required not in relative:
                raise SystemExit(f"sdist missing required file: {required}")
        if any(r.startswith("crates/yaga-python/") for r in relative):
            raise SystemExit("sdist must not contain retired crates/yaga-python")


def exercise_installed_wheel(uv: str, output: Path, wheel: Path) -> None:
    """Install the wheel with --no-deps into an isolated venv and smoke-test CLI commands."""
    venv = output / "wheel-venv"
    subprocess.run(
        [uv, "--no-config", "venv", "--no-project", "--python", sys.executable, str(venv)],
        cwd=output,
        check=True,
    )
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    yaga_exe = bin_dir / ("yaga.exe" if os.name == "nt" else "yaga")

    subprocess.run(
        [uv, "--no-config", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        cwd=output,
        check=True,
    )

    # Confirm zero extra runtime packages were installed and find_yaga_bin resolves inside venv.
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata, pathlib, sys, yaga; "
            "dists = {d.metadata['Name'] for d in importlib.metadata.distributions()}; "
            "assert dists == {'yaga-cli'}, dists; "
            "exe = yaga.find_yaga_bin().resolve(); "
            "assert exe.is_relative_to(pathlib.Path(sys.prefix).resolve()) and exe.is_file()",
        ],
        cwd=output,
        check=True,
    )

    consumer = output / "consumer"
    consumer.mkdir()

    # Check both console entry point and python -m yaga
    for entry in ([str(yaga_exe)], [str(python), "-I", "-m", "yaga"]):
        out = subprocess.check_output([*entry, "--version"], cwd=consumer, text=True, timeout=30)
        if not out.strip().startswith("yaga "):
            raise SystemExit(f"unexpected version output: {out!r}")

    # Smoke test core checks end-to-end
    commit = subprocess.run(
        [
            str(yaga_exe),
            "commit",
            "check",
            "--message",
            "feat(wheel): verify installed launcher\n\nConfirm the bundled Rust CLI works end to end.",
            "--format",
            "json",
        ],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    if json.loads(commit.stdout).get("valid") is not True:
        raise SystemExit("installed commit check returned valid != true")

    branch_policy = consumer / "branch.toml"
    branch_policy.write_text(
        'branch-policy-version = 1\nallowed-patterns = ["feat/*"]\n', encoding="utf-8"
    )
    subprocess.run(
        [str(yaga_exe), "branch", "check", "--policy", str(branch_policy), "--name", "feat/smoke"],
        cwd=consumer,
        check=True,
        timeout=30,
    )

    subprocess.run(
        [str(yaga_exe), "policy", "init", "--all", "--repo", str(consumer)],
        cwd=consumer,
        check=True,
        timeout=30,
    )
    subprocess.run(
        [str(yaga_exe), "policy", "check", "--strict", "--repo", str(consumer)],
        cwd=consumer,
        check=True,
        timeout=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Retain verified dist artifacts here")
    args = parser.parse_args()

    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to build distributions")

    with tempfile.TemporaryDirectory(prefix="yaga-build-check-") as temp:
        work = Path(temp)
        dist_dir = work / "dist"
        dist_dir.mkdir()
        subprocess.run(
            [uv, "build", "--no-build-isolation", "--out-dir", str(dist_dir)],
            cwd=ROOT,
            check=True,
        )
        (wheel,) = dist_dir.glob("*.whl")
        (sdist,) = dist_dir.glob("*.tar.gz")
        validate_wheel(wheel)
        validate_sdist(sdist)
        exercise_installed_wheel(uv, work, wheel)
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for artifact in (wheel, sdist):
                shutil.copy2(artifact, args.output_dir / artifact.name)
    print("Verified zero-dependency native wheel and source distribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
