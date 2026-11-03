"""Safe, BOM-preserving shell completion generation and installation for YAGA."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import shellingham
import typer
from typer.completion import completion_init

from yaga.errors import InputError, YagaError, safe_error_text
from yaga.files import read_file_prefix

MAX_PROFILE_BYTES = 1024 * 1024
MAX_SUBPROCESS_OUTPUT_BYTES = 16 * 1024
PROFILE_QUERY_TIMEOUT_SECONDS = 10.0

SUPPORTED_SHELLS = ("bash", "zsh", "fish", "powershell", "pwsh")
MARKER_START = "# >>> yaga completion >>>"
MARKER_END = "# <<< yaga completion <<<"

_INVALID_IDENT_RE = re.compile(r"[^a-zA-Z0-9_]")

_BASH_TEMPLATE = """\
%(complete_func)s() {
    local IFS=$'\\n'
    COMPREPLY=( $( env COMP_WORDS="${COMP_WORDS[*]}" \\
                   COMP_CWORD=$COMP_CWORD \\
                   %(autocomplete_var)s=complete_bash $1 ) )
    return 0
}

complete -o default -F %(complete_func)s %(prog_name)s"""

_ZSH_TEMPLATE = """\
#compdef %(prog_name)s

%(complete_func)s() {
  eval $(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" %(autocomplete_var)s=complete_zsh %(prog_name)s)
}

compdef %(complete_func)s %(prog_name)s"""

_FISH_TEMPLATE = (
    "complete --command %(prog_name)s --no-files --arguments "
    '"(env %(autocomplete_var)s=complete_fish '
    "_TYPER_COMPLETE_FISH_ACTION=get-args "
    '_TYPER_COMPLETE_ARGS=(commandline -cp) %(prog_name)s)" '
    '--condition "env %(autocomplete_var)s=complete_fish '
    "_TYPER_COMPLETE_FISH_ACTION=is-args "
    '_TYPER_COMPLETE_ARGS=(commandline -cp) %(prog_name)s"'
)

_POWERSHELL_TEMPLATE = """\
Register-ArgumentCompleter -Native -CommandName %(prog_name)s -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $Env:%(autocomplete_var)s = "complete_powershell"
    $Env:_TYPER_COMPLETE_ARGS = $commandAst.ToString()
    $Env:_TYPER_COMPLETE_WORD_TO_COMPLETE = $wordToComplete
    try {
        %(prog_name)s | ForEach-Object {
            $commandArray = $_ -Split ":::"
            $command = $commandArray[0]
            $helpString = if ($commandArray.Length -gt 1 -and $commandArray[1]) { $commandArray[1] } else { " " }
            [System.Management.Automation.CompletionResult]::new(
                $command, $command, 'ParameterValue', $helpString)
        }
    } finally {
        Remove-Item Env:\\%(autocomplete_var)s, Env:\\_TYPER_COMPLETE_ARGS, Env:\\_TYPER_COMPLETE_WORD_TO_COMPLETE -ErrorAction SilentlyContinue
    }
}"""

_TEMPLATES = {
    "bash": _BASH_TEMPLATE,
    "zsh": _ZSH_TEMPLATE,
    "fish": _FISH_TEMPLATE,
    "powershell": _POWERSHELL_TEMPLATE,
    "pwsh": _POWERSHELL_TEMPLATE,
}


def ensure_completion_registered() -> None:
    """Register Typer's shell completion classes for _YAGA_COMPLETE handling."""
    completion_init()


def detect_shell_name() -> str:
    """Detect the active interactive shell without invoking child shells."""
    if os.environ.get("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"):
        raise InputError(
            "could not detect the current shell; supported shells are "
            + ", ".join(SUPPORTED_SHELLS)
        )
    detected: str | None = None
    try:
        raw_name, _command = shellingham.detect_shell()
        detected = Path(raw_name).stem.lower()
    except (shellingham.ShellDetectionFailure, OSError, RuntimeError, ValueError):
        detected = None

    if detected not in SUPPORTED_SHELLS:
        env_shell = os.environ.get("SHELL", "").strip()
        if env_shell:
            candidate = Path(env_shell).stem.lower()
            if candidate in SUPPORTED_SHELLS:
                detected = candidate

    if detected not in SUPPORTED_SHELLS:
        raise InputError(
            "could not detect the current shell; supported shells are "
            + ", ".join(SUPPORTED_SHELLS)
        )
    return detected


