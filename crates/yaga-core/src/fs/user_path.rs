//! Home-directory expansion at explicit user-input boundaries.
use std::{
    ffi::OsStr,
    io,
    path::{Component, Path, PathBuf},
};

/// Resolve an explicit CLI path with pathlib's leading-home semantics.
/// Relative paths inside a policy or a Git object have their own binding rules.
pub fn resolve_user_path(path: &Path) -> io::Result<PathBuf> {
    soft_canonicalize::soft_canonicalize(expand_user_path(path)?)
}

pub(crate) fn expand_user_path(path: &Path) -> io::Result<std::borrow::Cow<'_, Path>> {
    use std::borrow::Cow;
    let mut parts = path.components();
    let Some(Component::Normal(first)) = parts.find(|p| *p != Component::CurDir) else {
        return Ok(Cow::Borrowed(path));
    };
    if !first.as_encoded_bytes().starts_with(b"~") {
        return Ok(Cow::Borrowed(path));
    }

    let mut expanded = user_home(first)
        .filter(|home| !home.as_os_str().as_encoded_bytes().starts_with(b"~"))
        .ok_or_else(|| io::Error::other("Could not determine home directory."))?;
    let tail = parts.as_path();
    if !tail.as_os_str().is_empty() {
        // A drive prefix in the original tail remains an interior component.
        #[cfg(windows)]
        if matches!(tail.components().next(), Some(Component::Prefix(_))) {
            let mut literal = std::ffi::OsString::from(".\\");
            literal.push(tail);
            expanded.push(literal);
        } else {
            expanded.push(tail);
        }
        #[cfg(not(windows))]
        expanded.push(tail);
    }
    if expanded.as_os_str().is_empty() {
        expanded.push(".");
    }
    Ok(Cow::Owned(expanded))
}

#[cfg(unix)]
#[allow(unsafe_code)] // POSIX account lookup accepts OS byte strings, including non-UTF-8 names.
fn user_home(component: &OsStr) -> Option<PathBuf> {
    use std::{
        ffi::{CStr, CString},
        mem::MaybeUninit,
        os::unix::ffi::OsStrExt,
        ptr,
    };

    let username = &component.as_bytes()[1..];
    let home = if username.is_empty()
        && let Some(home) = std::env::var_os("HOME")
    {
        PathBuf::from(home)
    } else {
        let name = CString::new(username).ok()?;
        let mut entry = MaybeUninit::<libc::passwd>::uninit();
        let mut buffer = vec![0u8; 1024];
        loop {
            let mut result = ptr::null_mut();
            // SAFETY: Both reentrant routines write their record and strings into
            // the caller-owned storage; the returned record is read only on success.
            let status = unsafe {
                if username.is_empty() {
                    libc::getpwuid_r(
                        libc::getuid(),
                        entry.as_mut_ptr(),
                        buffer.as_mut_ptr().cast(),
                        buffer.len(),
                        &mut result,
                    )
                } else {
                    libc::getpwnam_r(
                        name.as_ptr(),
                        entry.as_mut_ptr(),
                        buffer.as_mut_ptr().cast(),
                        buffer.len(),
                        &mut result,
                    )
                }
            };
            if status == libc::ERANGE {
                let larger = buffer.len().checked_mul(2)?;
                buffer.try_reserve(buffer.len()).ok()?;
                buffer.resize(larger, 0);
                continue;
            }
            if status != 0 || result.is_null() {
                return None;
            }
            // SAFETY: status == 0 and result is non-null, pointing into `entry`,
            // and pw_dir is a NUL-terminated C string inside `buffer`.
            let directory = unsafe { (*result).pw_dir };
            if directory.is_null() {
                return None;
            }
            // SAFETY: directory is non-null and NUL-terminated until `buffer` drops.
            let bytes = unsafe { CStr::from_ptr(directory) }.to_bytes();
            break PathBuf::from(OsStr::from_bytes(bytes));
        }
    };
    // posixpath strips trailing slashes and uses "/" for an empty home.
    Some(if home.as_os_str().is_empty() {
        PathBuf::from("/")
    } else {
        home
    })
}

#[cfg(windows)]
/// Expand a raw, separate Windows CLI path before converting it to a Path.
/// Typer performs this pass on argv before its Path.expanduser-based loader.
pub fn windows_cli_home(word: &OsStr) -> Option<std::ffi::OsString> {
    use std::{
        ffi::OsString,
        os::windows::ffi::{OsStrExt, OsStringExt},
    };
    if !word.as_encoded_bytes().starts_with(b"~") {
        return None;
    }
    let units: Vec<u16> = word.encode_wide().collect();
    let end = units
        .iter()
        .position(|c| matches!(*c, 47 | 92))
        .unwrap_or(units.len());
    let mut expanded = user_home(&OsString::from_wide(&units[..end]))?.into_os_string();
    expanded.push(OsString::from_wide(&units[end..]));
    Some(expanded)
}

#[cfg(windows)]
fn user_home(component: &OsStr) -> Option<PathBuf> {
    use std::{
        env::var_os,
        ffi::OsString,
        os::windows::ffi::{OsStrExt, OsStringExt},
    };
    let profile = var_os("USERPROFILE").map(PathBuf::from).or_else(|| {
        let home = var_os("HOMEPATH")?;
        Some(PathBuf::from(var_os("HOMEDRIVE").unwrap_or_default()).join(home))
    })?;
    let requested = OsString::from_wide(&component.encode_wide().skip(1).collect::<Vec<_>>());
    let current = var_os("USERNAME");
    if requested.is_empty() || current.as_ref() == Some(&requested) {
        return Some(profile);
    }
    // ntpath.basename compares literal spelling, including final dots and
    // trailing separators. Path::file_name normalizes those components.
    let units: Vec<u16> = profile.as_os_str().encode_wide().collect();
    let drive = match profile.components().next() {
        Some(Component::Prefix(prefix)) => prefix.as_os_str().encode_wide().count(),
        _ => 0,
    };
    let basename = units[drive..]
        .iter()
        .rposition(|c| matches!(*c, 47 | 92))
        .map_or(drive, |i| drive + i + 1);
    if current != Some(OsString::from_wide(&units[basename..])) {
        return None;
    }
    Some(PathBuf::from(OsString::from_wide(&units[..basename])).join(requested))
}
