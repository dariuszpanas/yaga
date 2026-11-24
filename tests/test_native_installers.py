"""Exercise release resolution, verification and ownership with a private loopback server."""

from __future__ import annotations

import functools
import hashlib
import http.server
import os
import platform
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scripts.native_release import MANIFEST, ROOT, TARGETS, asset_name, manifest, project_version

VERSION = project_version()
WINDOWS = sys.platform == "win32"
TARGET = (
    ("aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64")
    + "-"
    + {"win32": "pc-windows-msvc", "darwin": "apple-darwin"}.get(sys.platform, "unknown-linux-musl")
)
EXE = "yaga.exe" if WINDOWS else "yaga"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def do_HEAD(self) -> None:  # noqa: N802 - HTTP method name required by the server
        if self.path == "/latest":
            self.send_response(302)
            assert isinstance(self.server, http.server.ThreadingHTTPServer)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/tag/v0.4.0")
            self.end_headers()
            return
        super().do_HEAD()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - inherited API
        pass


@contextmanager
def release_server(
    root: Path, handler_type: type[QuietHandler] = QuietHandler, tls: ssl.SSLContext | None = None
):
    handler = functools.partial(handler_type, directory=str(root))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        if tls:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def release_fixture(root: Path, binary: bytes) -> Path:
    release = root / "download" / f"v{VERSION}"
    latest = root / "latest/download"
    release.mkdir(parents=True)
    latest.mkdir(parents=True)
    asset = asset_name(TARGET)
    (release / asset).write_bytes(binary)
    header = f"yaga-native-v1 {VERSION}\n"
    for target in TARGETS:
        name = asset_name(target)
        digest = hashlib.sha256(binary if name == asset else name.encode()).hexdigest()
        header += f"{digest}  {name}\n"
    for directory in (release, latest):
        (directory / MANIFEST).write_bytes(header.encode("ascii"))
    return release


def command() -> list[str]:
    if WINDOWS:
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        assert pwsh
        return [pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "docs/install.ps1")]
    return ["sh", str(ROOT / "docs/install.sh")]


def install(tmp: Path, url: str, *arguments: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("YAGA_")}
    env.update(
        HOME=str(tmp),
        YAGA_RELEASE_BASE_URL=url,
        YAGA_NO_MODIFY_PATH="1",
        YAGA_INSTALL_DIR=str(tmp / "user's native bin"),
    )
    env.update(extra)
    return subprocess.run(
        [*command(), *arguments], cwd=tmp, env=env, text=True, capture_output=True, timeout=45
    )


def executable_fixture() -> bytes:
    if binary := os.environ.get("YAGA_TEST_NATIVE_BINARY") or os.environ.get(
        "YAGA_TEST_NATIVE_CLI"
    ):
        return Path(binary).read_bytes()
    if WINDOWS:
        pytest.skip("set YAGA_TEST_NATIVE_BINARY to qualify a built Windows release")
    return f"#!/bin/sh\nprintf '%s\\n' 'yaga {VERSION}'\n".encode()


