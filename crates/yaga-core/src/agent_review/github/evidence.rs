//! Validate exact-head connector evidence for the privileged lifecycle publisher.
use super::{api::RestApi, *};
use std::collections::{HashMap, HashSet};
static CLEAN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r###"\AAgent Review: Didn't find any major issues\.(?: (?P<flourish>[^\r\n]{1,80}))?\n\n\*\*Reviewed commit:\*\* `(?P<commit>[0-9a-f]{10,40})`(?P<footer>[\s\S]*)\z"###).unwrap()
});
static FOOTER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r###"\A\n\n<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n(?:\[Your team has set up Codex to review pull requests in this repo\]\(https://chatgpt\.com/codex/cloud/settings/general\)\. Reviews are triggered when you\n|Codex has been enabled to automatically review pull requests in this repo\. Reviews are triggered when you\n)- Open a pull request for review\n- Mark a draft as ready\n- Comment "@codex review"\.\n\nIf Codex has suggestions, it will comment; otherwise it will react with 👍\.\n\s*(?:Codex can also answer questions or update the PR\. Try commenting "@codex address that feedback"\.|When you \[sign up for Codex through ChatGPT\]\(https://openai\.com/codex\), Codex can also answer questions or update the PR, like "@codex address that feedback"\.)\s*</details>\n?\z"###).unwrap()
});
static MARKER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r###"(?m)^\*\*Reviewed commit:\*\* `(?P<commit>[0-9a-f]{10,40})`\s*$"###).unwrap()
});
static INVALID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{C}\p{Zl}\p{Zp}]").unwrap());
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CodexOutcome {
    pub occurred_at: i64,
    pub database_id: i64,
    pub kind: String,
    pub reviewed_commit: String,
}
fn reviewed(v: &Value, formal: bool) -> Option<String> {
    let s = v
        .as_str()
        .filter(|v| v.len() <= 262144)?
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    let mut markers = MARKER.captures_iter(&s);
    let matched = markers.next()?.name("commit")?.as_str().to_owned();
    if markers.next().is_some() {
        return None;
    }
    if formal {
        return s
            .trim_start_matches('\n')
            .starts_with(FORMAL_PREFIX)
            .then_some(matched);
    }
    let m = CLEAN.captures(&s)?;
    if m.name("flourish").is_some_and(|f| {
        let v = f.as_str();
        v.len() > 80 || v.trim() != v || INVALID.is_match(v)
    }) {
        return None;
    }
    let footer = &m["footer"];
    (m["commit"] == matched && (matches!(footer, "" | "\n") || FOOTER.is_match(footer)))
        .then_some(matched)
}
const FORMAL_PREFIX: &str = r###"### 💡 Agent Review

