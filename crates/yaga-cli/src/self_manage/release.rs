use std::{io, path::Path, process::Command};
use yaga_core::{
    fs::{read_regular_prefix, sha256_regular_file},
    text::{python_repr, safe_text},
};

const RECEIPT: &str = ".yaga-native-receipt";
const INVALID: &str = "the native installation receipt does not match this executable";

pub(super) fn receipt(exe: &Path) -> Result<Option<String>, String> {
    let dir = exe.parent().ok_or(INVALID)?;
    let contents = match read_regular_prefix(&dir.join(RECEIPT), 256) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(INVALID.into()),
        Ok(bytes) => bytes,
    };
    let name = if cfg!(windows) { "yaga.exe" } else { "yaga" };
    if contents.len() > 256 || exe.file_name().is_none_or(|s| s != name) {
        return Err(INVALID.into());
    }
    let digest = sha256_regular_file(exe, 128 * 1024 * 1024).map_err(|_| INVALID)?;
    let version = env!("CARGO_PKG_VERSION");
    let expected = format!("yaga-native-install-v1 {version}\n{digest}  {name}\n");
    if expected.as_bytes() != contents {
        return Err(INVALID.into());
    }
    Ok(Some(digest))
}

pub(super) fn installer_env(command: &mut Command, dir: &Path, digest: &str) {
    command
        .env("YAGA_INSTALL_METHOD", "native")
        .env("YAGA_INSTALL_DIR", dir)
        .env("YAGA_NATIVE_UPDATE_FROM", digest)
        .env("YAGA_NO_MODIFY_PATH", "1")
        .env_remove("YAGA_VERSION");
}

pub(super) fn update(exe: &Path, digest: &str, dry_run: bool) -> Result<(), String> {
    let dir = exe.parent().ok_or(INVALID)?;
    if dry_run {
        println!(
            "Update {} from the latest native release using the verified installer.",
            safe_text(&python_repr(&exe.to_string_lossy()), 1000)
        );
        return Ok(());
    }
    #[cfg(unix)]
    {
        use std::time::Duration;
        use yaga_core::git::process::run_bounded;
        let mut cmd = Command::new("/bin/sh");
        installer_env(&mut cmd, dir, digest);
        cmd.current_dir("/")
            .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
            .arg("-s");
        let script = include_str!("../../assets/install.sh").replace("\r\n", "\n");
        let output = run_bounded(
            cmd,
            Some(script.into_bytes()),
            2 << 20,
            2 << 20,
            Duration::from_secs(600),
        )
        .map_err(|_| "native installer could not complete the update")?;
        if !output.status.success() {
            return Err(
                "native update failed; verify the release source and installation receipt".into(),
            );
        }
        println!("Native update complete. Run yaga --version to inspect the installed version.");
        Ok(())
    }
    #[cfg(windows)]
    {
        let plan = super::Plan {
            tool: super::windows_shell()?,
            arguments: [
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "& ([ScriptBlock]::Create($env:YAGA_UPDATE_INSTALLER))",
            ]
            .into_iter()
            .map(Into::into)
            .collect(),
            root: dir.to_owned(),
        };
        super::windows_handoff(&plan, exe, Some(digest))
    }
}
