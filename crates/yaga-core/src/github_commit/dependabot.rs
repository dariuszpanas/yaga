use super::{PullRequestEvent, signature};
use crate::commit_source::CommitTarget;
use crate::git::{Repository, parse_object_id};
use regex::Regex;
use std::collections::HashSet;
use std::sync::LazyLock;

fn field<'a>(raw: &'a [u8], unsafe_text: &mut impl FnMut(&str) -> bool) -> Result<&'a str, String> {
    if raw.len() > 256 {
        return Err("commit metadata identity exceeds the hard byte limit".into());
    }
    let text =
        std::str::from_utf8(raw).map_err(|_| "commit metadata identity is not valid UTF-8")?;
    let unsafe_field = if text.is_ascii() {
        text.bytes().any(|b| b.is_ascii_control())
    } else {
        unsafe_text(text)
    };
    if unsafe_field {
        return Err("commit metadata identity contains unsupported control characters".into());
    }
    Ok(text)
}
fn email(value: &str) -> bool {
    matches!(
        value,
        "49699333+dependabot[bot]@users.noreply.github.com"
            | "dependabot[bot]@users.noreply.github.com"
    )
}
pub(super) fn filter(
    repo: &Repository,
    event: &PullRequestEvent,
    targets: &[CommitTarget],
    require_signature: bool,
) -> Result<HashSet<String>, String> {
    let expected = targets
        .iter()
        .filter_map(|t| t.sha.as_deref())
        .collect::<HashSet<_>>();
    if targets.is_empty() || expected.len() != targets.len() {
        return Err("Dependabot filter requires a complete unique commit selection".into());
    }
    let history = || {
        repo.require_complete_history(
            "Dependabot filter requires non-shallow history",
            "Dependabot filter requires history without legacy graft overlays",
        )
        .map_err(|e| e.to_string())
    };
    history()?;
    let output = repo
        .run(
            &[
                "log",
                "--no-show-signature",
                "--encoding=UTF-8",
                "--format=%H%x00%an%x00%ae%x00%cn%x00%ce",
                "-z",
                &format!("--max-count={}", targets.len() + 1),
                &format!("{}..{}", event.base_sha, event.head_sha),
                "--",
            ],
            16 * 1024 * 1024,
        )
        .map_err(|e| e.to_string())?;
    history()?;
    if !output.status.success() {
        return Err("git could not read the complete commit metadata selection".into());
    }
    let candidates = candidates(&output.stdout, expected)?;
    Ok(if require_signature && !candidates.is_empty() {
        signature::verified(repo, candidates)
    } else {
        candidates.into_iter().collect()
    })
}

/// Select eligible metadata only after proving the complete expected SHA set.
pub fn candidates(output: &[u8], expected: HashSet<&str>) -> Result<Vec<String>, String> {
    static UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Cf}]").unwrap());
    candidates_with_unicode(output, expected, |text| UNSAFE.is_match(text))
}

/// Allow a language adapter to use its Unicode tables when classifying identity fields.
pub fn candidates_with_unicode(
    output: &[u8],
    mut expected: HashSet<&str>,
    mut unsafe_text: impl FnMut(&str) -> bool,
) -> Result<Vec<String>, String> {
    if output.len() > 16 * 1024 * 1024 {
        return Err("git returned too much commit metadata".into());
    }
    let data = output
        .strip_suffix(&[0])
        .ok_or("git returned incomplete commit metadata")?;
    if !data.split(|&c| c == 0).count().is_multiple_of(5) {
        return Err("git returned incomplete commit metadata".into());
    }
    let mut fields = data.split(|&c| c == 0);
    let mut seen = HashSet::new();
    let mut candidates = Vec::new();
    let mut extra = false;
    while let Some(raw) = fields.next() {
        let mut next = || {
            fields
                .next()
                .ok_or("git returned incomplete commit metadata")
        };
        let (author, email_raw, committer, committer_email) = (next()?, next()?, next()?, next()?);
        let sha = parse_object_id(raw, "commit metadata SHA").map_err(|e| e.to_string())?;
        if !seen.insert(sha.clone()) {
            return Err("git returned duplicate commit metadata".into());
        }
        let (author, address, committer, committer_email) = (
            field(author, &mut unsafe_text)?,
            field(email_raw, &mut unsafe_text)?,
            field(committer, &mut unsafe_text)?,
            field(committer_email, &mut unsafe_text)?,
        );
        extra |= !expected.remove(sha.as_str());
        if author == "dependabot[bot]"
            && email(address)
            && ((committer == "GitHub" && committer_email == "noreply@github.com")
                || (committer == "dependabot[bot]" && email(committer_email)))
        {
            candidates.push(sha)
        }
    }
    if extra || !expected.is_empty() {
        return Err("commit metadata does not match the complete selected SHA set".into());
    }
    Ok(candidates)
}
