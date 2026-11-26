mod release;

use clap::Subcommand;
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    process::{Command, ExitCode},
};
use yaga_core::{
    fs::{absolute_path, read_regular_prefix},
    git::external_executable,
    json::strict_value,
    text::{python_repr, safe_text},
};
#[cfg(not(windows))]
use {std::time::Duration, yaga_core::git::process::run_bounded};

#[derive(Subcommand)]
pub enum Operation {
    /// Update the current installation through its recorded owner.
    #[command(args_override_self = true)]
    Update {
        #[arg(long)]
        dry_run: bool,
    },
}
const UNMANAGED: &str = "self update requires a standalone native release, uv tool, or local Cargo checkout installation; update this executable with the manager that installed it";
struct Plan {
    tool: PathBuf,
    arguments: Vec<OsString>,
    root: PathBuf,
}

fn token(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
}
fn owned_installation(exe: &Path) -> Result<(PathBuf, PathBuf, Vec<OsString>), String> {
    let exe = absolute_path(exe).map_err(|_| UNMANAGED)?;
    let bin = exe
        .parent()
        .filter(|dir| dir.file_name().is_some_and(|s| s == "bin"))
        .ok_or(UNMANAGED)?;
    let root = bin.parent().ok_or(UNMANAGED)?;
    let name = if cfg!(windows) { "yaga.exe" } else { "yaga" };
    if exe.file_name().is_none_or(|s| s != name) {
        return Err(UNMANAGED.into());
    }
    let receipt =
        read_regular_prefix(&root.join(".crates2.json"), 1024 * 1024).map_err(|_| UNMANAGED)?;
    if receipt.len() > 1024 * 1024 {
        return Err("Cargo installation receipt exceeds its byte limit".into());
    }
    let receipt = strict_value(&receipt).map_err(|_| "Cargo installation receipt is invalid")?;
    let entries = receipt["installs"]
        .as_object()
        .ok_or("Cargo installation receipt is invalid")?;
    let mut owners = entries.iter().filter(|(_, p)| {
        p["bins"]
            .as_array()
            .is_some_and(|arr| arr.iter().any(|v| v == name))
    });
    let (identity, p) = owners.next().ok_or(UNMANAGED)?;
    if owners.next().is_some()
        || !identity.starts_with("yaga-cli ")
        || p["bins"].as_array().is_none_or(|bins| bins.len() != 1)
    {
        return Err(UNMANAGED.into());
    }
    let (_, origin) = identity.split_once(" (path+").ok_or(UNMANAGED)?;
    let url = url::Url::parse(origin.strip_suffix(')').ok_or(UNMANAGED)?).map_err(|_| UNMANAGED)?;
    if url.scheme() != "file"
        || url.host_str().is_some_and(|host| host != "localhost")
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(UNMANAGED.into());
    }
    let path = absolute_path(&url.to_file_path().map_err(|_| UNMANAGED)?).map_err(|_| UNMANAGED)?;
    let manifest = read_regular_prefix(&path.join("Cargo.toml"), 1024 * 1024)
        .ok()
        .filter(|bytes| bytes.len() <= 1024 * 1024)
        .ok_or("the recorded Cargo checkout is unavailable")?;
    let manifest = std::str::from_utf8(&manifest)
        .ok()
        .and_then(|s| toml::from_str::<toml::Table>(s).ok())
        .ok_or("the recorded Cargo manifest is invalid")?;
    if manifest
        .get("package")
        .and_then(|p| p.get("name"))
        .and_then(|s| s.as_str())
        != Some("yaga-cli")
    {
        return Err("the recorded checkout does not contain the YAGA CLI package".into());
    }
    let mut args: Vec<OsString> = ["install", "--locked", "--force", "--path"]
        .into_iter()
        .map(Into::into)
        .collect();
    args.extend([
        path.as_os_str().into(),
        "--root".into(),
        root.as_os_str().into(),
        "--bin".into(),
        "yaga".into(),
    ]);
    let invalid = "Cargo installation settings are invalid";
    for (key, flag) in [("profile", "--profile"), ("target", "--target")] {
        let s = p[key].as_str().filter(|s| token(s)).ok_or(invalid)?;
        args.extend([flag.into(), s.into()]);
    }
    for (key, flag) in [
        ("all_features", "--all-features"),
        ("no_default_features", "--no-default-features"),
    ] {
        if p[key].as_bool().ok_or(invalid)? {
            args.push(flag.into())
        }
    }
    let features = p["features"].as_array().ok_or(invalid)?;
    if features.len() > 256 {
        return Err(invalid.into());
    }
    for f in features {
        let f = f
            .as_str()
            .filter(|s| s.split('/').all(token))
            .ok_or(invalid)?;
        // Cargo retains previously selected features in .crates2.json.
        // Stop carrying the retired local-inference switch into new receipts.
        if !matches!(
            f,
            "quality-models" | "yaga-cli/quality-models" | "yaga-core/quality-models"
        ) {
            args.extend(["--features".into(), f.into()]);
        }
    }
    if let Some(version) = p.get("version_req").filter(|s| !s.is_null()) {
        let version = version
            .as_str()
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= 128
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"=^.*+<>-~ ,".contains(&b))
            })
            .ok_or(invalid)?;
        args.extend(["--version".into(), version.into()]);
    }
    Ok((root.to_owned(), path, args))
}

