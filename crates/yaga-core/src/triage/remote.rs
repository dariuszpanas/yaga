use super::{
    Snapshot, evaluate, publication,
    snapshot::{BOUNDARY, MAX_FILES, timestamp},
};
use crate::{
    gh::{Client, PullRequest},
    policies::triage::TriagePolicy,
};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    time::{Duration, Instant},
};

const MAX_REQUESTS: usize = 160;
const MAX_FILES_PAGE_STDOUT_BYTES: usize = 8 * 1024 * 1024;

pub(super) struct TriageClient {
    pub api: Client,
    requests: usize,
    deadline: Instant,
}
impl TriageClient {
    fn admit(&mut self) -> Result<(), String> {
        if self.requests >= MAX_REQUESTS || Instant::now() + Duration::from_secs(30) > self.deadline
        {
            return Err("triage API request or deadline budget exhausted".into());
        }
        self.requests += 1;
        Ok(())
    }
    pub fn get(&mut self, endpoint: &str) -> Result<Value, String> {
        self.admit()?;
        self.api.get(endpoint)
    }
    pub fn get_files_page(&mut self, endpoint: &str) -> Result<Value, String> {
        self.admit()?;
        self.api
            .get_with_stdout_limit(endpoint, MAX_FILES_PAGE_STDOUT_BYTES)
    }
    pub fn write(&mut self, endpoint: &str, method: &str, body: &str) -> Result<Value, String> {
        self.admit()?;
        self.api.write(endpoint, method, &[("body", body)])
    }
    pub fn prefix(&self) -> String {
        format!(
            "repos/{}/{}",
            self.api.reference.owner(),
            self.api.reference.name()
        )
    }
}
pub(super) fn positive(v: &Value, label: &str) -> Result<u64, String> {
    v.as_u64()
        .filter(|n| (1..=(1 << 53) - 1).contains(n))
        .ok_or_else(|| format!("invalid triage {label}"))
}
fn sha<'a>(v: &'a Value, label: &str) -> Result<&'a str, String> {
    v.as_str()
        .filter(|s| {
            s.len() == 40
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || matches!(b, b'a'..=b'f'))
        })
        .ok_or_else(|| format!("invalid triage {label}"))
}
pub(super) fn record<'a>(v: &'a Value, label: &str) -> Result<&'a Value, String> {
    if v.is_object() {
        Ok(v)
    } else {
        Err(format!("missing triage API {label}"))
    }
}
fn same_repo(a: &PullRequest, b: &PullRequest) -> bool {
    a.repository().eq_ignore_ascii_case(&b.repository())
}
fn metadata(client: &mut TriageClient) -> Result<(Value, usize), String> {
    let pr = client.get(&format!(
        "{}/pulls/{}",
        client.prefix(),
        client.api.reference.number()
    ))?;
    let reference = &client.api.reference;
    record(&pr, "PR")?;
    if pr["number"].as_i64() != Some(reference.number()) {
        return Err("triage API returned a different PR".into());
    }
    let actual = PullRequest::parse(
        pr["html_url"]
            .as_str()
            .ok_or("triage API returned a missing PR URL")?,
        None,
    )?;
    if !same_repo(&actual, reference) || actual.number() != reference.number() {
        return Err("triage API returned a different PR URL".into());
    }
    let head = record(&pr["head"], "PR head")?;
    let base = record(&pr["base"], "PR base")?;
    let head_repo = record(&head["repo"], "head repository")?;
    let base_repo = record(&base["repo"], "base repository")?;
    let author = record(&pr["user"], "PR author")?;
    let repo = format!("{}/{}", reference.owner(), reference.name());
    if !base_repo["full_name"]
        .as_str()
        .is_some_and(|s| s.eq_ignore_ascii_case(&repo))
    {
        return Err("triage base repository does not match explicit repository".into());
    }
    let count = pr["changed_files"]
        .as_u64()
        .filter(|n| *n <= MAX_FILES as u64)
        .ok_or_else(|| format!("triage file inventory exceeds its {MAX_FILES}-file bound"))?
        as usize;
    Ok((
        json!({"version":1,"host":reference.host(),"repository":repo,"repository_id":base_repo["id"],"number":pr["number"],
        "state":pr["state"],"draft":pr["draft"],"head_sha":sha(&head["sha"],"head SHA")?,"base_sha":sha(&base["sha"],"base SHA")?,"base_ref":base["ref"],
        "head_repository_id":head_repo["id"],"head_repository_fork":head_repo["fork"],"author_id":author["id"],"author_login":author["login"],"author_type":author["type"]}),
        count,
    ))
}
fn tree(client: &mut TriageClient, commit: &str) -> Result<String, String> {
    let document = client.get(&format!("{}/git/commits/{commit}", client.prefix()))?;
    let object = record(&document, "Git commit")?;
    if object["sha"] != commit {
        return Err("triage API returned a different Git commit".into());
    }
    Ok(sha(&record(&object["tree"], "Git tree")?["sha"], "tree SHA")?.into())
}
fn inventory(client: &mut TriageClient, policy: &TriagePolicy) -> Result<Value, String> {
    let (mut doc, count) = metadata(client)?;
    let number = client.api.reference.number();
    let prefix = client.prefix();
    let mut files = vec![];
    for page in 1..=count.div_ceil(100).max(1) {
        let items = client.get_files_page(&format!(
            "{prefix}/pulls/{number}/files?per_page=100&page={page}"
        ))?;
        let items = items
            .as_array()
            .filter(|a| a.len() <= 100)
            .ok_or("invalid triage file page")?;
        for item in items {
            record(item, "changed file")?;
            files.push(json!({"filename":item["filename"],"previous_filename":item["previous_filename"],"sha":item["sha"],"status":item["status"]}));
        }
    }
    let mut selected = Vec::new();
    let mut candidates = vec![];
    if policy.duplicate_candidates > 0 {
        let limit = policy.duplicate_candidates + 1;
        let closed = client.get(&format!(
            "{prefix}/pulls?state=closed&sort=updated&direction=desc&per_page={limit}"
        ))?;
        let items = closed
            .as_array()
            .filter(|a| a.len() <= limit)
            .ok_or("invalid triage closed-PR candidate inventory")?;
        let mut seen = HashSet::new();
        for c in items {
            record(c, "closed PR")?;
            let n = positive(&c["number"], "closed PR number")? as i64;
            if !seen.insert(n) {
                return Err("duplicate triage closed PR candidate".into());
            }
            let reference = PullRequest::parse(
                c["html_url"]
                    .as_str()
                    .ok_or("triage closed PR has missing identity")?,
                None,
            )?;
            if reference.number() != n
                || !same_repo(&reference, &client.api.reference)
                || c["state"] != "closed"
                || c.get("merged_at").is_none()
            {
                return Err("triage closed PR has unknown merge state".into());
            }
            let merged = if !c["merged_at"].is_null() {
                timestamp(&c["merged_at"])?;
                Some(sha(&c["merge_commit_sha"], "merged commit")?.to_owned())
            } else {
                None
            };
            if n != number && selected.len() < policy.duplicate_candidates {
                selected.push((n, merged));
            }
        }
        for (n, commit) in &selected {
            if let Some(commit) = commit {
                candidates.push(
                    json!({"number":n,"merge_commit_sha":commit,"tree_sha":tree(client,commit)?}),
                );
            }
        }
    }
    doc["head_tree"] = tree(client, doc["head_sha"].as_str().unwrap())?.into();
    doc["changed_files"] = count.into();
    doc["files"] = files.into();
    doc["candidate_limit"] = policy.duplicate_candidates.into();
    doc["candidates_examined"] = selected.len().into();
    doc["candidates"] = candidates.into();
    Ok(doc)
}
pub(super) fn fetch(client: &mut TriageClient, policy: &TriagePolicy) -> Result<Snapshot, String> {
    let doc = inventory(client, policy)?;
    let raw = client.get(&format!(
        "{}/pulls/{}/reviews?per_page=100",
        client.prefix(),
        client.api.reference.number()
    ))?;
    finish(client, policy, doc, raw)
}
fn finish(
    client: &mut TriageClient,
    policy: &TriagePolicy,
    mut doc: Value,
    raw: Value,
) -> Result<Snapshot, String> {
    let reviews = raw
        .as_array()
        .filter(|a| a.len() < 100)
        .ok_or("triage review inventory is incomplete")?;
    let mut normalized = vec![];
    for r in reviews {
        record(r, "review")?;
        let user = record(&r["user"], "review author")?;
        let commit = r["commit_id"]
            .as_str()
            .map(|s| Value::String(s.to_ascii_lowercase()))
            .unwrap_or_else(|| r["commit_id"].clone());
        normalized.push(json!({"id":r["id"],"user_id":user["id"],"user_type":user["type"],"state":r["state"],"commit_id":commit,
            "submitted_at":r["submitted_at"],"body":if r["body"].is_null(){json!("")}else{r["body"].clone()}}));
    }
    doc["reviews"] = normalized.into();
    let (current, count) = metadata(client)?;
    if doc["changed_files"] != count
        || current
            .as_object()
            .unwrap()
            .iter()
            .any(|(k, v)| doc[k] != *v)
    {
        return Err("triage PR boundary changed during collection".into());
    }
    let snapshot = Snapshot::parse(doc)?;
    evaluate(policy, &snapshot)?;
    Ok(snapshot)
}
/// Evaluate one explicitly identified live PR. Only the explicit publish flag allows a write.
pub fn check_live(
    reference: PullRequest,
    policy: &TriagePolicy,
    publish: bool,
) -> Result<Value, String> {
    let mut client = TriageClient {
        api: Client::open(reference)?,
        requests: 0,
        deadline: Instant::now() + Duration::from_secs(300),
    };
    let initial = fetch(&mut client, policy)?;
    if publish {
        publication::publish(&mut client, policy, &initial)
    } else {
        evaluate(policy, &initial)
    }
}
/// Collect review evidence and admission from the same review inventory and immutable PR head/base.
pub fn collect_for_review<T>(
    reference: PullRequest,
    policy: &TriagePolicy,
    collect: impl FnOnce(
        &mut dyn FnMut(&str) -> Result<Value, String>,
    ) -> Result<(T, Value, String, String), String>,
) -> Result<(T, Value), String> {
    let mut client = TriageClient {
        api: Client::open(reference)?,
        requests: 0,
        deadline: Instant::now() + Duration::from_secs(300),
    };
    let inventory = inventory(&mut client, policy)?;
    let (review, raw, head, base) = collect(&mut |endpoint| client.get(endpoint))?;
    let snapshot = finish(&mut client, policy, inventory, raw)?;
    if snapshot.0["head_sha"] != head || snapshot.0["base_sha"] != base {
        return Err("PR head/base changed during shared publication collection".into());
    }
    Ok((review, evaluate(policy, &snapshot)?))
}
pub(super) fn revalidate(
    client: &mut TriageClient,
    policy: &TriagePolicy,
    expected: &Snapshot,
) -> Result<(Snapshot, Value), String> {
    let current = fetch(client, policy)?;
    if BOUNDARY
        .iter()
        .any(|key| current.0[*key] != expected.0[*key])
        || current.0["files"] != expected.0["files"]
    {
        return Err("triage PR boundary changed before publication".into());
    }
    let report = evaluate(policy, &current)?;
    Ok((current, report))
}
