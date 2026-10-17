"""Real GitHub signatures and conservative failures of the offline exemption."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from yaga.commits import github_signature as signature
from yaga.errors import GitError
from yaga.git import GitRepository, ProcessResult, open_repository
from yaga.git import runtime as git_runtime

FIXTURES = Path(__file__).parent / "fixtures" / "github-signatures"
KNOWN_SHAS = {
    "dependabot-urllib3.commit": "2dea34660d96cdc91ae89eb11b445f816fbf3cea",
    "dependabot-setup-uv.commit": "6680afa1ca995c956871005c280d24e06007dd44",
}
FINGERPRINT = "968479A1AFF927E37D1A566BB5690EEEBB952194"
VALID_STATUS = (
    "[GNUPG:] NEWSIG\n"
    f"[GNUPG:] KEY_CONSIDERED {FINGERPRINT} 0\n"
    "[GNUPG:] GOODSIG B5690EEEBB952194 GitHub <noreply@github.com>\n"
    f"[GNUPG:] VALIDSIG {FINGERPRINT} 2026-10-11 1791682550 0 4 0 1 8 00 {FINGERPRINT}\n"
).encode("ascii")
GOOD_RESULT = ProcessResult(0, VALID_STATUS, b"", False, False, False)


@pytest.fixture
def repository(tmp_path: Path) -> GitRepository:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    return open_repository(tmp_path)


@pytest.fixture
def real_gpgv(monkeypatch: pytest.MonkeyPatch) -> Path:
    executable = shutil.which("gpgv")
    if executable is None and os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).parent.parent / "usr" / "bin" / "gpgv.exe"
            if candidate.is_file():
                executable = str(candidate)
    if executable is None:
        pytest.skip("real gpgv is unavailable")
    path = Path(executable).resolve(strict=True)
    monkeypatch.setenv("PATH", str(path.parent) + os.pathsep + os.environ.get("PATH", ""))
    return path


def _store(repository: GitRepository, raw: bytes) -> str:
    return (
        subprocess.check_output(
            [
                repository.executable,
                "-C",
                str(repository.directory),
                "hash-object",
                "-w",
                "-t",
                "commit",
                "--stdin",
            ],
            input=raw,
        )
        .decode("ascii")
        .strip()
    )


def _raw() -> bytes:
    return (FIXTURES / "dependabot-urllib3.commit").read_bytes()


def _allow_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signature, "find_external_executable", lambda *_: "trusted-gpgv")


@pytest.mark.parametrize("name", KNOWN_SHAS)
def test_real_github_signatures_are_verified_offline(
    repository: GitRepository, real_gpgv: Path, name: str
) -> None:
    raw = (FIXTURES / name).read_bytes()
    sha = _store(repository, raw)
    assert sha == KNOWN_SHAS[name]
    assert signature.verified_github_commits(repository, (sha,)) == {sha}


def test_vendor_keyring_matches_the_reviewed_public_fixture() -> None:
    lines = (FIXTURES / "web-flow.asc").read_text(encoding="ascii").splitlines()
    encoded = "".join(line for line in lines if line and not line.startswith(("-", "=")))
    assert base64.b64decode(signature._KEYRING_BASE64) == base64.b64decode(encoded)
    assert signature.PINNED_FINGERPRINT == FINGERPRINT


@pytest.mark.parametrize("mutation", ["message", "author", "tree", "copied-signature", "unsigned"])
def test_real_verifier_rejects_changed_payloads_and_copied_or_missing_signatures(
    repository: GitRepository, real_gpgv: Path, mutation: str
) -> None:
    raw = _raw()
    if mutation == "message":
        raw = raw.replace(b"2.8.0", b"9.9.9", 1)
    elif mutation == "author":
        raw = raw.replace(b"dependabot[bot]", b"human-maintainer", 1)
    elif mutation == "tree":
        raw = raw.replace(b"tree f2", b"tree a2", 1)
    elif mutation == "copied-signature":
        other = (FIXTURES / "dependabot-setup-uv.commit").read_bytes()
        prefix = raw.partition(b"\ngpgsig ")[0]
        signed_header = other.partition(b"\ngpgsig ")[2].partition(b"\n\n")[0]
        raw = prefix + b"\ngpgsig " + signed_header + b"\n\n" + raw.partition(b"\n\n")[2]
    else:
        parts = signature._split_signature(raw)
        assert parts is not None
        raw = parts[0]
    sha = _store(repository, raw)
    assert signature.verified_github_commits(repository, (sha,)) == frozenset()


def test_real_valid_signature_from_an_unapproved_key_is_not_exempted(
    repository: GitRepository, real_gpgv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = _store(repository, _raw())
    monkeypatch.setattr(signature, "PINNED_FINGERPRINT", "A" * 40)
    assert signature.verified_github_commits(repository, (sha,)) == frozenset()


def test_repository_signature_configuration_cannot_change_the_verifier(
    repository: GitRepository, real_gpgv: Path
) -> None:
    sha = _store(repository, _raw())
    for name, value in (
        ("gpg.program", str(repository.directory / "attacker-gpg")),
        ("gpg.format", "ssh"),
        ("gpg.ssh.program", str(repository.directory / "attacker-ssh")),
        ("gpg.ssh.allowedSignersFile", str(repository.directory / "attacker-signers")),
    ):
        subprocess.run(
            [repository.executable, "-C", str(repository.directory), "config", name, value],
            check=True,
        )
    assert signature.verified_github_commits(repository, (sha,)) == {sha}


def test_signed_payload_reconstruction_preserves_exact_message_bytes() -> None:
    raw = _raw() + b"\r\nextra\tcontent\n\n"
    parts = signature._split_signature(raw)
    assert parts is not None
    payload, detached = parts
    assert payload.endswith(b"\r\nextra\tcontent\n\n")
    assert detached.startswith(b"-----BEGIN PGP SIGNATURE-----\n")
    assert detached.endswith(b"-----END PGP SIGNATURE-----\n\n")
    assert payload.partition(b"\n\n")[2] == raw.partition(b"\n\n")[2]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"no boundary",
        b"\0\n\nmessage",
        b" continuation\n\nmessage",
        b"tree\n\nmessage",
        b"tree a\r\n\nmessage",
        b"gpgsig -----BEGIN SSH SIGNATURE-----\n -----END SSH SIGNATURE-----\n\nmessage",
        b"gpgsig -----BEGIN PGP SIGNATURE-----\n -----END PGP SIGNATURE-----\n"
        b"gpgsig -----BEGIN PGP SIGNATURE-----\n -----END PGP SIGNATURE-----\n\nmessage",
        b"gpgsig-sha256 -----BEGIN PGP SIGNATURE-----\n -----END PGP SIGNATURE-----\n\nmessage",
        b"gpgsig -----BEGIN PGP SIGNATURE-----\n bad armor\n\nmessage",
    ],
)
def test_missing_ambiguous_or_unsupported_signature_headers_are_rejected(raw: bytes) -> None:
    assert signature._split_signature(raw) is None


def test_raw_commit_and_signature_byte_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw()
    monkeypatch.setattr(signature, "MAX_COMMIT_BYTES", len(raw) - 1)
    assert signature._split_signature(raw) is None
    monkeypatch.setattr(signature, "MAX_COMMIT_BYTES", len(raw))
    assert signature._split_signature(raw) is not None
    monkeypatch.setattr(signature, "MAX_SIGNATURE_BYTES", 16)
    assert signature._split_signature(raw) is None


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"Good signature from GitHub\n",
        VALID_STATUS.rstrip(b"\n"),
        VALID_STATUS.replace(FINGERPRINT.encode(), b"A" * 40),
        VALID_STATUS.replace(b"VALIDSIG ", b"VALIDSIG B5690EEEBB952194 "),
        VALID_STATUS + VALID_STATUS,
        VALID_STATUS.replace(b"GOODSIG", b"BADSIG"),
        VALID_STATUS.replace(b"GOODSIG", b"EXPSIG"),
        VALID_STATUS.replace(b"GOODSIG", b"EXPKEYSIG"),
        VALID_STATUS.replace(b"GOODSIG", b"REVKEYSIG"),
        VALID_STATUS.replace(b"GOODSIG", b"ERRSIG"),
        VALID_STATUS + b"[GNUPG:] NO_PUBKEY B5690EEEBB952194\n",
        VALID_STATUS + b"[GNUPG:] FAILURE verify 1\n",
        VALID_STATUS + b"[GNUPG:] FUTURE_SECURITY_REQUIREMENT\n",
        VALID_STATUS.replace(b" 1 8 00 ", b" 1 2 00 "),
        VALID_STATUS.replace(b" 1 8 00 ", b" 1 8 01 "),
        VALID_STATUS.replace(b" 0 4 0 1 ", b" 1792000000 4 0 1 "),
        VALID_STATUS.replace(b"NEWSIG", b"SIG_ID"),
        VALID_STATUS.replace(b"2026-10-11", b"invalid"),
        VALID_STATUS.replace(b"1791682550", b"invalid"),
    ],
)
def test_status_requires_one_complete_success_from_the_exact_strong_key(output: bytes) -> None:
    assert not signature._valid_status(output)


def test_strict_success_status_is_accepted() -> None:
    assert signature._valid_status(VALID_STATUS)


@pytest.mark.parametrize(
    "result",
    [
        replace(GOOD_RESULT, returncode=1),
        replace(GOOD_RESULT, timed_out=True),
        replace(GOOD_RESULT, stdout_overflow=True),
        replace(GOOD_RESULT, stderr_overflow=True),
        replace(GOOD_RESULT, stdout=b"untrusted output"),
    ],
)
def test_verifier_failures_deny_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: ProcessResult
) -> None:
    monkeypatch.setattr(signature, "run_bounded_process", lambda *_args, **_kwargs: result)
    assert not signature._verify("gpgv", tmp_path, b"payload", b"signature", timeout=1)


def test_verifier_launch_failure_denies_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> ProcessResult:
        raise OSError("unavailable verifier")

    monkeypatch.setattr(signature, "run_bounded_process", unavailable)
    assert not signature._verify("gpgv", tmp_path, b"payload", b"signature", timeout=1)


def test_verifier_uses_isolated_environment_private_storage_and_shared_process_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GNUPGHOME", "attacker-home")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-forwarded")
    monkeypatch.setenv("LD_PRELOAD", "attacker-library")
    captured: dict[str, object] = {}

    def run(command: object, **kwargs: object) -> ProcessResult:
        captured.update(kwargs)
        captured["command"] = command
        return GOOD_RESULT

    monkeypatch.setattr(signature, "run_bounded_process", run)
    assert signature._verify("absolute-gpgv", tmp_path, b"payload\r\n", b"signature", timeout=1)
    assert captured["stdin_data"] == b"payload\r\n"
    assert captured["cwd"] == tmp_path
    assert captured["timeout_seconds"] == 1
    environment = cast(dict[str, str], captured["environment"])
    assert set(environment) <= {"SYSTEMROOT", "WINDIR", "LC_ALL"}
    command = cast(list[str], captured["command"])
    assert command[0] == "absolute-gpgv"
    assert command[command.index("--homedir") + 1] == "."
    assert command[command.index("--keyring") + 1] == "github-keyring.gpg"
    assert command[-1] == "-"
    assert (tmp_path / "signature.asc").read_bytes() == b"signature"


def test_missing_verifier_does_not_read_commit_objects(
    repository: GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*_args: object) -> str:
        raise GitError("gpgv unavailable")

    def unexpected(*_args: object, **_kwargs: object) -> ProcessResult:
        pytest.fail("unavailable verification should avoid Git reads")

    monkeypatch.setattr(signature, "find_external_executable", missing)
    monkeypatch.setattr(signature, "run_git", unexpected)
    assert signature.verified_github_commits(repository, ("a" * 40,)) == frozenset()


def test_object_substitution_cannot_use_a_different_commit_signature(
    repository: GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_tool(monkeypatch)
    monkeypatch.setattr(
        signature, "run_git", lambda *_args, **_kwargs: replace(GOOD_RESULT, stdout=_raw())
    )
    monkeypatch.setattr(
        signature, "_verify", lambda *_args, **_kwargs: pytest.fail("wrong object ID")
    )
    assert signature.verified_github_commits(repository, ("a" * 40,)) == frozenset()


@pytest.mark.parametrize("budget", ["count", "bytes", "time"])
def test_shared_budgets_stop_verification_without_exempting_remaining_commits(
    repository: GitRepository, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    first = _store(repository, _raw())
    second = _store(repository, (FIXTURES / "dependabot-setup-uv.commit").read_bytes())
    _allow_tool(monkeypatch)
    calls: list[str] = []

    def verify(*_args: object, **_kwargs: object) -> bool:
        calls.append("verification")
        return True

    monkeypatch.setattr(signature, "_verify", verify)
    if budget == "count":
        monkeypatch.setattr(signature, "MAX_CANDIDATES", 1)
    elif budget == "bytes":
        monkeypatch.setattr(signature, "MAX_TOTAL_BYTES", len(_raw()))
    else:
        timeouts = iter((1.0, 1.0, 0.0))
        monkeypatch.setattr(signature, "_remaining_timeout", lambda _: next(timeouts))
    assert signature.verified_github_commits(repository, (first, second)) == {first}
    assert calls == ["verification"]


def test_git_read_failure_and_invalid_ids_receive_no_exemption(
    repository: GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_tool(monkeypatch)

    def failed(*_args: object, **_kwargs: object) -> ProcessResult:
        raise GitError("bounded Git failure")

    monkeypatch.setattr(signature, "run_git", failed)
    assert (
        signature.verified_github_commits(repository, ("a" * 40, "--all", "\N{SNOWMAN}" * 40))
        == frozenset()
    )
    assert signature.verified_github_commits(repository, "a" * 40) == frozenset()


def test_sha256_object_identity_is_bound_to_the_full_raw_commit() -> None:
    raw = _raw()
    sha = hashlib.sha256(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert signature._matches_object_id(raw, sha)
    assert not signature._matches_object_id(raw + b"changed", sha)


def test_verifier_inside_outer_repository_is_rejected_from_a_subdirectory(
    repository: GitRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = repository.directory / "nested"
    nested.mkdir()
    executable = repository.directory / "gpgv.exe"
    executable.write_bytes(b"attacker executable")
    monkeypatch.setattr(git_runtime.shutil, "which", lambda _: str(executable))
    with pytest.raises(GitError, match="inside the repository"):
        git_runtime.find_external_executable(GitRepository(nested, repository.executable), "gpgv")


@pytest.mark.parametrize("timeout", [0.0, -1.0, 31.0, float("inf"), float("nan")])
def test_git_caller_cannot_weaken_the_shared_timeout(
    repository: GitRepository, timeout: float
) -> None:
    with pytest.raises(ValueError, match="timeout"):
        git_runtime.run_git(repository, ["version"], stdout_limit=256, timeout_seconds=timeout)