def test_native_self_update_uses_the_installation_receipt(tmp_path: Path) -> None:
    if not (os.environ.get("YAGA_TEST_NATIVE_BINARY") or os.environ.get("YAGA_TEST_NATIVE_CLI")):
        pytest.skip("requires the native YAGA executable from native-ci")
    binary = executable_fixture()
    # ELF and PE allow an opaque trailer. Two functional images at the same version
    # let the test observe an actual replacement without breaking Apple code signing.
    original = binary if sys.platform == "darwin" else binary + b"private installation fixture"
    release = release_fixture(tmp_path / "server", original)
    dest = tmp_path / "user's native bin"
    exe = dest / EXE
    receipt = dest / ".yaga-native-receipt"
    with release_server(tmp_path / "server") as url:
        installed = install(tmp_path, url, "--native")
        assert installed.returncode == 0, installed.stderr
        env = os.environ | {
            "YAGA_RELEASE_BASE_URL": url,
            "YAGA_VERSION": "0.0.1",
            "YAGA_INSTALL_DIR": str(tmp_path / "wrong destination"),
            "YAGA_INSTALL_METHOD": "python",
        }

        def update(*args: str) -> tuple[int, str]:
            result = subprocess.run(
                [exe, "self", "update", *args],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                encoding="utf-8",
                timeout=60,
            )
            if WINDOWS and not args and result.returncode == 0:
                log = Path(result.stdout.split("Completion log: ")[1].splitlines()[0])
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    text = log.read_text(encoding="utf-8", errors="replace")
                    if "YAGA_UPDATE_EXIT_CODE=" in text:
                        return int(text.split("YAGA_UPDATE_EXIT_CODE=")[-1].strip()), text
                    time.sleep(0.05)
                pytest.fail(f"update worker timed out; see {log}")
            return result.returncode, result.stdout + result.stderr

        code, text = update("--dry-run")
        assert code == 0 and "latest native release" in text
        assert hashlib.sha256(original).hexdigest().encode() in receipt.read_bytes()
        previous_hash = hashlib.sha256(original).hexdigest().encode()
        current_hash = hashlib.sha256(binary).hexdigest().encode()
        for table in [release / MANIFEST, tmp_path / "server/latest/download" / MANIFEST]:
            table.write_bytes(table.read_bytes().replace(previous_hash, current_hash))
        (release / asset_name(TARGET)).write_bytes(binary)
        code, text = update()
        assert code == 0, text
        before = receipt.read_bytes()
        assert current_hash in before and exe.read_bytes() == binary
        assert not (tmp_path / "wrong destination").exists()

        (release / asset_name(TARGET)).write_bytes(b"corrupt")
        code, _ = update()
        assert code != 0
        assert receipt.read_bytes() == before and exe.read_bytes() == binary
        receipt.write_bytes(before.replace(b"install-v1", b"install-v2"))
        code, text = update("--dry-run")
        assert code == 2 and "receipt" in text