def normalize_shell_name(shell: str | None) -> str:
    """Normalize an explicit shell name or detect the active shell."""
    if shell is None:
        return detect_shell_name()
    normalized = Path(shell.strip()).stem.lower()
    if normalized not in SUPPORTED_SHELLS:
        raise InputError(
            f"unsupported shell {safe_error_text(shell)!r}; supported shells are "
            + ", ".join(SUPPORTED_SHELLS)
        )
    return normalized


def completion_script(shell: str | None = None, *, prog_name: str = "yaga") -> str:
    """Render the shell completion script for one supported shell."""
    resolved_shell = normalize_shell_name(shell)
    ident = _INVALID_IDENT_RE.sub("", prog_name.replace("-", "_"))
    if not ident:
        raise InputError("program name cannot be normalized into a shell identifier")
    complete_var = f"_{ident.upper()}_COMPLETE"
    template = _TEMPLATES[resolved_shell]
    return (
        template
        % {
            "complete_func": f"_{ident}_completion",
            "prog_name": prog_name,
            "autocomplete_var": complete_var,
        }
    ).strip()


def install_completion(
    shell: str | None = None,
    *,
    prog_name: str = "yaga",
    home: Path | None = None,
) -> tuple[str, Path]:
    """Install shell completion safely without mutating OS/PowerShell security policy."""
    resolved_shell = normalize_shell_name(shell)
    user_home = (home or Path.home()).expanduser().resolve()
    script = completion_script(resolved_shell, prog_name=prog_name)

    if resolved_shell == "bash":
        completion_path = user_home / ".bash_completions" / f"{prog_name}.sh"
        _write_utf8_file(completion_path, f"{script}\n")
        rc_path = user_home / ".bashrc"
        _upsert_marked_block(
            rc_path,
            'source "$HOME/.bash_completions/' + f'{prog_name}.sh"',
            default_newline="\n",
            default_bom=b"",
        )
        return resolved_shell, completion_path

    if resolved_shell == "zsh":
        completion_path = user_home / ".zfunc" / f"_{prog_name}"
        _write_utf8_file(completion_path, f"{script}\n")
        rc_path = user_home / ".zshrc"
        _upsert_marked_block(
            rc_path,
            "fpath+=~/.zfunc; autoload -Uz compinit; compinit",
            default_newline="\n",
            default_bom=b"",
        )
        return resolved_shell, completion_path

    if resolved_shell == "fish":
        completion_path = user_home / ".config" / "fish" / "completions" / f"{prog_name}.fish"
        _write_utf8_file(completion_path, f"{script}\n")
        return resolved_shell, completion_path

    profile_path = _resolve_powershell_profile(resolved_shell)
    default_newline = "\r\n" if os.name == "nt" else "\n"
    # Windows PowerShell 5.1 reads BOM-less profiles using the legacy ANSI code page;
    # UTF-8 with BOM works identically in Windows PowerShell 5.1 and PowerShell 7+,
    # while existing UTF-16 LE / UTF-16 BE / UTF-8 profiles preserve their exact BOM.
    default_bom = b"\xef\xbb\xbf" if resolved_shell == "powershell" else b""
    _upsert_marked_block(
        profile_path,
        script,
        default_newline=default_newline,
        default_bom=default_bom,
    )
    return resolved_shell, profile_path


