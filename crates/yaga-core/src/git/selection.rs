use super::{GitError, Repository, parse_object_id, safe_git_error};
use crate::paths::{PathComponents, records::MAX_TREE_OUTPUT};
use crate::text::{python_repr, safe_text};
use regex::Regex;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

static UNSAFE_REVISION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[\s\p{Cc}\p{Cf}]").unwrap());
fn valid_token(rev: &str) -> bool {
    !rev.is_empty()
        && rev.len() <= 4 * 512
        && rev.chars().count() <= 512
        && !rev.starts_with('-')
        && !UNSAFE_REVISION.is_match(rev)
}
fn commitish(rev: &str) -> bool {
    valid_token(rev)
        && !rev.starts_with('^')
        && !["..", ":", "^@", "^!", "^-"]
            .iter()
            .any(|token| rev.contains(token))
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum GitTreeKind {
    Tree,
    Path,
    Mode,
    Size,
}
impl GitTreeKind {
    fn grafts(self) -> &'static str {
        match self {
            Self::Tree => "committed-tree selection rejects legacy graft overlays",
            Self::Path => "committed-path selection rejects legacy graft overlays",
            Self::Mode => "committed-mode selection rejects legacy graft overlays",
            Self::Size => "committed blob-size selection rejects legacy graft overlays",
        }
    }
    fn label(self) -> &'static str {
        match self {
            Self::Tree => "tree",
            Self::Path => "path",
            Self::Mode => "mode",
            Self::Size => "size",
        }
    }
}

#[derive(Debug)]
pub struct CommittedTreeIdentity {
    repo: Repository,
    revision: String,
    commit: String,
    tree: String,
}
impl CommittedTreeIdentity {
    pub fn repository(&self) -> &Path {
        self.repo.directory()
    }
    pub fn revision(&self) -> &str {
        &self.revision
    }
    pub fn commit_sha(&self) -> &str {
        &self.commit
    }
    pub fn tree_sha(&self) -> &str {
        &self.tree
    }

    pub fn enumerate(&self, kind: GitTreeKind) -> Result<Vec<u8>, GitError> {
        self.repo.require_no_legacy_grafts(kind.grafts())?;
        let (format, failure, warning) = match kind {
            GitTreeKind::Tree => (
                "--name-only",
                "git could not read the selected commit tree",
                "git returned unexpected error output while reading the commit tree",
            ),
            GitTreeKind::Path => (
                "--name-only",
                "git could not read the selected commit paths",
                "git returned unexpected error output while reading committed paths",
            ),
            GitTreeKind::Size => (
                "--format=%(objectmode)%x20%(objecttype)%x20%(objectname)%x20%(objectsize)%x09%(path)",
                "git could not read blob sizes from the selected commit tree",
                "git returned unexpected error output while reading blob sizes",
            ),
            GitTreeKind::Mode => (
                "--format=%(objectmode)%x20%(objecttype)%x20%(objectname)%x09%(path)",
                "git could not read modes from the selected commit tree",
                "git returned unexpected error output while reading committed modes",
            ),
        };
        let result = self.repo.checked(
            &[
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                format,
                &self.tree,
                "--",
            ],
            MAX_TREE_OUTPUT,
            failure,
            Some(warning),
        )?;
        self.repo.require_no_legacy_grafts(kind.grafts())?;
        Ok(result)
    }
}

impl Repository {
    fn checked(
        &self,
        args: &[&str],
        limit: usize,
        failure: &str,
        warning: Option<&str>,
    ) -> Result<Vec<u8>, GitError> {
        let result = self.run(args, limit)?;
        if !result.status.success() {
            return Err(GitError::Git(format!(
                "{failure}: {}",
                safe_git_error(&result.stderr)
            )));
        }
        if let Some(message) = warning
            && !result.stderr.is_empty()
        {
            return Err(GitError::Git(message.into()));
        }
        Ok(result.stdout)
    }

    fn object(
        &self,
        revision: &str,
        kind: &str,
        failure: &str,
        warning: Option<&str>,
    ) -> Result<String, GitError> {
        let selector = format!("{revision}^{{{kind}}}");
        let bytes = self.checked(
            &["rev-parse", "--verify", "--end-of-options", &selector],
            256,
            failure,
            warning,
        )?;
        parse_object_id(&bytes, kind)
    }
}

pub fn resolve_committed_tree(
    repo: &Path,
    revision: &str,
    kind: GitTreeKind,
) -> Result<CommittedTreeIdentity, GitError> {
    if !commitish(revision) {
        return Err(GitError::Input(format!(
            "{} revision must be one bounded, non-option commit-ish of at most 512 characters",
            kind.label()
        )));
    }
    resolve_identity(repo, revision, kind.grafts())
}

pub fn resolve_repository_tree(
    repo: &Path,
    revision: &str,
) -> Result<CommittedTreeIdentity, GitError> {
    if !commitish(revision) {
        return Err(GitError::Input(
            "revision must be one bounded, non-option commit-ish of at most 512 characters".into(),
        ));
    }
    resolve_identity(
        repo,
        revision,
        "repository committed-tree selection rejects legacy graft overlays",
    )
}

fn resolve_identity(
    repo: &Path,
    revision: &str,
    grafts: &str,
) -> Result<CommittedTreeIdentity, GitError> {
    let repo = Repository::open(repo)?;
    repo.require_no_legacy_grafts(grafts)?;
    let label = python_repr(&safe_text(revision, 160));
    let commit = repo.object(
        revision,
        "commit",
        &format!("git could not resolve {label} as one commit"),
        Some("git returned unexpected error output while resolving commit"),
    )?;
    let tree = repo.object(
        &commit,
        "tree",
        "git could not resolve the selected commit tree",
        Some("git returned unexpected error output while resolving tree"),
    )?;
    if tree.len() != commit.len() {
        return Err(GitError::Git(
            "git returned inconsistent object identity lengths".into(),
        ));
    }
    repo.require_no_legacy_grafts(grafts)?;
    Ok(CommittedTreeIdentity {
        repo,
        revision: revision.into(),
        commit,
        tree,
    })
}

