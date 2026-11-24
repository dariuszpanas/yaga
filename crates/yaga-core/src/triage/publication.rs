use super::{
    Snapshot, ascii_json,
    remote::{TriageClient, positive, record, revalidate},
};
use crate::policies::triage::TriagePolicy;
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fmt::Write, sync::LazyLock};
const OWNED: &str = "<!-- yaga-pull-request-triage:v1 -->";
fn inline(s: &str) -> String {
    static CONTROL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\p{C}$").unwrap());
    let mut visible = String::new();
    for c in s.chars().take(200) {
        if CONTROL.is_match(c.encode_utf8(&mut [0; 4])) {
            write!(visible, "\\u{:04x}", c as u32).unwrap();
        } else {
            visible.push(c);
        }
    }
    if s.chars().nth(200).is_some() {
        visible.push_str("...");
    }
    format!(
        "<code>{}</code>",
        visible
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
            .replace('"', "&quot;")
            .replace('\'', "&#x27;")
            .replace('`', "&#96;")
    )
}
fn body(report: &Value) -> Result<String, String> {
    let mut canonical = report.clone();
    canonical.sort_all_objects();
    let digest = format!(
        "{:x}",
        Sha256::digest(ascii_json(&canonical.to_string()).as_bytes())
    );
    let b = &report["boundary"];
    let a = &report["admission"];
    let i = &report["inventory"];
    let s = |v: &Value| v.as_str().unwrap().to_owned();
    let mut lines=vec![OWNED.into(),format!("<!-- report-sha256:{digest} -->"),"YAGA pull-request triage".into(),"".into(),
        format!("Repository ID: {}; PR: #{}; head: `{}`.",b["repository_id"],b["number"],s(&b["head_sha"])),
        format!("Admission: **{}** ({}).",s(&a["decision"]),s(&a["reason"])),
        format!("Author classification: {}; source: {}.",s(&report["classification"]),s(&report["provenance"])),"".into(),
        "Findings describe review surfaces and exact content matches; they do not establish malicious intent.".into(),
        "Diffs and instruction text are untrusted data. This metadata check does not inspect their meaning.".into(),"".into(),
        format!("Changed files: {}. Duplicate scope: {} most recently updated closed PRs (limit {}); this is not a complete historical search.",i["changed_files"],i["candidates_examined"],i["candidate_limit"]),
        format!("Findings: {} ({} omitted from JSON).",report["finding_count"],report["omitted_findings"])];
    for f in report["findings"].as_array().unwrap().iter().take(10) {
        let detail = f["path"].as_str().map_or_else(
            || format!("merged PR #{}", f["pull_request_number"]),
            inline,
        );
        lines.push(format!("- {}: {detail}", s(&f["code"])));
    }
    lines.extend(["".into(),"A bot review, label, comment, or earlier-head approval cannot authorize external changes.".into(),
        "A configured maintainer who has reviewed this exact head, base, and trusted policy can copy this acknowledgment into their APPROVED review:".into(),"```text".into(),s(&a["acknowledgment"]),"```".into(),
        format!("Base: {} ({}).",s(&b["base_sha"]),inline(&s(&b["base_ref"]))),
        format!("Trusted policy digest: {}.",s(&report["policy_digest"]))]);
    let text = lines.join("\n") + "\n";
    if text.len() > 16384 {
        return Err("triage comment exceeded its bound".into());
    }
    Ok(text)
}
pub(super) fn publish(
    client: &mut TriageClient,
    policy: &TriagePolicy,
    expected: &Snapshot,
) -> Result<Value, String> {
    let actor = match policy.comment_author_id {
        Some(id) => id,
        None => positive(
            &record(&client.get("user")?, "authenticated publisher")?["id"],
            "publisher ID",
        )?,
    };
    let prefix = client.prefix();
    let number = client.api.reference.number();
    let comments = client.get(&format!("{prefix}/issues/{number}/comments?per_page=100"))?;
    let comments = comments
        .as_array()
        .filter(|a| a.len() < 100)
        .ok_or("triage comment inventory is incomplete")?;
    let mut matched = None;
    for comment in comments {
        if !comment.is_object() {
            return Err("missing triage API comment".into());
        }
        let text = if comment["body"].is_null() {
            ""
        } else {
            comment["body"]
                .as_str()
                .filter(|s| s.len() <= 65536)
                .ok_or("invalid triage comment body")?
        };
        if text.contains(OWNED) {
            if !comment["user"].is_object() {
                return Err("missing triage API comment author".into());
            }
            if positive(&comment["user"]["id"], "comment author ID")? != actor
                || !text.starts_with(&format!("{OWNED}\n"))
            {
                return Err("triage comment marker ownership is ambiguous".into());
            }
            if matched.replace((comment, text)).is_some() {
                return Err("multiple owned triage comments are ambiguous".into());
            }
        }
    }
    let (current, mut report) = revalidate(client, policy, expected)?;
    if current.0["state"] != "open" || current.0["draft"] != false {
        return Err("triage comment requires an open ready PR".into());
    }
    let new_body = body(&report)?;
    let (method, endpoint, expected_id) = if let Some((c, text)) = matched {
        let id = positive(&c["id"], "comment ID")?;
        if new_body == text {
            report["published_comment"] = json!({"action":"unchanged","comment_id":id});
            return Ok(report);
        }
        ("PATCH", format!("{prefix}/issues/comments/{id}"), Some(id))
    } else {
        ("POST", format!("{prefix}/issues/{number}/comments"), None)
    };
    let response = client.write(&endpoint, method, &new_body)?;
    let id = published_id(&response, actor, &new_body, expected_id)?;
    report["published_comment"] =
        json!({"action":if expected_id.is_some(){"updated"}else{"created"},"comment_id":id});
    Ok(report)
}
fn published_id(
    response: &Value,
    actor: u64,
    body: &str,
    expected_id: Option<u64>,
) -> Result<u64, String> {
    let response = record(response, "published comment")?;
    let author = record(&response["user"], "published comment author")?;
    if positive(&author["id"], "publisher ID")? != actor || response["body"] != body {
        return Err("triage comment publication did not match expected owner/body".into());
    }
    let id = positive(&response["id"], "comment ID")?;
    if expected_id.is_some_and(|expected| expected != id) {
        return Err("triage comment publication returned a different comment ID".into());
    }
    Ok(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn update_receipt_requires_the_exact_selected_comment() {
        let response = json!({"user":{"id":55}, "body":"updated report", "id":501});
        assert_eq!(published_id(&response, 55, "updated report", None), Ok(501));
        assert_eq!(
            published_id(&response, 55, "updated report", Some(501)),
            Ok(501)
        );
        assert_eq!(
            published_id(&response, 55, "updated report", Some(500)),
            Err("triage comment publication returned a different comment ID".into())
        );
    }
}