fn plan(exe: &Path) -> Result<Plan, String> {
    let (root, path, arguments) = owned_installation(exe)?;
    let resolved = external_executable(&path, "cargo")
        .map_err(|_| "could not resolve a trusted Cargo executable outside the source checkout")?;
    let cwd = std::env::current_dir().map_err(|_| "could not determine the working directory")?;
    if external_executable(&cwd, "cargo").as_ref() != Ok(&resolved) {
        return Err(
            "could not resolve a trusted Cargo executable outside the current directory".into(),
        );
    }
    // Preserve the proxy's argv[0] when cargo is a verified symlink to rustup.
    let program = if cfg!(windows) { "cargo.exe" } else { "cargo" };
    let path = std::env::var_os("PATH").unwrap_or_default();
    let tool = std::env::split_paths(&path)
        .map(|p| p.join(program))
        .find(|p| p.is_absolute() && p.canonicalize().ok().as_ref() == Some(&resolved))
        .ok_or("could not locate the verified Cargo tool")?;
    Ok(Plan {
        tool,
        arguments,
        root,
    })
}
fn update(dry_run: bool) -> Result<(), String> {
    let exe = std::env::current_exe()
        .and_then(|path| absolute_path(&path))
        .map_err(|_| UNMANAGED)?;
    if let Some(digest) = release::receipt(&exe)? {
        return release::update(&exe, &digest, dry_run);
    }
    let plan = plan(&exe)?;
    if dry_run {
        let args = std::iter::once(plan.tool.as_os_str())
            .chain(plan.arguments.iter().map(OsString::as_os_str))
            .map(|s| python_repr(&s.to_string_lossy()))
            .collect::<Vec<_>>();
        println!(
            "Update command: {}",
            safe_text(&format!("[{}]", args.join(", ")), 2000)
        );
        return Ok(());
    }
    #[cfg(not(windows))]
    {
        let mut cmd = Command::new(plan.tool);
        cmd.args(plan.arguments).current_dir(plan.root);
        let output = run_bounded(
            cmd,
            None,
            2 * 1024 * 1024,
            2 * 1024 * 1024,
            Duration::from_secs(600),
        )
        .map_err(|_| "Cargo could not complete the update")?;
        if !output.status.success() {
            return Err("Cargo could not complete the update; run self update --dry-run to inspect the command".into());
        }
        println!(
            "Cargo completed the update. Run yaga --version to inspect the installed version."
        );
    }
    #[cfg(windows)]
    {
        windows_handoff(&plan, &exe, None)?;
    }
    Ok(())
}

pub fn run(operation: Operation) -> ExitCode {
    let Operation::Update { dry_run } = operation;
    if std::env::var_os("_YAGA_WHEEL_UPDATE_REQUEST").is_some_and(|s| s == "1") {
        // A wheel update must run in the original interpreter/launcher. Only an
        // accepted explicit self-update argument vector reaches this request.
        return ExitCode::from(if dry_run { 80 } else { 81 });
    }
    match update(dry_run) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{}", safe_text(&e, 1000));
            ExitCode::from(2)
        }
    }
}

#[cfg(windows)]
fn windows_shell() -> Result<PathBuf, String> {
    std::env::var_os("SystemRoot")
        .map(PathBuf::from)
        .map(|r| r.join("System32/WindowsPowerShell/v1.0/powershell.exe"))
        .filter(|p| p.is_absolute() && p.is_file())
        .ok_or_else(|| "could not locate the Windows update worker runtime".into())
}

