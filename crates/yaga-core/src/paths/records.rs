//! Decode bounded, NUL-separated output of full-tree recursive Git listings.
//! Callers own Git invocation, exact commit/tree identity and history checks.
use super::{PathComponents, PathError, leaf_topology};
use std::collections::{HashMap, HashSet};

pub const MAX_TREE_OUTPUT: usize = 64 * 1024 * 1024;
pub const MAX_ENTRIES: usize = 50_000;
pub const MAX_BLOB_BYTES: u64 = (1 << 53) - 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ModeKind {
    Regular,
    Executable,
    Symlink,
    Gitlink,
}

impl ModeKind {
    pub fn mode(self) -> &'static str {
        match self {
            Self::Regular => "100644",
            Self::Executable => "100755",
            Self::Symlink => "120000",
            Self::Gitlink => "160000",
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::Regular => "regular",
            Self::Executable => "executable",
            Self::Symlink => "symlink",
            Self::Gitlink => "gitlink",
        }
    }
    pub fn object_type(self) -> &'static str {
        if self == Self::Gitlink {
            "commit"
        } else {
            "blob"
        }
    }
    fn from_pair(mode: &str, object_type: &str) -> Result<Self> {
        let kind = match mode {
            "100644" => Self::Regular,
            "100755" => Self::Executable,
            "120000" => Self::Symlink,
            "160000" => Self::Gitlink,
            _ => {
                return Err(PathError::Selection(
                    "git returned an unsupported tree-entry mode",
                ));
            }
        };
        if kind.object_type() != object_type {
            return Err(PathError::Selection(
                "git returned an incompatible tree-entry mode and type",
            ));
        }
        Ok(kind)
    }
}

#[derive(Debug)]
pub struct Entry<'a> {
    pub path: &'a str,
    pub oid: &'a str,
    pub kind: ModeKind,
}

#[derive(Debug)]
pub struct ModeSelection<'a>(Vec<Entry<'a>>);

#[derive(Debug)]
pub struct SizeSelection<'a> {
    blobs: Vec<(Entry<'a>, u64)>,
    gitlinks: Vec<&'a str>,
}

type Result<T> = std::result::Result<T, PathError>;

fn records<'a>(
    output: &'a [u8],
    label: &str,
    count_error: &'static str,
) -> Result<impl Iterator<Item = &'a [u8]>> {
    if output.len() > MAX_TREE_OUTPUT {
        return Err(PathError::Selection("committed tree output exceeds 64 MiB"));
    }
    if !output.is_empty() && output.last() != Some(&0) {
        return Err(PathError::Configuration(format!(
            "git returned an incomplete {label} record"
        )));
    }
    let count = output.iter().filter(|&&b| b == 0).count();
    if count > MAX_ENTRIES {
        return Err(PathError::Selection(count_error));
    }
    Ok(output.split(|&b| b == 0).take(count))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NameSelection {
    Tree,
    Portability,
}

fn path_value<'a>(
    raw: &'a [u8],
    portable: bool,
    invalid: &'static str,
    encoding: &'static str,
) -> Result<&'a str> {
    if raw.len() > super::MAX_PATH_BYTES
        || raw.iter().filter(|&&b| b == b'/').count() >= super::MAX_COMPONENTS
    {
        return Err(PathError::Selection(invalid));
    }
    let path = std::str::from_utf8(raw).map_err(|_| PathError::Selection(encoding))?;
    if portable {
        PathComponents::parse(path)
    } else {
        PathComponents::committed(path)
    }
    .map_err(|_| PathError::Selection(invalid))?;
    Ok(path)
}
const INVALID_TREE_PATH: &str = "git returned a non-canonical or unsafe repository-relative path";
const TREE_ENCODING: &str = "git tree path is not valid UTF-8";

/// Read NUL-delimited Git names in their provider's exact path grammar.
pub fn parse_names(output: &[u8], kind: NameSelection) -> Result<Vec<&str>> {
    let (label, cap, empty, invalid, encoding, duplicate) = match kind {
        NameSelection::Tree => (
            "tree-path",
            "tree paths exceed the hard 50000-path limit",
            "git returned an empty tree path",
            INVALID_TREE_PATH,
            TREE_ENCODING,
            "git returned a duplicate tree path",
        ),
        NameSelection::Portability => (
            "committed-path",
            "committed paths exceed the hard 50000-entry limit",
            "git returned an empty committed path",
            "git returned an unsafe repository-relative committed path",
            "git committed path is not valid UTF-8",
            "git returned a duplicate committed path",
        ),
    };
    let mut seen = HashSet::new();
    for raw in records(output, label, cap)? {
        if raw.is_empty() {
            return Err(PathError::Selection(empty));
        }
        let path = path_value(raw, kind == NameSelection::Tree, invalid, encoding)?;
        if !seen.insert(path) {
            return Err(PathError::Selection(duplicate));
        }
    }
    let mut paths: Vec<_> = seen.into_iter().collect();
    paths.sort_unstable();
    if kind == NameSelection::Portability {
        leaf_topology(&paths).map_err(|_| {
            PathError::Selection("git returned an impossible committed-path leaf topology")
        })?
    }
    Ok(paths)
}

