//! Shell completion scripts and bounded, encoding-preserving profile updates.

use crate::{
    fs::{absolute_path, open_regular_for_write, read_regular_prefix},
    text::safe_text,
};
use std::{
    fs,
    io::{self, Write},
    path::Path,
};

const MAX: usize = 1024 * 1024;
const START: &str = "# >>> yaga completion >>>";
const END: &str = "# <<< yaga completion <<<";
const UTF8: &[u8] = b"\xef\xbb\xbf";
const LE: &[u8] = b"\xff\xfe";
const BE: &[u8] = b"\xfe\xff";

/// Return the committed shell script shared by the installer and Python package.
pub fn script(shell: &str) -> Option<&'static str> {
    let text = match shell {
        "bash" => include_str!("completion/yaga.bash"),
        "fish" => include_str!("completion/yaga.fish"),
        "zsh" => include_str!("completion/yaga.zsh"),
        "pwsh" | "powershell" => include_str!("completion/yaga.ps1"),
        _ => return None,
    };
    Some(text.trim())
}

fn decode<'a>(raw: &'a [u8], label: &str) -> Result<(&'a [u8], String), String> {
    if raw.starts_with(b"\xff\xfe\0\0") || raw.starts_with(b"\0\0\xfe\xff") {
        return Err(format!(
            "shell profile {label} uses unsupported UTF-32 encoding"
        ));
    }
    let bom = [LE, BE, UTF8]
        .into_iter()
        .find(|b| raw.starts_with(b))
        .unwrap_or(&[]);
    let payload = &raw[bom.len()..];
    let codec = if bom == LE {
        "utf-16-le"
    } else if bom == BE {
        "utf-16-be"
    } else {
        "utf-8"
    };
    let invalid =
        || format!("shell profile {label} could not be decoded as {codec}; refusing to modify it");
    let text = if bom == LE || bom == BE {
        let (pairs, remainder) = payload.as_chunks::<2>();
        if !remainder.is_empty() {
            return Err(invalid());
        }
        let words = pairs
            .iter()
            .map(|&s| {
                if bom == LE {
                    u16::from_le_bytes(s)
                } else {
                    u16::from_be_bytes(s)
                }
            })
            .collect::<Vec<_>>();
        String::from_utf16(&words).map_err(|_| invalid())?
    } else {
        if bom.is_empty() && payload.contains(&0) {
            return Err(format!(
                "shell profile {label} contains NUL bytes without a UTF-16 BOM; refusing to modify it"
            ));
        }
        std::str::from_utf8(payload)
            .map_err(|_| invalid())?
            .to_owned()
    };
    Ok((bom, text))
}

pub fn write(path: &Path, data: &[u8]) -> io::Result<()> {
    // Address the target of a user-managed symlink, like Python's Path.write_bytes.
    // Writing its existing inode preserves hardlinks, ACLs, ownership and permissions.
    let physical = absolute_path(path)?;
    let parent = physical
        .parent()
        .ok_or_else(|| io::Error::other("missing parent"))?;
    fs::create_dir_all(parent)?;
    let mut file = open_regular_for_write(&physical)?;
    file.write_all(data)?;
    file.set_len(data.len() as u64)?;
    file.flush()
}

pub fn upsert(path: &Path, body: &str, default_newline: &str, legacy: bool) -> Result<(), String> {
    let label = safe_text(&path.to_string_lossy(), 300);
    let raw = match path.metadata() {
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            if path.is_symlink() {
                return Err(format!("shell profile path is a broken symlink: {label}"));
            }
            None
        }
        Ok(m) if m.is_file() => {
            let raw = absolute_path(path)
                .and_then(|p| read_regular_prefix(&p, MAX))
                .map_err(|_| format!("cannot read shell profile {label}"))?;
            if raw.len() > MAX {
                return Err(format!("shell profile exceeds {MAX} bytes: {label}"));
            }
            Some(raw)
        }
        Ok(_) => return Err(format!("shell profile path is not a regular file: {label}")),
        Err(_) => return Err(format!("cannot read shell profile {label}")),
    };
    let body = body.trim().lines().collect::<Vec<_>>().join("\n");
    let encoded = profile_bytes(
        raw.as_deref(),
        &body,
        default_newline,
        if legacy { UTF8 } else { &[] },
        &label,
    )?;
    write(path, &encoded).map_err(|_| format!("cannot write shell profile {label}"))
}