def test_native_install_upgrade_and_preserve_ownership(tmp_path: Path) -> None:
    binary = executable_fixture()
    release = release_fixture(tmp_path / "server", binary)
    dest = tmp_path / "user's native bin"
    receipt = dest / ".yaga-native-receipt"
    exe = dest / EXE
    with release_server(tmp_path / "server") as url:
        for args in [(), ("--version", VERSION, "--install-dir", str(dest))]:
            result = install(tmp_path, url, *args)
            assert result.returncode == 0, result.stderr
            assert exe.read_bytes() == binary
            assert receipt.read_bytes() == (
                f"yaga-native-install-v1 {VERSION}\n{hashlib.sha256(binary).hexdigest()}  {EXE}\n"
            ).encode("ascii")
            assert subprocess.check_output([str(exe), "--version"]).splitlines() == [
                f"yaga {VERSION}".encode()
            ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            contenders = list(pool.map(lambda _: install(tmp_path, url, "--native"), range(2)))
        assert any(result.returncode == 0 for result in contenders)
        assert not list(dest.glob(".yaga-install.*"))
        assert not (dest / ".yaga-native-lock").exists()
        assert exe.read_bytes() == binary
        assert (
            receipt.read_bytes()
            .splitlines()[1]
            .startswith(hashlib.sha256(binary).hexdigest().encode())
        )
        (release / asset_name(TARGET)).write_bytes(b"corrupt")
        before = receipt.read_bytes()
        failed = install(tmp_path, url, "--native", "--version", VERSION)
        assert failed.returncode != 0
        assert "checksum mismatch" in failed.stderr.lower()
        assert exe.read_bytes() == binary and receipt.read_bytes() == before
        (release / asset_name(TARGET)).write_bytes(binary)
        digest = hashlib.sha256(binary).hexdigest()
        changed = install(tmp_path, url, "--native", YAGA_NATIVE_UPDATE_FROM="f" * 64)
        assert changed.returncode != 0 and "changed" in changed.stderr.lower()
        newer = before.replace(f"v1 {VERSION}\n".encode(), b"v1 99999.0.0\n")
        receipt.write_bytes(newer)
        older = install(tmp_path, url, "--native", YAGA_NATIVE_UPDATE_FROM=digest)
        assert older.returncode != 0 and "older" in older.stderr.lower()
        assert receipt.read_bytes() == newer and exe.read_bytes() == binary
        receipt.write_bytes(before)
        for change in ("owned by another installer", "modified", "concurrent"):
            if change == "owned by another installer":
                receipt.unlink()
            elif change == "modified":
                receipt.write_bytes(before.replace(b"install-v1", b"install-v2"))
            else:
                receipt.write_bytes(before)
                (dest / ".yaga-native-lock").mkdir()
            failed = install(tmp_path, url, "--native")
            assert failed.returncode != 0, change
            assert exe.read_bytes() == binary
        assert (dest / ".yaga-native-lock").is_dir()
        assert not list(dest.glob(".yaga-install.*"))


@pytest.mark.parametrize("bad", ["version", "duplicate", "extra", "uppercase", "missing", "binary"])
def test_native_invalid_release_never_delegates(tmp_path: Path, bad: str) -> None:
    release = release_fixture(tmp_path / "server", b"fixture")
    file = release / MANIFEST
    data = file.read_bytes().splitlines(keepends=True)
    match bad:
        case "version":
            data[0] = b"yaga-native-v1 99.0.0\n"
        case "duplicate":
            data[-1] = data[-2]
        case "extra":
            data.append(b"ignored\n")
        case "uppercase":
            data[1] = data[1].upper()
        case "missing":
            file.unlink()
        case "binary":
            (release / asset_name(TARGET)).unlink()
    if bad != "missing":
        file.write_bytes(b"".join(data))
    with release_server(tmp_path / "server") as url:
        result = install(tmp_path, url, "--native", "--version", VERSION)
    assert result.returncode != 0
    assert not (tmp_path / "user's native bin" / EXE).exists()


@pytest.mark.parametrize("arguments", [(), ("--version", "0.4.0")])
def test_python_only_historical_release_uses_prior_manager(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    log = tmp_path / "uv-call"
    mock = tmp_path / ("uv.cmd" if WINDOWS else "uv")
    if WINDOWS:
        mock.write_text('@echo off\necho %* > "%YAGA_TEST_LOG%"\n')
    else:
        mock.write_text('#!/bin/sh\nprintf "%s" "$*" > "$YAGA_TEST_LOG"\n')
        mock.chmod(0o755)
    with release_server(tmp_path) as url:
        result = install(
            tmp_path,
            url,
            *arguments,
            YAGA_TEST_LOG=str(log),
            PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        )
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == "--no-config tool install --python 3.12 yaga-cli==0.4.0"


def test_crate_bundled_installers_match_docs_installers() -> None:
    for name in ("install.sh", "install.ps1"):
        assert (ROOT / "crates/yaga-cli/assets" / name).read_bytes() == (
            ROOT / "docs" / name
        ).read_bytes(), name


def test_release_matrix_matches_the_installer_asset_set(tmp_path: Path) -> None:
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_bytes(), yaml.BaseLoader)
    rows = workflow["jobs"]["native-binaries"]["strategy"]["matrix"]["include"]
    assert sorted(row["target"] for row in rows) == list(TARGETS)
    assert len({row["os"] for row in rows}) == 6
    assert workflow["jobs"]["native-assets"]["needs"] == "native-binaries"
    with pytest.raises(ValueError, match="six targets"):
        manifest(tmp_path)
    for target in TARGETS:
        (tmp_path / asset_name(target)).write_bytes(b"invalid")
    with pytest.raises(ValueError, match="architecture or format"):
        manifest(tmp_path)


@pytest.mark.skipif(WINDOWS, reason="Unix executable mode and shell profile")
def test_unix_path_update_preserves_quoted_directory(tmp_path: Path) -> None:
    release_fixture(tmp_path / "server", executable_fixture())
    with release_server(tmp_path / "server") as url:
        for _ in range(2):
            result = install(tmp_path, url, YAGA_NO_MODIFY_PATH="0", SHELL="/bin/sh")
            assert result.returncode == 0, result.stderr
    profile = tmp_path / ".profile"
    assert profile.read_text().count("export PATH") == 1
    exe = tmp_path / "user's native bin" / EXE
    assert exe.stat().st_mode & stat.S_IXUSR
    report = subprocess.check_output(["sh", "-c", '. "$1"; command -v yaga', "sh", str(profile)])
    assert report.decode().strip() == str(exe)


@pytest.mark.skipif(WINDOWS, reason="POSIX file permissions and shell startup files")
def test_unix_install_with_read_only_profile(tmp_path: Path) -> None:
    profile = tmp_path / ".bashrc"
    original = b"# Managed shell settings\n"
    profile.write_bytes(original)
    profile.chmod(0o444)
    if os.access(profile, os.W_OK):
        pytest.skip("requires a user who cannot write the read-only file")
    binary = executable_fixture()
    release_fixture(tmp_path / "server", binary)
    with release_server(tmp_path / "server") as url:
        result = install(tmp_path, url, "--native", YAGA_NO_MODIFY_PATH="0", SHELL="/bin/bash")
    assert result.returncode == 0, result.stderr
    assert "could not update" in result.stderr.lower()
    assert profile.read_bytes() == original and stat.S_IMODE(profile.stat().st_mode) == 0o444
    dest = tmp_path / "user's native bin"
    assert (dest / EXE).read_bytes() == binary
    assert (dest / ".yaga-native-receipt").read_bytes() == (
        f"yaga-native-install-v1 {VERSION}\n{hashlib.sha256(binary).hexdigest()}  {EXE}\n".encode()
    )
    export = next(line.strip() for line in result.stdout.splitlines() if "export PATH=" in line)
    assert subprocess.check_output(
        ["sh", "-c", export + "\ncommand -v yaga"], text=True
    ).strip() == str(dest / EXE)


@pytest.mark.skipif(not WINDOWS, reason="PowerShell per-user PATH")
@pytest.mark.parametrize("host", ["powershell", "pwsh"])
def test_windows_install_with_denied_user_path(tmp_path: Path, host: str) -> None:
    shell = shutil.which(host)
    if shell is None:
        pytest.skip(f"{host} is unavailable")
    command = [shell, "-NoProfile", "-NonInteractive"]
    utf8 = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
    # Redirect the installer's user PATH write to a file in this disposable process.
    wrapper = tmp_path / "denied-user-path.ps1"
    wrapper.write_text(
        """class Environment {
    static [OperatingSystem]$OSVersion = [System.Environment]::OSVersion
    static [string] GetEnvironmentVariable([string]$name, [string]$target) { return '' }
    static [void] SetEnvironmentVariable([string]$name, [string]$value, [string]$target) {
        [IO.File]::WriteAllText((Join-Path $env:HOME 'attempted-path'), "$target/$name/$value")
        throw [UnauthorizedAccessException]::new('Fixture denied a PATH update')
    }
}
if ([Environment] -eq [System.Environment]) { throw 'User PATH fixture is not isolated' }
"""
        + utf8
        + (ROOT / "docs/install.ps1").read_text("utf-8"),
        encoding="utf-8-sig",
    )
    binary = executable_fixture()
    release_fixture(tmp_path / "server", binary)
    dest = tmp_path / "Dev’s Tools" / "L'équipe ‘alpha’" / "‚beta‛"
    # Each host must discover its own modules when launched from another PowerShell version.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("YAGA_") and k.upper() != "PSMODULEPATH"
    }
    env.update(HOME=str(tmp_path), YAGA_INSTALL_DIR=str(dest))
    with release_server(tmp_path / "server") as url:
        result = subprocess.run(
            [*command, "-File", wrapper, "-Native"],
            cwd=tmp_path,
            env=env | {"YAGA_RELEASE_BASE_URL": url},
            encoding="utf-8",
            capture_output=True,
            timeout=45,
        )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attempted-path").read_text("utf-8") == f"User/Path/{dest}"
    assert "could not update" in result.stderr.lower()
    assert (dest / EXE).read_bytes() == binary
    assert (dest / ".yaga-native-receipt").read_bytes() == (
        f"yaga-native-install-v1 {VERSION}\n{hashlib.sha256(binary).hexdigest()}  {EXE}\n".encode()
    )
    assignment = next(line.strip() for line in result.stdout.splitlines() if "$env:Path =" in line)
    resolved = subprocess.check_output(
        [
            *command,
            "-Command",
            utf8
            + assignment
            + "; (Get-Command yaga -CommandType Application | Select-Object -First 1).Source",
        ],
        cwd=tmp_path,
        env=env,
        encoding="utf-8",
        timeout=45,
    )
    assert Path(resolved.strip()) == dest / EXE


