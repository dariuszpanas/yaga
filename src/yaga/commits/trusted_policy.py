"""Read one literal policy blob from an independently verified trusted commit."""

import re
from pathlib import Path, PurePosixPath

from yaga.commits.config import MAX_CONFIG_BYTES, load_config_bytes
from yaga.commits.models import LoadedConfig
from yaga.errors import InputError
from yaga.git import open_repository, run_git


def load_trusted_policy(repository: Path, revision: str, path: str) -> LoadedConfig:
    """Never read PR-controlled working-tree policy, symlink targets, or global defaults."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
        raise InputError("trusted policy revision must be a full commit SHA")
    parts = PurePosixPath(path).parts
    if (
        not path
        or len(path) > 1024
        or len(parts) > 64
        or PurePosixPath(path).as_posix() != path
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts)
        or parts[-1] not in {".yaga.toml", "pyproject.toml"}
    ):
        raise InputError(
            "trusted policy must be a canonical relative .yaga.toml or pyproject.toml path"
        )
    repo = open_repository(repository)
    entry = run_git(repo, ["ls-tree", "-z", revision, "--", path], stdout_limit=4096)
    match = re.fullmatch(
        rb"100(?:644|755) blob ([0-9a-f]{40}|[0-9a-f]{64})\t([^\0]+)\0", entry.stdout
    )
    if entry.returncode or match is None or match[2] != path.encode("ascii"):
        raise InputError("trusted policy must be one committed regular file")
    blob = run_git(
        repo, ["cat-file", "blob", match[1].decode("ascii")], stdout_limit=MAX_CONFIG_BYTES
    )
    if blob.returncode:
        raise InputError("trusted policy blob could not be read")
    return load_config_bytes(blob.stdout, path=repository / path)