/// Replace the managed block in an existing profile or encode a new one.
/// The caller supplies a body with LF line endings and a display-safe path label.
pub fn profile_bytes(
    raw: Option<&[u8]>,
    body: &str,
    default_newline: &str,
    default_bom: &[u8],
    label: &str,
) -> Result<Vec<u8>, String> {
    let (bom, old) = match raw {
        Some(raw) => decode(raw, label)?,
        None => (default_bom, String::new()),
    };
    let newline = if old.contains("\r\n") {
        "\r\n"
    } else if old.contains('\n') {
        "\n"
    } else {
        default_newline
    };
    let body = body.replace('\n', newline);
    let block = format!("{START}{newline}{body}{newline}{END}");
    let (starts, ends) = (old.matches(START).count(), old.matches(END).count());
    if starts != ends || starts > 1 {
        return Err(format!(
            "shell profile {label} has unmatched or duplicate YAGA completion markers"
        ));
    }
    let updated = if let (Some(start), Some(end)) = (old.find(START), old.find(END)) {
        if end < start {
            return Err(format!(
                "shell profile {label} has reversed YAGA completion markers"
            ));
        }
        let mut updated = format!("{}{block}{}", &old[..start], &old[end + END.len()..]);
        if !updated.ends_with(newline) {
            updated.push_str(newline)
        }
        updated
    } else {
        let old = old.trim_end_matches(['\r', '\n']);
        if old.is_empty() {
            format!("{block}{newline}")
        } else {
            format!("{old}{newline}{newline}{block}{newline}")
        }
    };
    let mut encoded = bom.to_vec();
    if raw.is_some() && (bom == LE || bom == BE) {
        for word in updated.encode_utf16() {
            encoded.extend(if bom == LE {
                word.to_le_bytes()
            } else {
                word.to_be_bytes()
            })
        }
    } else {
        encoded.extend_from_slice(updated.as_bytes())
    }
    Ok(encoded)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn edits_hardlinked_profiles_and_scripts_in_place() {
        let tmp = tempfile::tempdir().unwrap();
        let profile = tmp.path().join("profile");
        let alias = tmp.path().join("alias");
        fs::write(&profile, b"# my settings\n").unwrap();
        fs::hard_link(&profile, &alias).unwrap();
        upsert(&profile, "new settings", "\n", false).unwrap();
        let updated = fs::read(&profile).unwrap();
        assert!(updated.starts_with(b"# my settings\n"));
        assert_eq!(fs::read(&alias).unwrap(), updated);
        write(&profile, b"short").unwrap();
        assert_eq!(fs::read(&alias).unwrap(), b"short");
    }

    #[cfg(unix)]
    #[test]
    fn respects_umask_symlink_targets_and_write_permissions() {
        use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};

        let tmp = tempfile::tempdir().unwrap();
        let profile = tmp.path().join("profile");
        let reference = tmp.path().join("reference");
        fs::write(&reference, b"reference").unwrap();
        write(&profile, b"initial").unwrap();
        assert_eq!(
            profile.metadata().unwrap().mode(),
            reference.metadata().unwrap().mode()
        );

        let link = tmp.path().join("user-profile");
        symlink(&profile, &link).unwrap();
        let original = profile.metadata().unwrap();
        upsert(&link, "body", "\n", false).unwrap();
        let updated = fs::read(&profile).unwrap();
        assert!(link.is_symlink());
        let now = profile.metadata().unwrap();
        assert_eq!(
            (now.ino(), now.uid(), now.gid()),
            (original.ino(), original.uid(), original.gid())
        );
        fs::set_permissions(&profile, fs::Permissions::from_mode(0o444)).unwrap();
        if fs::OpenOptions::new().write(true).open(&profile).is_err() {
            assert!(upsert(&link, "body", "\n", false).is_err());
            assert_eq!(fs::read(&profile).unwrap(), updated);
        }

        let socket = tmp.path().join("socket");
        let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        assert!(write(&socket, b"script").is_err());
    }

    #[test]
    fn retains_encoding_newlines_and_marker_idempotence() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("profile.ps1");
        for bom in [LE, BE, UTF8, &[]] {
            let mut raw = bom.to_vec();
            let text = "ä custom\r\n";
            if bom == LE || bom == BE {
                for w in text.encode_utf16() {
                    raw.extend(if bom == LE {
                        w.to_le_bytes()
                    } else {
                        w.to_be_bytes()
                    })
                }
            } else {
                raw.extend_from_slice(text.as_bytes())
            }
            fs::write(&path, &raw).unwrap();
            upsert(&path, "test\nline", "\n", false).unwrap();
            let once = fs::read(&path).unwrap();
            assert!(once.starts_with(&raw));
            assert_eq!(decode(&once, "test").unwrap().1.matches(START).count(), 1);
            upsert(&path, "test\nline", "\n", false).unwrap();
            assert_eq!(once, fs::read(&path).unwrap());
        }
        fs::write(&path, b"x\0").unwrap();
        assert!(upsert(&path, "test", "\n", false).is_err());
        assert_eq!(fs::read(path).unwrap(), b"x\0")
    }
}