@pytest.mark.skipif(not WINDOWS, reason="PowerShell runner without a home directory")
@pytest.mark.parametrize("host", ["powershell", "pwsh"])
@pytest.mark.parametrize("location", ["default", "argument", "environment"])
def test_windows_install_without_home(tmp_path: Path, host: str, location: str) -> None:
    shell = shutil.which(host)
    if shell is None:
        pytest.skip(f"{host} is unavailable")
    # Each PowerShell initializes its own HOME and module path from the process environment.
    excluded = {"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PSMODULEPATH"}
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("YAGA_") and k.upper() not in excluded
    }
    env.update(YAGA_NO_MODIFY_PATH="1", YAGA_RELEASE_BASE_URL="http://127.0.0.1:1")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-File", ROOT / "docs/install.ps1", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )

    if location == "default":
        help_result = run("--help")
        assert help_result.returncode == 0 and "-InstallDir" in help_result.stdout
        missing = run("--native")
        assert missing.returncode != 0 and "YAGA_INSTALL_DIR" in missing.stderr
        assert not list(tmp_path.iterdir())
        # The Python method can use an existing uv independently of the native install location.
        env["PATH"] = str(tmp_path)
        (tmp_path / "uv.cmd").write_text("@echo %* > uv-args.txt\n", encoding="ascii")
        python_install = run("--python")
        assert python_install.returncode == 0, python_install.stderr
        assert (tmp_path / "uv-args.txt").read_text().strip() == (
            "--no-config tool install --python 3.12 yaga-cli"
        )
        return

    binary = executable_fixture()
    release_fixture(tmp_path / "server", binary)
    dest = tmp_path / "chosen native bin"
    args: list[str] = []
    if location == "argument":
        args = ["--install-dir", str(dest)]
    else:
        env["YAGA_INSTALL_DIR"] = str(dest)
    with release_server(tmp_path / "server") as url:
        env["YAGA_RELEASE_BASE_URL"] = url
        installed = run("--native", *args)
    assert installed.returncode == 0, installed.stderr
    assert (dest / EXE).read_bytes() == binary
    assert (dest / ".yaga-native-receipt").read_bytes() == (
        f"yaga-native-install-v1 {VERSION}\n{hashlib.sha256(binary).hexdigest()}  {EXE}\n".encode()
    )