fn blob_size(raw: &[u8], allow_unavailable: bool) -> Result<Option<u64>> {
    if raw == b"-" && allow_unavailable {
        return Ok(None);
    }
    if raw.len() > 16
        || raw.is_empty()
        || raw.len() > 1 && raw[0] == b'0'
        || !raw.iter().all(u8::is_ascii_digit)
    {
        return Err(PathError::Selection(
            "git returned a non-canonical tree-entry size",
        ));
    }
    let value = raw.iter().fold(0u64, |n, &b| n * 10 + u64::from(b - b'0'));
    if value > MAX_BLOB_BYTES {
        return Err(PathError::Selection(
            "git returned a tree-entry size above the hard 9007199254740991-byte limit",
        ));
    }
    Ok(Some(value))
}

fn entries<'a>(
    output: &'a [u8],
    oid_length: usize,
    with_size: bool,
) -> Result<Vec<(Entry<'a>, Option<u64>)>> {
    if !matches!(oid_length, 40 | 64) {
        return Err(PathError::Selection(
            "git returned an unsupported object identity length",
        ));
    }
    let label = if with_size {
        "blob-size"
    } else {
        "committed-mode"
    };
    let mut entries = Vec::new();
    let mut seen = HashSet::new();
    for raw in records(
        output,
        label,
        "tree entries exceed the hard 50000-entry limit",
    )? {
        if raw.is_empty() {
            return Err(PathError::Configuration(format!(
                "git returned an empty {label} record"
            )));
        }
        let tab = raw
            .iter()
            .position(|&b| b == b'\t')
            .filter(|&i| i + 1 < raw.len())
            .ok_or_else(|| {
                PathError::Configuration(format!("git returned a malformed {label} record"))
            })?;
        let mut fields = raw[..tab].split(|&b| b == b' ');
        let malformed =
            || PathError::Configuration(format!("git returned malformed {label} metadata"));
        let mut next = || {
            fields
                .next()
                .filter(|s| !s.is_empty())
                .ok_or_else(malformed)
        };
        let (raw_mode, raw_type, raw_oid) = (next()?, next()?, next()?);
        let size_bytes = if with_size { Some(next()?) } else { None };
        if fields.next().is_some() {
            return Err(malformed());
        }
        let ascii = |raw: &'a [u8]| {
            if raw.is_ascii() {
                Ok(std::str::from_utf8(raw).expect("ASCII is valid UTF-8"))
            } else {
                Err(PathError::Configuration(format!(
                    "git returned non-ASCII {label} metadata"
                )))
            }
        };
        let (mode, object_type, oid) = (ascii(raw_mode)?, ascii(raw_type)?, ascii(raw_oid)?);
        let kind = if with_size {
            None
        } else {
            Some(ModeKind::from_pair(mode, object_type)?)
        };
        if oid.len() != oid_length
            || !oid
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(PathError::Selection(
                "git returned a malformed or mixed-format tree-entry identity",
            ));
        }
        let path = path_value(&raw[tab + 1..], with_size, INVALID_TREE_PATH, TREE_ENCODING)?;
        if !seen.insert(path) {
            return Err(PathError::Selection("git returned a duplicate tree path"));
        }
        let kind = match kind {
            Some(k) => k,
            None => ModeKind::from_pair(mode, object_type)?,
        };
        let size = size_bytes
            .map(|b| blob_size(b, kind == ModeKind::Gitlink))
            .transpose()?
            .flatten();
        entries.push((Entry { path, oid, kind }, size));
    }
    entries.sort_unstable_by_key(|(entry, _)| entry.path);
    Ok(entries)
}

/// Decode raw sized records. Use SizeSelection::parse to enforce consistency
/// across object identities and check directory/file topology before evaluation.
pub fn parse_size_records(
    output: &[u8],
    oid_length: usize,
) -> Result<Vec<(Entry<'_>, Option<u64>)>> {
    entries(output, oid_length, true)
}

impl<'a> ModeSelection<'a> {
    /// Read `mode type oid<TAB>path<NUL>` records with an independent expected object-ID length.
    pub fn parse(output: &'a [u8], oid_length: usize) -> Result<Self> {
        let entries = entries(output, oid_length, false)?;
        leaf_topology(&entries.iter().map(|(e, _)| e.path).collect::<Vec<_>>()).map_err(|_| {
            PathError::Selection("git returned an inconsistent committed-mode selection")
        })?;
        Ok(Self(entries.into_iter().map(|(entry, _)| entry).collect()))
    }
    pub fn entries(&self) -> &[Entry<'a>] {
        &self.0
    }
}

impl<'a> SizeSelection<'a> {
    /// Read `mode type oid size<TAB>path<NUL>` records with an independent expected object-ID length.
    pub fn parse(output: &'a [u8], oid_length: usize) -> Result<Self> {
        let mut blobs = Vec::new();
        let mut gitlinks = Vec::new();
        let mut sizes_by_oid = HashMap::new();
        let entries = parse_size_records(output, oid_length)?;
        leaf_topology(&entries.iter().map(|(e, _)| e.path).collect::<Vec<_>>()).map_err(|_| {
            PathError::Selection("git returned an inconsistent committed-tree blob selection")
        })?;
        for (entry, size) in entries {
            if entry.kind == ModeKind::Gitlink {
                gitlinks.push(entry.path);
            } else {
                let size = size.expect("size parser requires an integer for blobs");
                if sizes_by_oid
                    .insert(entry.oid, size)
                    .is_some_and(|prior| prior != size)
                {
                    return Err(PathError::Selection(
                        "git returned an inconsistent committed-tree blob selection",
                    ));
                }
                blobs.push((entry, size));
            }
        }
        Ok(Self { blobs, gitlinks })
    }
    pub fn blobs(&self) -> &[(Entry<'a>, u64)] {
        &self.blobs
    }
    pub fn gitlinks(&self) -> &[&'a str] {
        &self.gitlinks
    }
}
