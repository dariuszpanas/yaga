"""Offline, bounded verification against YAGA's pinned GitHub signing key.

This verifies commit integrity, not exclusive Dependabot authorship or GitHub's
live verification badge. Callers must independently require eligible PR-author
policy and compatible commit metadata. Every uncertainty denies the exemption.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from yaga.errors import GitError, InputError
from yaga.git import GitRepository, parse_object_id, run_git
from yaga.git.runtime import find_external_executable, run_bounded_process

PINNED_FINGERPRINT = "968479A1AFF927E37D1A566BB5690EEEBB952194"
MAX_CANDIDATES = 256
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_COMMIT_BYTES = 256 * 1024
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_STATUS_BYTES = 16 * 1024
MAX_TOTAL_SECONDS = 30.0
MAX_PROCESS_SECONDS = 5.0

# Public key material from https://github.com/web-flow.gpg, retrieved 2026-10-17.
# The endpoint also carries a historical key; only PINNED_FINGERPRINT is accepted.
# Key rotation requires an audited package update, never a runtime network lookup.
_KEYRING_BASE64 = (
    "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"
)


def verified_github_commits(repository: GitRepository, shas: Sequence[str]) -> frozenset[str]:
    """Return exact commit IDs verified with the pinned key, within shared budgets."""
    if not isinstance(repository, GitRepository) or isinstance(shas, (str, bytes)):
        return frozenset()
    deadline = time.monotonic() + MAX_TOTAL_SECONDS
    verified: set[str] = set()
    try:
        executable = find_external_executable(repository, "gpgv")
        with tempfile.TemporaryDirectory(prefix="yaga-github-signature-") as directory:
            temporary = Path(directory).resolve(strict=True)
            (temporary / "github-keyring.gpg").write_bytes(base64.b64decode(_KEYRING_BASE64))
            remaining_bytes = MAX_TOTAL_BYTES
            for index, sha in enumerate(shas):
                if index >= MAX_CANDIDATES or remaining_bytes <= 0:
                    break
                timeout = _remaining_timeout(deadline)
                if timeout <= 0:
                    break
                if not _valid_sha(sha):
                    continue
                maximum = min(MAX_COMMIT_BYTES, remaining_bytes)
                try:
                    result = run_git(
                        repository,
                        ["cat-file", "commit", sha],
                        stdout_limit=maximum,
                        timeout_seconds=timeout,
                    )
                except (GitError, OSError, ValueError):
                    # An overflow can consume the entire allowance without returning bytes.
                    remaining_bytes -= maximum
                    continue
                remaining_bytes -= len(result.stdout)
                if result.returncode != 0 or not _matches_object_id(result.stdout, sha):
                    continue
                parts = _split_signature(result.stdout)
                if parts is None:
                    continue
                payload, signature = parts
                timeout = _remaining_timeout(deadline)
                if timeout <= 0:
                    break
                if _verify(executable, temporary, payload, signature, timeout=timeout):
                    verified.add(sha)
    except (GitError, InputError, OSError, RuntimeError, TypeError, ValueError):
        # Missing tools, unusable temporary storage, and malformed input never skip checks.
        return frozenset(verified)
    return frozenset(verified)


def _remaining_timeout(deadline: float) -> float:
    return min(MAX_PROCESS_SECONDS, deadline - time.monotonic())


def _valid_sha(value: object) -> bool:
    if not isinstance(value, str) or len(value) not in {40, 64}:
        return False
    try:
        return parse_object_id(value.encode("ascii"), label="signature commit") == value
    except (GitError, UnicodeError):
        return False


def _matches_object_id(raw: bytes, sha: str) -> bool:
    if not raw or len(raw) > MAX_COMMIT_BYTES:
        return False
    digest = hashlib.sha1(usedforsecurity=False) if len(sha) == 40 else hashlib.sha256()
    digest.update(b"commit " + str(len(raw)).encode("ascii") + b"\0")
    digest.update(raw)
    return digest.hexdigest() == sha


def _split_signature(raw: bytes) -> tuple[bytes, bytes] | None:
    """Strip exactly one OpenPGP header while preserving all other payload bytes."""
    if not raw or len(raw) > MAX_COMMIT_BYTES or b"\0" in raw:
        return None
    headers, separator, body = raw.partition(b"\n\n")
    if not separator or b"\r" in headers:
        return None
    retained: list[bytes] = []
    signature: list[bytes] | None = None
    in_signature = False
    for line in headers.split(b"\n"):
        if line.startswith(b" "):
            if in_signature:
                assert signature is not None
                signature.append(line[1:])
            elif retained:
                retained.append(line)
            else:
                return None
        else:
            in_signature = False
            name, space, value = line.partition(b" ")
            if not space or not name:
                return None
            if name.startswith(b"gpgsig"):
                if name != b"gpgsig" or signature is not None:
                    return None
                signature = [value]
                in_signature = True
            else:
                retained.append(line)
    if signature is None:
        return None
    detached = b"\n".join(signature) + b"\n"
    if (
        len(detached) > MAX_SIGNATURE_BYTES
        or not detached.startswith(b"-----BEGIN PGP SIGNATURE-----\n")
        or not detached.rstrip(b"\n").endswith(b"-----END PGP SIGNATURE-----")
        or detached.count(b"-----BEGIN ") != 1
        or detached.count(b"-----END ") != 1
    ):
        return None
    return b"\n".join(retained) + b"\n\n" + body, detached


def _verify(
    executable: str,
    temporary: Path,
    payload: bytes,
    signature: bytes,
    *,
    timeout: float,
) -> bool:
    (temporary / "signature.asc").write_bytes(signature)
    environment = {
        name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ
    }
    environment["LC_ALL"] = "C"
    try:
        result = run_bounded_process(
            [
                executable,
                "--homedir",
                # The private cwd also avoids MSYS treating a drive path as relative.
                ".",
                "--keyring",
                "github-keyring.gpg",
                "--status-fd",
                "1",
                "--weak-digest",
                "SHA1",
                "--weak-digest",
                "RIPEMD160",
                "--",
                str(temporary / "signature.asc"),
                "-",
            ],
            stdin_data=payload,
            cwd=temporary,
            environment=environment,
            stdout_limit=MAX_STATUS_BYTES,
            stderr_limit=MAX_STATUS_BYTES,
            timeout_seconds=timeout,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return (
        result.returncode == 0
        and not result.timed_out
        and not result.stdout_overflow
        and not result.stderr_overflow
        and _valid_status(result.stdout)
    )


def _valid_status(output: bytes) -> bool:
    """Require one successful signature and its complete pinned fingerprint."""
    if not output or len(output) > MAX_STATUS_BYTES or not output.endswith(b"\n"):
        return False
    good = valid = started = 0
    fingerprint = PINNED_FINGERPRINT.encode("ascii")
    for line in output.splitlines():
        if not line.startswith(b"[GNUPG:] "):
            return False
        name, _, arguments = line[9:].partition(b" ")
        if name == b"NEWSIG":
            started += 1
        elif name == b"GOODSIG":
            good += 1
            signer, separator, _ = arguments.partition(b" ")
            if not separator or signer not in {fingerprint, fingerprint[-16:]}:
                return False
        elif name == b"VALIDSIG":
            valid += 1
            fields = arguments.split(b" ")
            if (
                len(fields) != 10
                or fields[0] != fingerprint
                or fields[9] != fingerprint
                or re.fullmatch(rb"[0-9]{4}-[0-9]{2}-[0-9]{2}", fields[1]) is None
                or re.fullmatch(rb"[0-9]{1,12}", fields[2]) is None
                or fields[3] != b"0"
                or fields[4:7] != [b"4", b"0", b"1"]
                or fields[7] not in {b"8", b"9", b"10", b"11"}
                or fields[8] != b"00"
            ):
                return False
        elif name not in {b"KEY_CONSIDERED", b"SIG_ID"}:
            # Unknown/error/expired/revoked/unsupported status all deny the exemption.
            return False
    return good == valid == started == 1