#[derive(Debug)]
pub struct ChangedPaths {
    pub repository: PathBuf,
    pub base_sha: String,
    pub head_sha: String,
    pub comparison_sha: String,
    pub paths: Vec<String>,
}

fn split_range(range: &str) -> Result<(&str, bool, &str), GitError> {
    if !valid_token(range) {
        return Err(GitError::Git(
            "revision range must be one bounded, non-option Git expression".into(),
        ));
    }
    let (base, tail) = range.split_once("..").ok_or_else(|| {
        GitError::Git("changed paths require an explicit non-empty A..B or A...B range".into())
    })?;
    let three_dot = tail.starts_with('.');
    let head = tail.strip_prefix('.').unwrap_or(tail);
    if base.is_empty() || head.is_empty() {
        return Err(GitError::Git(
            "changed paths require an explicit non-empty A..B or A...B range".into(),
        ));
    }
    if base.ends_with('.') || head.starts_with('.') || head.contains("..") {
        return Err(GitError::Git(
            "changed paths require exactly one two-dot or three-dot separator".into(),
        ));
    }
    if base.contains(':') || head.contains(':') {
        return Err(GitError::Git(
            "changed-path range endpoints must not contain object-path selectors".into(),
        ));
    }
    if !commitish(base) || !commitish(head) {
        return Err(GitError::Git(
            "changed-path range endpoints must each name exactly one commit-ish".into(),
        ));
    }
    Ok((base, three_dot, head))
}

pub fn read_changed_paths(
    repo: &Path,
    range: &str,
    strict_output: bool,
) -> Result<ChangedPaths, GitError> {
    let (base, three_dot, head) = split_range(range)?;
    let repo = Repository::open(repo)?;
    let shallow = "changed paths require a non-shallow repository with complete history";
    let grafts = "changed paths require complete history without legacy graft overlays";
    repo.require_complete_history(shallow, grafts)?;
    let endpoint = |revision: &str, label: &str| {
        repo.object(
            revision,
            "commit",
            &format!(
                "git could not resolve range {label} {} as one commit",
                python_repr(&safe_text(revision, 160))
            ),
            strict_output
                .then_some("git returned unexpected error output while resolving a range endpoint"),
        )
    };
    let base_sha = endpoint(base, "base")?;
    let head_sha = endpoint(head, "head")?;
    if base_sha.len() != head_sha.len() {
        return Err(GitError::Git(
            "git returned inconsistent commit identity lengths".into(),
        ));
    }
    let comparison_sha = if three_dot {
        let bytes = repo.checked(
            &["merge-base", "--all", &base_sha, &head_sha],
            4096,
            "git could not determine one merge base",
            strict_output
                .then_some("git returned unexpected error output while resolving the merge base"),
        )?;
        if bytes.split_inclusive(|&b| b == b'\n').count() > 1 {
            return Err(GitError::Git(
                "three-dot changed paths require exactly one merge base".into(),
            ));
        }
        let oid = parse_object_id(&bytes, "merge-base")?;
        if oid.len() != base_sha.len() {
            return Err(GitError::Git(
                "git returned an inconsistent merge-base identity length".into(),
            ));
        }
        oid
    } else {
        base_sha.clone()
    };
    let bytes = repo.checked(
        &[
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-relative",
            "--ignore-submodules=none",
            &comparison_sha,
            &head_sha,
            "--",
        ],
        4 * 1024 * 1024,
        "git could not read changed paths",
        strict_output.then_some("git returned unexpected error output while reading changed paths"),
    )?;
    let paths = parse_changed_paths(&bytes)?;
    repo.require_complete_history(shallow, grafts)?;
    Ok(ChangedPaths {
        repository: repo.directory().to_owned(),
        base_sha,
        head_sha,
        comparison_sha,
        paths,
    })
}

pub fn parse_changed_paths(bytes: &[u8]) -> Result<Vec<String>, GitError> {
    if bytes.len() > 4 * 1024 * 1024 {
        return Err(GitError::Git(
            "git output exceeds the hard 4194304-byte limit".into(),
        ));
    }
    if bytes.is_empty() {
        return Ok(Vec::new());
    }
    let body = bytes
        .strip_suffix(&[0])
        .ok_or_else(|| GitError::Git("git returned an incomplete changed-path record".into()))?;
    let mut paths = Vec::new();
    if bytes.iter().filter(|&&b| b == 0).count() > 2048 {
        return Err(GitError::Git(
            "changed paths exceed the hard 2048-path limit".into(),
        ));
    }
    for piece in body.split(|&b| b == 0) {
        if piece.is_empty() {
            return Err(GitError::Git("git returned an empty changed path".into()));
        }
        let path = std::str::from_utf8(piece)
            .map_err(|_| GitError::Git("git changed path is not valid UTF-8".into()))?;
        PathComponents::parse(path).map_err(|_| {
            GitError::Git("git returned a non-canonical or unsafe repository-relative path".into())
        })?;
        paths.push(path.to_owned());
    }
    paths.sort_unstable();
    if paths.windows(2).any(|w| w[0] == w[1]) {
        return Err(GitError::Git(
            "git returned a duplicate changed path".into(),
        ));
    }
    Ok(paths)
}