@pytest.mark.skipif(WINDOWS, reason="POSIX runner without a home directory")
@pytest.mark.parametrize("home", [None, ""])
def test_unix_install_without_home(tmp_path: Path, home: str | None) -> None:
    binary = executable_fixture()
    release_fixture(tmp_path / "server", binary)
    dest = tmp_path / "bin"
    with release_server(tmp_path / "server") as url:
        env = {"PATH": os.environ["PATH"], "SHELL": "/bin/sh", "YAGA_RELEASE_BASE_URL": url}
        if home is not None:
            env["HOME"] = home

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*command(), *args],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )

        help_result = run("--help")
        assert help_result.returncode == 0 and "--install-dir" in help_result.stdout
        missing = run("--native")
        assert missing.returncode == 2 and "--install-dir" in missing.stderr
        installed = run("--native", "--install-dir", str(dest))
        assert installed.returncode == 0, installed.stderr
        assert "export PATH" in installed.stdout
        assert (dest / EXE).read_bytes() == binary
        assert (
            hashlib.sha256(binary).hexdigest().encode()
            in (dest / ".yaga-native-receipt").read_bytes()
        )
        assert not (tmp_path / ".profile").exists()

        env.update(SHELL="/bin/zsh", ZDOTDIR=str(tmp_path), YAGA_INSTALL_DIR=str(dest))
        assert run("--native").returncode == 0
        assert str(dest) in (tmp_path / ".zshrc").read_text()


def wget_tools(tmp_path: Path, *, gnu: bool = True, certificate: Path | None = None) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in "sh uname grep mkdir mktemp rm rmdir awk wc tr cmp chmod sed mv".split():
        program = shutil.which(name)
        assert program
        (tools / name).symlink_to(program)
    for name in ("sha256sum", "shasum"):
        if program := shutil.which(name):
            (tools / name).symlink_to(program)
            break
    if gnu:
        wget = shutil.which("wget")
        if (
            not wget
            or b"GNU Wget" not in subprocess.run([wget, "--version"], capture_output=True).stdout
        ):
            pytest.skip("requires GNU Wget")
        if certificate is None:
            (tools / "wget").symlink_to(wget)
        else:
            # Trust only our test CA for this process. All HTTP and TLS handling uses real Wget.
            wrapper = tools / "wget"
            wrapper.write_text(
                f'#!/bin/sh\nexec {shlex.quote(wget)} --ca-certificate={shlex.quote(str(certificate))} "$@"\n'
            )
            wrapper.chmod(0o755)
    else:
        wget_stub = tools / "wget"
        wget_stub.write_text("#!/bin/sh\nexit 1\n")
        wget_stub.chmod(0o755)
    return tools