Here are some automated review suggestions for this pull request."###;
fn actor(v: &Value) -> bool {
    actor_is(v, CODEX_CONNECTOR_USER_ID, CODEX_CONNECTOR_LOGIN)
}
fn connector_comment(v: &Value) -> bool {
    actor(v)
        && v["performed_via_github_app"]["id"].as_i64() == Some(CODEX_CONNECTOR_APP_ID)
        && v["performed_via_github_app"]["slug"] == CODEX_CONNECTOR_APP_SLUG
}
fn resolves(
    api: &mut dyn RestApi,
    repo: &str,
    prefix: &str,
    head: &str,
    cache: &mut HashMap<String, bool>,
) -> Result<bool, String> {
    if !head.starts_with(prefix) {
        return Ok(false);
    }
    if prefix.len() == 40 {
        return Ok(prefix == head);
    }
    if let Some(&matched) = cache.get(prefix) {
        return Ok(matched);
    }
    let commit = api.get(&format!("/repos/{repo}/commits/{prefix}"))?;
    let matched = sha(
        &record(&commit, "Agent reviewed commit")?["sha"],
        "Agent reviewed commit",
    )? == head;
    cache.insert(prefix.into(), matched);
    Ok(matched)
}
fn valid_content(value: &Value) -> bool {
    matches!(
        value.as_str(),
        Some("+1" | "-1" | "confused" | "eyes" | "heart" | "hooray" | "laugh" | "rocket")
    )
}
fn reaction_threshold(not_before: i64, threshold: Option<i64>) -> Result<i64, String> {
    let t = threshold.unwrap_or(not_before);
    if t < not_before {
        Err("Codex reaction boundary predates the candidate boundary".into())
    } else {
        Ok(t)
    }
}
pub fn has_codex_pending_reaction(
    api: &mut dyn RestApi,
    repository: &str,
    pr: &PullRequest,
    not_before: i64,
) -> Result<bool, String> {
    let items = api.paginate(&format!(
        "/repos/{repository}/issues/{}/reactions",
        pr.number
    ))?;
    let mut seen = HashSet::new();
    let mut pending = false;
    for (i, v) in items.iter().enumerate() {
        record(v, &format!("Codex pending reaction {i}"))?;
        let id = positive(&v["id"], "Codex pending reaction ID")?;
        if !seen.insert(id) {
            return Err("Codex pending reaction ID is repeated".into());
        }
        if !actor(v) {
            continue;
        }
        if !valid_content(&v["content"]) {
            return Err("Codex pending reaction content is invalid".into());
        }
        if v["content"] == "eyes"
            && timestamp(&v["created_at"], "Codex pending reaction creation")? >= not_before
        {
            pending = true;
        }
    }
    Ok(pending)
}
pub fn select_codex_outcome(
    api: &mut dyn RestApi,
    repository: &str,
    pr: &PullRequest,
    not_before: i64,
    allow_clean_reaction: bool,
    clean_reaction_not_before: Option<i64>,
) -> Result<Option<CodexOutcome>, String> {
    let mut outcomes = vec![];
    let mut seen = HashSet::new();
    let mut cache = HashMap::new();
    for (i, c) in api
        .paginate(&format!(
            "/repos/{repository}/issues/{}/comments",
            pr.number
        ))?
        .iter()
        .enumerate()
    {
        record(c, &format!("Codex outcome comment {i}"))?;
        if !connector_comment(c) {
            continue;
        }
        let id = positive(&c["id"], "Codex outcome comment ID")?;
        if !seen.insert(id) {
            return Err("Codex outcome comment ID is repeated".into());
        }
        let created = timestamp(&c["created_at"], "Agent review outcome comment creation")?;
        if timestamp(&c["updated_at"], "Agent review outcome comment update")? < created {
            return Err("Agent review outcome comment update predates its creation".into());
        }
        if let Some(prefix) = reviewed(&c["body"], false)
            && created > not_before
            && resolves(api, repository, &prefix, &pr.head_sha, &mut cache)?
        {
            outcomes.push(CodexOutcome {
                occurred_at: created,
                database_id: id,
                kind: "clean comment".into(),
                reviewed_commit: prefix,
            });
        }
    }
    seen.clear();
    for (i, r) in api
        .paginate(&format!("/repos/{repository}/pulls/{}/reviews", pr.number))?
        .iter()
        .enumerate()
    {
        record(r, &format!("Agent review outcome {i}"))?;
        if !actor(r) {
            continue;
        }
        let id = positive(&r["id"], "Agent review outcome ID")?;
        if !seen.insert(id) {
            return Err("Agent review outcome ID is repeated".into());
        }
        if !matches!(
            r["state"].as_str(),
            Some("APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "DISMISSED" | "PENDING")
        ) {
            return Err("Agent review outcome state is invalid".into());
        }
        let at = timestamp(&r["submitted_at"], "Agent review outcome submission")?;
        let commit = sha(&r["commit_id"], "Agent review outcome commit")?;
        if let Some(prefix) = reviewed(&r["body"], true)
            && r["state"] == "COMMENTED"
            && commit == pr.head_sha
            && at > not_before
            && resolves(api, repository, &prefix, &pr.head_sha, &mut cache)?
        {
            outcomes.push(CodexOutcome {
                occurred_at: at,
                database_id: id,
                kind: "findings review".into(),
                reviewed_commit: prefix,
            });
        }
    }
    if allow_clean_reaction {
        let threshold = reaction_threshold(not_before, clean_reaction_not_before)?;
        seen.clear();
        for (i, r) in api
            .paginate(&format!(
                "/repos/{repository}/issues/{}/reactions",
                pr.number
            ))?
            .iter()
            .enumerate()
        {
            record(r, &format!("Agent review reaction {i}"))?;
            if !actor(r) {
                continue;
            }
            let id = positive(&r["id"], "Agent review reaction ID")?;
            if !seen.insert(id) {
                return Err("Agent review reaction ID is repeated".into());
            }
            if !valid_content(&r["content"]) {
                return Err("Agent review reaction content is invalid".into());
            }
            if r["content"] == "+1" {
                let at = timestamp(&r["created_at"], "Agent review reaction creation")?;
                if at >= threshold {
                    outcomes.push(CodexOutcome {
                        occurred_at: at,
                        database_id: id,
                        kind: "clean automatic reaction".into(),
                        reviewed_commit: pr.head_sha.clone(),
                    });
                }
            }
        }
    }
    Ok(outcomes.into_iter().max_by(|a, b| {
        (a.occurred_at, a.database_id, &a.kind).cmp(&(b.occurred_at, b.database_id, &b.kind))
    }))
}
pub fn validate_codex_outcome(
    api: &mut dyn RestApi,
    repository: &str,
    pr: &PullRequest,
    not_before: i64,
    allow_clean_reaction: bool,
    outcome: &CodexOutcome,
    clean_reaction_not_before: Option<i64>,
) -> Result<bool, String> {
    let id = outcome.database_id;
    let mut cache = HashMap::new();
    match outcome.kind.as_str() {
        "clean comment" => {
            let v = api.get(&format!("/repos/{repository}/issues/comments/{id}"))?;
            record(&v, "current Codex outcome comment")?;
            if positive(&v["id"], "current Codex outcome comment ID")? != id
                || !connector_comment(&v)
            {
                return Ok(false);
            }
            let at = timestamp(&v["created_at"], "current Codex outcome creation")?;
            let updated = timestamp(&v["updated_at"], "current Codex outcome update")?;
            Ok(at == outcome.occurred_at
                && updated >= at
                && at > not_before
                && reviewed(&v["body"], false).as_ref() == Some(&outcome.reviewed_commit)
                && resolves(
                    api,
                    repository,
                    &outcome.reviewed_commit,
                    &pr.head_sha,
                    &mut cache,
                )?)
        }
        "findings review" => {
            let v = api.get(&format!(
                "/repos/{repository}/pulls/{}/reviews/{id}",
                pr.number
            ))?;
            record(&v, "current Codex outcome review")?;
            if positive(&v["id"], "current Codex outcome review ID")? != id || !actor(&v) {
                return Ok(false);
            }
            let at = timestamp(&v["submitted_at"], "current Codex outcome submission")?;
            Ok(v["state"] == "COMMENTED"
                && sha(&v["commit_id"], "current Codex outcome commit")? == pr.head_sha
                && at == outcome.occurred_at
                && at > not_before
                && reviewed(&v["body"], true).as_ref() == Some(&outcome.reviewed_commit)
                && resolves(
                    api,
                    repository,
                    &outcome.reviewed_commit,
                    &pr.head_sha,
                    &mut cache,
                )?)
        }
        "clean automatic reaction" => {
            if !allow_clean_reaction || outcome.reviewed_commit != pr.head_sha {
                return Ok(false);
            }
            let threshold = reaction_threshold(not_before, clean_reaction_not_before)?;
            let mut observed = None;
            for (i, v) in api
                .paginate(&format!(
                    "/repos/{repository}/issues/{}/reactions",
                    pr.number
                ))?
                .into_iter()
                .enumerate()
            {
                record(&v, &format!("current Codex outcome reaction {i}"))?;
                if positive(&v["id"], "current Codex outcome reaction ID")? == id
                    && observed.replace(v).is_some()
                {
                    return Ok(false);
                }
            }
            let Some(v) = observed else {
                return Ok(false);
            };
            Ok(actor(&v)
                && v["content"] == "+1"
                && timestamp(&v["created_at"], "current Codex outcome reaction creation")?
                    == outcome.occurred_at
                && outcome.occurred_at >= threshold)
        }
        _ => Err("Codex outcome capability kind is invalid".into()),
    }
}