def install_completion_callback(value: bool) -> None:
    """Eager Typer callback for --install-completion."""
    if not value:
        return
    try:
        shell, installed_path = install_completion()
    except YagaError as error:
        typer.echo(f"YAGA {error.kind} error: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=2) from None
    safe_path = safe_error_text(installed_path, maximum=500)
    typer.echo(f"{shell} completion installed in {safe_path}")
    typer.echo("Completion will take effect once you restart the terminal.")
    raise typer.Exit(code=0)


def show_completion_callback(value: bool) -> None:
    """Eager Typer callback for --show-completion."""
    if not value:
        return
    try:
        script = completion_script()
    except YagaError as error:
        typer.echo(f"YAGA {error.kind} error: {safe_error_text(error)}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(script)
    raise typer.Exit(code=0)


def _resolve_powershell_profile(shell: str) -> Path:
    executable = shutil.which(shell)
    if executable is None:
        raise InputError(f"shell executable {shell!r} was not found on PATH")
    command = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "Write-Output $PROFILE.CurrentUserCurrentHost",
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROFILE_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise InputError(f"timed out querying {shell} profile path") from error
    except OSError as error:
        raise InputError(
            f"failed to query {shell} profile path: {safe_error_text(error)}"
        ) from error

    if len(completed.stdout) > MAX_SUBPROCESS_OUTPUT_BYTES:
        raise InputError(
            f"{shell} profile path output exceeded {MAX_SUBPROCESS_OUTPUT_BYTES} bytes"
        )
    if completed.returncode != 0:
        raise InputError(
            f"could not determine {shell} user profile path (exit {completed.returncode})"
        )

    try:
        decoded = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise InputError(f"{shell} profile path is not valid UTF-8") from error

    lines = [line.strip() for line in decoded.splitlines() if line.strip()]
    if len(lines) != 1:
        raise InputError(f"{shell} did not return a single profile path")
    raw_path = lines[0]
    if any(ord(ch) < 32 for ch in raw_path):
        raise InputError(f"{shell} returned an unsafe profile path")
    return Path(raw_path).expanduser()


def _detect_profile_encoding(raw: bytes, *, display_path: str) -> tuple[bytes, str]:
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise InputError(f"shell profile {display_path} uses unsupported UTF-32 encoding")
    if raw.startswith(b"\xff\xfe"):
        return b"\xff\xfe", "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return b"\xfe\xff", "utf-16-be"
    if raw.startswith(b"\xef\xbb\xbf"):
        return b"\xef\xbb\xbf", "utf-8"
    if b"\x00" in raw:
        raise InputError(
            f"shell profile {display_path} contains NUL bytes without a UTF-16 BOM; "
            "refusing to modify it"
        )
    return b"", "utf-8"


def _upsert_marked_block(
    path: Path,
    block_body: str,
    *,
    default_newline: str,
    default_bom: bytes,
) -> None:
    display_path = safe_error_text(path, maximum=300)
    if path.exists():
        if path.is_dir():
            raise InputError(f"shell profile path is a directory: {display_path}")
        try:
            raw = read_file_prefix(path, maximum=MAX_PROFILE_BYTES)
        except OSError as error:
            raise InputError(f"cannot read shell profile {display_path}") from error
        if len(raw) > MAX_PROFILE_BYTES:
            raise InputError(f"shell profile exceeds {MAX_PROFILE_BYTES} bytes: {display_path}")
        bom, codec = _detect_profile_encoding(raw, display_path=display_path)
        payload = raw[len(bom) :]
        try:
            existing_text = payload.decode(codec)
        except UnicodeDecodeError as error:
            raise InputError(
                f"shell profile {display_path} could not be decoded as {codec}; "
                "refusing to modify it"
            ) from error
        newline = (
            "\r\n"
            if "\r\n" in existing_text
            else ("\n" if "\n" in existing_text else default_newline)
        )
    else:
        existing_text = ""
        bom = default_bom
        codec = "utf-8"
        newline = default_newline

    normalized_body = "\n".join(block_body.strip().splitlines()).replace("\n", newline)
    marked_block = f"{MARKER_START}{newline}{normalized_body}{newline}{MARKER_END}"

    updated_text = _replace_or_append_marked_block(
        existing_text,
        marked_block,
        newline=newline,
        display_path=display_path,
    )
    encoded = bom + updated_text.encode(codec)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
    except OSError as error:
        raise InputError(f"cannot write shell profile {display_path}") from error


def _replace_or_append_marked_block(
    existing_text: str,
    marked_block: str,
    *,
    newline: str,
    display_path: str,
) -> str:
    start_count = existing_text.count(MARKER_START)
    end_count = existing_text.count(MARKER_END)
    if start_count != end_count or start_count > 1:
        raise InputError(
            f"shell profile {display_path} has unmatched or duplicate YAGA completion markers"
        )
    if start_count == 1:
        start_index = existing_text.index(MARKER_START)
        end_index = existing_text.index(MARKER_END)
        if end_index < start_index:
            raise InputError(f"shell profile {display_path} has reversed YAGA completion markers")
        end_after = end_index + len(MARKER_END)
        replaced = existing_text[:start_index] + marked_block + existing_text[end_after:]
        if not replaced.endswith(newline):
            replaced += newline
        return replaced

    stripped = existing_text.rstrip("\r\n")
    if not stripped:
        return f"{marked_block}{newline}"
    return f"{stripped}{newline}{newline}{marked_block}{newline}"


def _write_utf8_file(path: Path, content: str) -> None:
    display_path = safe_error_text(path, maximum=300)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    except OSError as error:
        raise InputError(f"cannot write completion script {display_path}") from error