@pytest.mark.skipif(WINDOWS, reason="POSIX shell and utilities")
@pytest.mark.parametrize("gnu", [True, False], ids=["gnu-wget", "unsupported-wget"])
def test_native_wget_only_install(tmp_path: Path, gnu: bool) -> None:
    tools = wget_tools(tmp_path, gnu=gnu)
    binary = executable_fixture()
    release_fixture(tmp_path / "server", binary)
    dest = tmp_path / "bin"
    with release_server(tmp_path / "server") as url:
        result = install(tmp_path, url, "--native", "--install-dir", str(dest), PATH=str(tools))
    if gnu:
        assert result.returncode == 0, result.stderr
        assert (dest / EXE).read_bytes() == binary
    else:
        assert result.returncode == 2 and "GNU Wget" in result.stderr
        assert not dest.exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX shell and GNU Wget")
def test_wget_https_redirects_install_only_verified_release(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("requires OpenSSL to generate a temporary test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-out",
            cert,
            "-keyout",
            key,
        ],
        check=True,
        capture_output=True,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    tools = wget_tools(tmp_path, certificate=cert)
    binary = executable_fixture()
    repo = tmp_path / "server"
    release = release_fixture(repo, binary)
    (repo / "published").write_bytes((release / MANIFEST).read_bytes())
    requests: list[str] = []
    first = f"/mirror/latest/download/{MANIFEST}"
    asset = asset_name(TARGET)
    routes = {
        first: [("Location", "/signed/manifest?key=fixture")],
        "/signed/manifest?key=fixture": [("location", "../published?signature=fixture")],
        f"/mirror/download/v{VERSION}/{asset}": [
            ("LoCaTiOn", f"{{origin}}/download/v{VERSION}/{asset}")
        ],
    }

    class Redirects(QuietHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            if self.path not in routes:
                super().do_GET()
                return
            self.send_response(302)
            assert isinstance(self.server, http.server.ThreadingHTTPServer)
            for name, value in routes[self.path]:
                self.send_header(
                    name, value.format(origin=f"https://127.0.0.1:{self.server.server_port}")
                )
            self.end_headers()

    dest = tmp_path / "bin"
    with release_server(repo, Redirects, tls) as origin:

        def run() -> subprocess.CompletedProcess[str]:
            return install(
                tmp_path,
                origin + "/mirror",
                "--native",
                "--install-dir",
                str(dest),
                PATH=str(tools),
                no_proxy="127.0.0.1",
            )

        result = run()
        assert result.returncode == 0, result.stderr
        assert requests == [
            first,
            "/signed/manifest?key=fixture",
            "/published?signature=fixture",
            f"/mirror/download/v{VERSION}/{asset}",
            f"/download/v{VERSION}/{asset}",
        ]
        receipt = (dest / ".yaga-native-receipt").read_bytes()
        for headers in (
            [("Location", origin.replace("https:", "http:") + "/trap")],
            [("Location", origin.removeprefix("https:") + "/trap")],
            [("Location", origin.replace("https://", "https://user@") + "/trap")],
            [("Location", "/first"), ("location", "/second")],
        ):
            requests.clear()
            routes[first] = headers
            assert run().returncode == 2
            assert requests == [first]
            assert (dest / EXE).read_bytes() == binary
            assert (dest / ".yaga-native-receipt").read_bytes() == receipt

    # The explicit HTTP loopback mirror permits zero download redirects, even to itself.
    with release_server(repo, Redirects) as local:
        requests.clear()
        entry = f"/latest/download/{MANIFEST}"
        routes[entry] = [("Location", "/published")]
        result = install(tmp_path, local, "--native", PATH=str(tools), no_proxy="127.0.0.1")
        assert result.returncode == 2
        assert requests == [entry]


@pytest.mark.skipif(WINDOWS, reason="POSIX shell and GNU Wget")
def test_wget_relative_legacy_tag_stays_at_the_release_base(tmp_path: Path) -> None:
    tools = wget_tools(tmp_path)
    manager = tools / "uv"
    manager.write_text('#!/bin/sh\nprintf "%s" "$*" > "$YAGA_TEST_LOG"\n')
    manager.chmod(0o755)
    log = tmp_path / "manager-call"
    location = "tag/v0.4.0"

    class Latest(QuietHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("location", location)
            self.end_headers()

    with release_server(tmp_path, Latest) as url:

        def run() -> subprocess.CompletedProcess[str]:
            return install(
                tmp_path, url, PATH=str(tools), YAGA_TEST_LOG=str(log), no_proxy="127.0.0.1"
            )

        result = run()
        assert result.returncode == 0, result.stderr
        assert log.read_text() == "--no-config tool install --python 3.12 yaga-cli==0.4.0"
        log.unlink()
        location = "/other/tag/v0.4.0"
        assert run().returncode == 2
        assert not log.exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX atomic replacement failure injection")
def test_failed_binary_replacement_restores_previous_receipt(tmp_path: Path) -> None:
    binary = executable_fixture()
    release_fixture(tmp_path / "server", binary)
    dest = tmp_path / "user's native bin"
    receipt = dest / ".yaga-native-receipt"
    with release_server(tmp_path / "server") as url:
        assert install(tmp_path, url).returncode == 0
        # Record a distinct, valid previous release receipt so a lost rollback is observable.
        old_record = receipt.read_bytes().replace(f"v1 {VERSION}".encode(), b"v1 0.0.1")
        receipt.write_bytes(old_record)
        shadow = tmp_path / "bin"
        shadow.mkdir()
        mv = shadow / "mv"
        real = shutil.which("mv")
        assert real and "'" not in real
        mv.write_text(f'#!/bin/sh\ncase "$3" in */yaga) exit 1;; esac\nexec \'{real}\' "$@"\n')
        mv.chmod(0o755)
        result = install(tmp_path, url, PATH=str(shadow) + os.pathsep + os.environ["PATH"])
        assert result.returncode != 0
        assert (dest / EXE).read_bytes() == binary and receipt.read_bytes() == old_record


@pytest.mark.skipif(WINDOWS, reason="POSIX shell signal handling")
@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("stage", ["lock", "receipt", "yaga"])
def test_interrupted_native_install_preserves_ownership(
    tmp_path: Path, stage: str, upgrade: bool
) -> None:
    binary = f"#!/bin/sh\necho 'yaga {VERSION}'\n".encode()
    previous = binary + b"# previously installed revision\n"
    dest = tmp_path / "user's native bin"
    receipt = dest / ".yaga-native-receipt"
    if upgrade:
        release_fixture(tmp_path / "old", previous)
        with release_server(tmp_path / "old") as url:
            assert install(tmp_path, url).returncode == 0

    release_fixture(tmp_path / "new", binary)
    tools = tmp_path / "tools"
    tools.mkdir()
    name, argument, suffix = (
        ("mkdir", "$1", ".yaga-native-lock") if stage == "lock" else ("mv", "$2", stage)
    )
    real_tool = shutil.which(name)
    assert real_tool
    wrapper = tools / name
    wrapper.write_text(
        f'#!/bin/sh\n{shlex.quote(real_tool)} "$@" || exit $?\n'
        f'case "{argument}" in */{suffix}) kill -TERM "$PPID";; esac\n'
    )
    wrapper.chmod(0o755)
    with release_server(tmp_path / "new") as url:
        run = install(tmp_path, url, PATH=str(tools) + os.pathsep + os.environ["PATH"])
        assert run.returncode != 0
        expected = binary if stage == "yaga" else previous if upgrade else None
        if expected is None:
            assert not (dest / EXE).exists() and not receipt.exists()
        else:
            assert (dest / EXE).read_bytes() == expected
            assert receipt.read_text() == (
                f"yaga-native-install-v1 {VERSION}\n{hashlib.sha256(expected).hexdigest()}  {EXE}\n"
            )
        assert not (dest / ".yaga-native-lock").exists()
        assert not list(dest.glob(".yaga-install.*"))
        assert install(tmp_path, url).returncode == 0