#[cfg(windows)]
fn windows_handoff(plan: &Plan, exe: &Path, native_digest: Option<&str>) -> Result<(), String> {
    use std::{fs::File, os::windows::process::CommandExt, process::Stdio};
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    // ProcessStartInfo.Arguments on .NET Framework accepts the Windows argv
    // representation. Quote every value, including whitespace and terminal backslashes.
    fn quote(s: &str) -> String {
        let mut result = String::from("\"");
        let mut backslashes = 0;
        for ch in s.chars() {
            if ch == '\\' {
                backslashes += 1;
                continue;
            }
            let n = if ch == '"' {
                backslashes * 2 + 1
            } else {
                backslashes
            };
            result.extend(std::iter::repeat_n('\\', n));
            result.push(ch);
            backslashes = 0
        }
        result.extend(std::iter::repeat_n('\\', backslashes * 2));
        result.push('"');
        result
    }
    let shell = windows_shell()?;
    let temporary = tempfile::Builder::new()
        .prefix("yaga-update-")
        .tempdir()
        .map_err(|_| "could not initialize the update log")?;
    let log = temporary.path().join("update.log");
    let output = File::create(&log).map_err(|_| "could not open the update log")?;
    let error_output = output
        .try_clone()
        .map_err(|_| "could not open the update log")?;
    let args = plan
        .arguments
        .iter()
        .map(|s| {
            s.to_str()
                .map(quote)
                .ok_or("Cargo arguments must be valid UTF-8 for the Windows handoff")
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut cmd = Command::new(&shell);
    let working_dir = if native_digest.is_some() {
        temporary.path()
    } else {
        &plan.root
    };
    if let Some(digest) = native_digest {
        release::installer_env(&mut cmd, &plan.root, digest);
        let modules = shell
            .parent()
            .expect("absolute Windows PowerShell directory")
            .join("Modules");
        cmd.env("PSModulePath", modules).env(
            "YAGA_UPDATE_INSTALLER",
            include_str!("../assets/install.ps1"),
        );
    }
    cmd.creation_flags(CREATE_NO_WINDOW)
        .current_dir(working_dir)
        .env("YAGA_UPDATE_PARENT", std::process::id().to_string())
        .env("YAGA_UPDATE_IMAGE", exe)
        .env("YAGA_UPDATE_TOOL", &plan.tool)
        .env("YAGA_UPDATE_ARGUMENTS", args.join(" "))
        .env("YAGA_UPDATE_ROOT", working_dir)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            include_str!("self_update_worker.ps1"),
        ])
        .stdin(Stdio::null())
        .stdout(output)
        .stderr(error_output)
        .spawn()
        .map_err(|_| "could not start the Windows update worker")?;
    let _directory = temporary.keep();
    println!(
        "Update scheduled after YAGA exits. Completion log: {}",
        safe_text(&log.to_string_lossy(), 500)
    );
    println!("Wait for YAGA_UPDATE_EXIT_CODE=0 in that log, then run yaga --version.");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn receipt_selects_only_the_recorded_local_package_and_features() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("root");
        let src = temp.path().join("source");
        std::fs::create_dir_all(root.join("bin")).unwrap();
        std::fs::create_dir_all(&src).unwrap();
        let name = if cfg!(windows) { "yaga.exe" } else { "yaga" };
        let exe = root.join("bin").join(name);
        std::fs::write(&exe, "fixture").unwrap();
        std::fs::write(src.join("Cargo.toml"), "[package]\nname='yaga-cli'\n").unwrap();
        let origin = url::Url::from_directory_path(&src).unwrap();
        let identity = format!("yaga-cli 0.5.0 (path+{origin})");
        let record = serde_json::json!({"version_req":null,"bins":[name],"features":["quality-models","quality-http"],"all_features":false,"no_default_features":false,"profile":"release","target":"x86_64-unknown-linux-gnu"});
        let mut receipt = serde_json::json!({"installs":{(identity):record}});
        let path = root.join(".crates2.json");
        std::fs::write(&path, receipt.to_string()).unwrap();
        let (_, _, args) = owned_installation(&exe).unwrap();
        assert!(!args.iter().any(|a| a == "quality-models"));
        assert!(args.iter().any(|a| a == "quality-http"));
        receipt["installs"]["other 0.1.0 (unowned)"] = record;
        std::fs::write(path, receipt.to_string()).unwrap();
        assert!(owned_installation(&exe).is_err())
    }

    #[test]
    fn native_receipt_resolves_canonical_executable_and_unmanaged_mentions_native() {
        use sha2::{Digest, Sha256};
        let temp = tempfile::tempdir().unwrap();
        let bin_dir = temp.path().join("native-bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let name = if cfg!(windows) { "yaga.exe" } else { "yaga" };
        let exe = bin_dir.join(name);
        let payload = b"native-binary-fixture";
        std::fs::write(&exe, payload).unwrap();
        let digest = format!("{:x}", Sha256::digest(payload));
        let receipt = format!(
            "yaga-native-install-v1 {}\n{digest}  {name}\n",
            env!("CARGO_PKG_VERSION")
        );
        std::fs::write(bin_dir.join(".yaga-native-receipt"), receipt).unwrap();
        assert_eq!(release::receipt(&exe).unwrap(), Some(digest.clone()));
        #[cfg(unix)]
        {
            let link_dir = temp.path().join("links");
            std::fs::create_dir_all(&link_dir).unwrap();
            let link = link_dir.join(name);
            std::os::unix::fs::symlink(&exe, &link).unwrap();
            assert_eq!(release::receipt(&link).unwrap(), Some(digest));
        }
        assert!(UNMANAGED.contains("standalone native release"));
    }
}
