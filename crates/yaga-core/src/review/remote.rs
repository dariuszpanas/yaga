//! Collect live evidence using the caller's bounded authenticated transport.
use super::{Snapshot, snapshot::hex};
use crate::gh::{Client, PullRequest};
use serde_json::{Value, json};

fn lower(v: &Value) -> Value {
    v.as_str()
        .map_or_else(|| v.clone(), |s| json!(s.to_ascii_lowercase()))
}
fn check_runs(v: &Value) -> Result<Vec<Value>, String> {
    if !v.is_object() {
        return Err("gh returned a non-object commit check-runs payload".into());
    }
    let runs = v["check_runs"]
        .as_array()
        .ok_or("gh returned a non-list commit check_runs field")?;
    let n = v["total_count"].as_u64();
    if n.is_none_or(|n| n >= 100 || n < runs.len() as u64) {
        return Err("gh returned an invalid or truncated commit check-runs count".into());
    }
    runs.iter()
        .map(|r| {
            if !r.is_object() {
                return Err("gh returned a non-object commit check-run entry".into());
            }
            let app = &r["app"];
            if !app.is_null() && !app.is_object() {
                return Err("gh returned an invalid check-run app field".into());
            }
            Ok(json!({
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "conclusion": r["conclusion"],
                "head_sha": lower(&r["head_sha"]),
                "app_id": app["id"],
                "app_slug": app["slug"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
            }))
        })
        .collect()
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Inventory {
    Reviews,
    Inline,
    Issues,
    Reactions,
    Requested,
}
fn normalize(v: &Value, inventory: Inventory, source: &str) -> Result<Vec<Value>, String> {
    let (name, singular, plural) = match inventory {
        Inventory::Reviews => ("reviews", "review", "reviews payload"),
        Inventory::Inline => (
            "review_comments",
            "review comment",
            "review comments payload",
        ),
        Inventory::Issues => ("issue_comments", "issue comment", "issue comments payload"),
        Inventory::Reactions => ("reactions", "reaction", "reactions payload"),
        Inventory::Requested => (
            "requested_reviewers",
            "requested reviewer",
            "requested_reviewers field",
        ),
    };
    let list = v
        .as_array()
        .ok_or_else(|| format!("gh returned a non-list pull-request {plural}"))?;
    if inventory == Inventory::Requested && list.len() >= 100 {
        return Err("gh returned an incomplete pull-request requested_reviewers list".into());
    }
    // A full page is rejected by the closed snapshot parser. Do not clone an
    // over-cap wire document while normalizing its individual records.
    if list.len() >= 100 {
        return Err(format!(
            "pull-request review snapshot {name} has {} records (limit 99); page is incomplete in {source}",
            list.len()
        ));
    }
    list.iter()
        .map(|r| {
            if !r.is_object() {
                return Err(format!(
                    "gh returned a non-object pull-request {singular} entry"
                ));
            }
            let actor = if inventory == Inventory::Requested {
                r
            } else {
                &r["user"]
            };
            if !actor.is_object() {
                return Err(format!(
                    "gh returned a {singular} without an author user object"
                ));
            }
            let mut item = json!({"author_login":actor["login"],"author_id":actor["id"]});
            if inventory == Inventory::Requested {
                return Ok(item);
            }
            item["id"] = r["id"].clone();
            let fields: &[&str] = match inventory {
                Inventory::Reviews => &["state", "submitted_at", "body"],
                Inventory::Inline => &["created_at", "pull_request_review_id", "path", "body"],
                Inventory::Issues => &["created_at", "updated_at", "body"],
                Inventory::Reactions => &["created_at", "content"],
                Inventory::Requested => unreachable!(),
            };
            for &key in fields {
                item[key] = r[key].clone()
            }
            match inventory {
                Inventory::Reviews => item["commit_id"] = lower(&r["commit_id"]),
                Inventory::Inline => {
                    item["commit_id"] = lower(if r["original_commit_id"].is_null() {
                        &r["commit_id"]
                    } else {
                        &r["original_commit_id"]
                    })
                }
                Inventory::Issues => {
                    let app = &r["performed_via_github_app"];
                    if !app.is_null() && !app.is_object() {
                        return Err("gh returned an invalid performed_via_github_app field".into());
                    }
                    item["app_id"] = app["id"].clone();
                }
                _ => (),
            }
            Ok(item)
        })
        .collect()
}

pub(super) fn collect(
    reference: &PullRequest,
    digest: &str,
    include_runs: bool,
    get: &mut dyn FnMut(&str) -> Result<Value, String>,
) -> Result<(Snapshot, Value), String> {
    let repo = format!("repos/{}/{}", reference.owner(), reference.name());
    let number = reference.number();
    let pr = format!("{repo}/pulls/{number}");
    let issue = format!("{repo}/issues/{number}");
    let metadata = get(&pr)?;
    if !metadata.is_object() {
        return Err("gh returned an unexpected pull-request metadata document".into());
    }
    if metadata["number"].as_i64() != Some(number) {
        return Err("gh returned a different or invalid pull-request number".into());
    }
    let actual = PullRequest::parse(
        metadata["html_url"]
            .as_str()
            .ok_or("gh returned an invalid pull-request URL")?,
        None,
    )
    .map_err(|_| "gh returned an invalid pull-request URL")?;
    if actual.number() != number
        || !actual
            .repository()
            .eq_ignore_ascii_case(&reference.repository())
    {
        return Err("gh returned a different pull-request identity".into());
    }
    let state = metadata["state"]
        .as_str()
        .filter(|s| matches!(*s, "open" | "closed"))
        .ok_or("gh returned an invalid pull-request state")?;
    let draft = metadata.get("draft").map_or(Ok(false), |v| {
        v.as_bool()
            .ok_or("gh returned an invalid pull-request draft flag")
    })?;
    let sha = |key: &str| {
        if !metadata[key].is_object() {
            return Err(format!("gh returned an invalid pull-request {key} object"));
        }
        metadata[key]["sha"]
            .as_str()
            .map(str::to_ascii_lowercase)
            .filter(|s| hex(s, 40))
            .ok_or_else(|| format!("gh returned an invalid pull-request {key}.sha"))
    };
    let head = sha("head")?;
    let base = sha("base")?;
    let inline = get(&format!("{pr}/comments?per_page=100"))?;
    let issues = get(&format!("{issue}/comments?per_page=100"))?;
    let reactions = get(&format!("{issue}/reactions?per_page=100"))?;
    let runs = if include_runs {
        check_runs(&get(&format!(
            "{repo}/commits/{head}/check-runs?per_page=100"
        ))?)?
    } else {
        Vec::new()
    };
    let reviews = get(&format!("{pr}/reviews?per_page=100"))?;
    let source = actual.url();
    let requested = metadata
        .get("requested_reviewers")
        .map(|v| normalize(v, Inventory::Requested, &source))
        .transpose()?
        .unwrap_or_default();
    let doc = json!({
        "version": 1,
        "plan_digest": digest,
        "number": number,
        "url": source,
        "state": state,
        "draft": draft,
        "head_sha": head,
        "base_sha": base,
        "head_committed_at": Value::Null,
        "reviews": normalize(&reviews, Inventory::Reviews, &source)?,
        "review_comments": normalize(&inline, Inventory::Inline, &source)?,
        "issue_comments": normalize(&issues, Inventory::Issues, &source)?,
        "reactions": normalize(&reactions, Inventory::Reactions, &source)?,
        "check_runs": runs,
        "requested_reviewers": requested,
    });
    Ok((Snapshot::parse(&doc, &source)?, reviews))
}

pub(super) fn submit(client: &Client, snapshot: &Snapshot, event: &str) -> Result<Value, String> {
    let head = snapshot.head_sha();
    let state = if event == "APPROVE" {
        "APPROVED"
    } else {
        "CHANGES_REQUESTED"
    };
    let substantive: Vec<_> = snapshot
        .reviews
        .iter()
        .filter(|r| {
            matches!(
                r.state.as_str(),
                "APPROVED" | "CHANGES_REQUESTED" | "DISMISSED"
            )
        })
        .collect();
    let receipt = |action: &str, id: u64| json!({"action":action,"event":event,"state":state,"review_id":id,"commit_id":head});
    if substantive
        .iter()
        .any(|r| r.commit.as_deref() == Some(head) && r.state == state)
    {
        let actor = match client.get("user") {
            Ok(user) => user["login"]
                .as_str()
                .filter(|s| !s.is_empty())
                .zip(user["id"].as_u64().filter(|&id| id > 0))
                .map(|(s, id)| (s.to_owned(), id)),
            Err(_) if std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true") => {
                Some(("github-actions[bot]".to_owned(), 41898282))
            }
            Err(_) => None,
        };
        if let Some((login, id)) = actor
            && let Some(latest) = substantive
                .iter()
                .filter(|r| r.actor.login.eq_ignore_ascii_case(&login) && r.actor.id == Some(id))
                .max_by_key(|r| (r.time, r.id))
            && latest.commit.as_deref() == Some(head)
            && latest.state == state
        {
            return Ok(receipt("unchanged", latest.id));
        }
    }
    let verdict = if event == "APPROVE" {
        "passed"
    } else {
        "failed"
    };
    let body = format!(
        "YAGA pull-request review check {verdict} for commit `{}`.\n\n<!-- yaga-pull-request-review:v1 -->",
        &head[..12]
    );
    let reference = &client.reference;
    let endpoint = format!(
        "repos/{}/{}/pulls/{}/reviews",
        reference.owner(),
        reference.name(),
        reference.number()
    );
    let created = client.write(
        &endpoint,
        "POST",
        &[("commit_id", head), ("event", event), ("body", &body)],
    )?;
    if !created.is_object() {
        return Err("gh returned an invalid pull-request review submission document".into());
    }
    let id = created["id"]
        .as_u64()
        .filter(|&n| n > 0)
        .ok_or("gh returned an invalid pull-request review id")?;
    if created["state"] != state {
        return Err("gh returned an unexpected pull-request review state".into());
    }
    if !created["commit_id"]
        .as_str()
        .is_some_and(|s| s.eq_ignore_ascii_case(head))
    {
        return Err("gh returned a pull-request review bound to a different commit_id".into());
    }
    Ok(receipt("submitted", id))
}
