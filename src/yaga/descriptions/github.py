"""Read one explicit current PR description through a bounded installed gh CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yaga.descriptions.models import MAX_DESCRIPTION_BYTES
from yaga.descriptions.sources import MAX_PULL_REQUEST_NUMBER
from yaga.errors import InputError
from yaga.git.runtime import run_bounded_process

MAX_REFERENCE_CHARACTERS = 1024
MAX_GH_STDOUT_BYTES = 512 * 1024
MAX_GH_STDERR_BYTES = 64 * 1024
GH_TIMEOUT_SECONDS = 30
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_URL_PATH = re.compile(r"/([^/]+)/([^/]+)/pull/([1-9][0-9]{0,18})/?")


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    """A fully selected host, repository and PR number, with no local inference."""

    host: str
    owner: str
    repository: str
    number: int

    def __post_init__(self) -> None:
        if not _valid_host(self.host):
            raise ValueError("pull request host must be a bounded ASCII DNS hostname")
        if not isinstance(self.owner, str) or _OWNER.fullmatch(self.owner) is None:
            raise ValueError("pull request owner must be a bounded ASCII repository owner")
        if (
            not isinstance(self.repository, str)
            or _REPOSITORY.fullmatch(self.repository) is None
            or self.repository in {".", ".."}
        ):
            raise ValueError("pull request repository must be a bounded ASCII repository name")
        if type(self.number) is not int or not 1 <= self.number <= MAX_PULL_REQUEST_NUMBER:
            raise ValueError("pull request number must be a bounded positive integer")
        object.__setattr__(self, "host", self.host.lower())

    @property
    def url(self) -> str:
        """Return the selected canonical HTTPS PR URL."""
        return f"https://{self.host}/{self.owner}/{self.repository}/pull/{self.number}"

    @property
    def gh_repository(self) -> str:
        """Return an explicit gh repository selector, including its host."""
        return f"{self.host}/{self.owner}/{self.repository}"


@dataclass(frozen=True, slots=True)
class FetchedPullRequestDescription:
    """The current returned body and verified remote identity."""

    body: str
    number: int
    url: str


def parse_pull_request_reference(value: str, repository: str | None = None) -> PullRequestReference:
    """Accept an explicit positive number plus repository, or one HTTPS PR URL."""
    _require_selector(value, label="--pr")
    if value.isdecimal():
        if _NUMBER.fullmatch(value) is None:
            raise InputError("--pr number must be positive ASCII digits without leading zeroes")
        number = _parse_number(value)
        if repository is None:
            raise InputError("a numeric --pr requires --repo [HOST/]OWNER/REPO")
        _require_selector(repository, label="--repo")
        parts = repository.split("/")
        if len(parts) == 2:
            parts.insert(0, "github.com")
        if len(parts) != 3:
            raise InputError("--repo must use [HOST/]OWNER/REPO")
        return _reference(*parts, number=number)
    if repository is not None:
        raise InputError("--repo is supported only with a numeric --pr, not a PR URL")
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.netloc.casefold() != (url.hostname or "").casefold()
            or url.query
            or url.fragment
            or "?" in value
            or "#" in value
            or "%" in value
        ):
            raise ValueError("unsupported URL structure")
        path = _URL_PATH.fullmatch(url.path)
        if path is None or url.hostname is None:
            raise ValueError("unsupported PR URL path")
        return _reference(url.hostname, path[1], path[2], number=_parse_number(path[3]))
    except ValueError:
        raise InputError(
            "--pr must be a positive ASCII number with --repo, or an HTTPS "
            "HOST/OWNER/REPO/pull/NUMBER URL without credentials, ports, query or fragment"
        ) from None


def fetch_pull_request_description(
    reference: PullRequestReference,
) -> FetchedPullRequestDescription:
    """Fetch one current snapshot using gh authentication, without Git or writes."""
    if not isinstance(reference, PullRequestReference):
        raise TypeError("remote description fetch requires a PullRequestReference")
    executable = _find_gh()
    try:
        result = run_bounded_process(
            [
                executable,
                "pr",
                "view",
                str(reference.number),
                "--repo",
                reference.gh_repository,
                "--json",
                "body,number,url",
            ],
            stdout_limit=MAX_GH_STDOUT_BYTES,
            stderr_limit=MAX_GH_STDERR_BYTES,
            timeout_seconds=GH_TIMEOUT_SECONDS,
            environment=_gh_environment(),
        )
    except (OSError, ValueError):
        raise InputError(
            "gh could not execute its bounded pull-request description request"
        ) from None
    if result.timed_out:
        raise InputError("gh exceeded the 30-second pull-request description timeout")
    if result.stdout_overflow or result.stderr_overflow:
        raise InputError("gh exceeded the bounded pull-request response output limit")
    if result.returncode != 0:
        raise InputError(
            "gh could not fetch the pull request; check gh authentication, repository access, "
            "PR existence, and network connectivity"
        )
    return _parse_response(result.stdout, reference)


def _require_selector(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_REFERENCE_CHARACTERS
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise InputError(f"{label} must be bounded printable ASCII without whitespace")


def _valid_host(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 253
        and all(_HOST_LABEL.fullmatch(label) is not None for label in value.split("."))
    )


def _parse_number(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_PULL_REQUEST_NUMBER:
        raise InputError("pull request number exceeds the supported positive integer range")
    return number


def _reference(host: str, owner: str, repository: str, *, number: int) -> PullRequestReference:
    try:
        return PullRequestReference(host, owner, repository, number)
    except ValueError:
        raise InputError(
            "pull request host, owner, or repository has an unsupported format"
        ) from None


def _find_gh() -> str:
    try:
        executable = shutil.which("gh")
    except (OSError, ValueError):
        raise InputError("gh executable could not be located safely") from None
    if executable is None:
        raise InputError("checking --pr requires the GitHub CLI (gh); install gh and authenticate")
    try:
        original = Path(executable)
        if not original.is_absolute():
            raise ValueError("relative executable")
        resolved = original.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("nonregular executable")
        current_directory = Path.cwd().resolve(strict=True)
        if (
            original.parent.resolve(strict=True) == current_directory
            or resolved.parent == current_directory
        ):
            raise ValueError("current-directory executable")
        if os.name == "nt" and any(
            path.suffix.casefold() != ".exe" for path in (original, resolved)
        ):
            raise ValueError("Windows executable must use .exe")
        return str(resolved)
    except (OSError, RuntimeError, ValueError):
        raise InputError(
            "gh must resolve to an absolute regular executable (gh.exe on Windows) "
            "installed on PATH outside the current directory"
        ) from None


def _gh_environment() -> dict[str, str]:
    # gh owns credentials and host-specific authentication. Never copy credentials
    # into arguments or output; remove ambient overrides and verbose HTTP logging.
    excluded = {"GH_DEBUG", "DEBUG", "GH_FORCE_TTY", "GH_HOST", "GH_REPO"}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in excluded}
    environment.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "",
            "PAGER": "",
            "GH_TELEMETRY": "false",
            "NO_COLOR": "1",
            "CLICOLOR": "0",
            "CLICOLOR_FORCE": "0",
        }
    )
    return environment


def _parse_response(output: bytes, expected: PullRequestReference) -> FetchedPullRequestDescription:
    if len(output) > MAX_GH_STDOUT_BYTES:
        raise InputError("gh exceeded the bounded pull-request JSON limit")
    try:
        document = json.loads(
            output.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise InputError("gh returned invalid or ambiguous UTF-8 JSON") from None
    if not isinstance(document, dict) or set(document) != {"body", "number", "url"}:
        raise InputError("gh returned an unexpected pull-request description document")
    if type(document["number"]) is not int or document["number"] != expected.number:
        raise InputError("gh returned a different or invalid pull-request number")
    try:
        actual = parse_pull_request_reference(document["url"])
    except InputError:
        raise InputError("gh returned an invalid pull-request URL") from None
    if (
        actual.number != expected.number
        or actual.gh_repository.casefold() != expected.gh_repository.casefold()
    ):
        raise InputError("gh returned a different pull-request identity")
    body = document["body"]
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise InputError("gh returned a non-text pull-request body")
    try:
        body_bytes = body.encode("utf-8")
    except UnicodeEncodeError:
        raise InputError("gh returned a pull-request body that is not valid UTF-8") from None
    if len(body_bytes) > MAX_DESCRIPTION_BYTES:
        raise InputError("pull-request body exceeds the hard 65536-byte limit")
    return FetchedPullRequestDescription(body, actual.number, actual.url)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON value")
