use super::{LoadedConfig, Presence, explain_type};
use crate::text::{json_text, python_repr, safe_text};
use serde_json::{Map, Value, json};

fn overrides(values: &[(String, Presence)]) -> Map<String, Value> {
    values
        .iter()
        .map(|(k, v)| (k.to_owned(), json!(v)))
        .collect()
}

/// Effective policy in the installed CLI's schema-v1 key order.
fn policy_document(cfg: &LoadedConfig) -> Value {
    let p = &cfg.policy;
    let q = &cfg.quality;
    let footer_values: Map<String, Value> = p
        .footer_values
        .iter()
        .map(|(key, values)| (key.to_owned(), json!(values)))
        .collect();
    // Insert each key in the report contract's order without relying on
    // recursion depth of the JSON macro for a large policy document.
    macro_rules! fields {
        ($($key:literal : $value:expr),+ $(,)?) => {{
            let mut doc = Map::new();
            $(doc.insert($key.into(),json!($value));)+
            Value::Object(doc)
        }};
    }
    fields! {
        "config_version":1,"required_version":cfg.required_version,
        "allowed_types":p.allowed_types,"type_case":p.type_case,
        "scope_policy":p.scope_policy,"scope_policy_by_type":overrides(&p.scope_policy_by_type),
        "allowed_scopes":p.allowed_scopes,"scope_case":p.scope_case,"header_max_length":p.header_max_length,
        "description_min_length":p.description_min_length,"description_max_length":p.description_max_length,
        "description_ending":p.description_ending,"description_case":p.description_case,
        "length_unit":p.length_unit,"footer_syntax":p.footer_syntax,"line_length_urls":p.line_length_urls,
        "footer_max_line_length":p.footer_max_line_length,"required_colon_footer_tokens":p.required_colon_footer_tokens,
        "breaking_markers":p.breaking_markers,"message_format":p.message_format,"warning_rules":p.warning_rules,
        "required_issue_prefixes":p.required_issue_prefixes,"footer_values":footer_values,
        "required_footer_tokens":p.required_footer_tokens,"forbidden_footer_tokens":p.forbidden_footer_tokens,
        "body_policy":p.body_policy,"body_policy_by_type":overrides(&p.body_policy_by_type),
        "body_max_length":p.body_max_length,"body_min_length":p.body_min_length,"body_min_words":p.body_min_words,
        "body_max_line_length":p.body_max_line_length,"body_paragraph_splitting":p.body_paragraph_splitting,
        "skip_pull_request_authors":cfg.skip_pull_request_authors,"dependabot_pull_requests":cfg.dependabot_pull_requests,
        "dependabot_require_github_signature":cfg.dependabot_require_github_signature,"typos":cfg.typos,
        "typos-config":cfg.typos_config,"pull_request_message":cfg.pull_request_message,
        "quality":json!({"provider":q.provider,"task":q.task,"model_id":q.model,"revision":q.revision,"threshold":q.threshold,
            "region":q.region,"max_tokens":q.max_tokens,"max_input_tokens":q.max_input_tokens,"input_mode":q.input_mode}),
        "merge_commits":p.merge_commits,"ignored_headers":p.ignored_headers,"max_commits":cfg.max_commits,
    }
}

fn nested(v: &Value) -> String {
    match v {
        Value::String(s) => s.to_owned(),
        Value::Array(a) => format!(
            "[{}]",
            a.iter()
                .map(|x| python_repr(x.as_str().unwrap_or_default()))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Null => "None".into(),
        _ => v.to_string(),
    }
}

fn text_value(v: &Value) -> String {
    let text = match v {
        Value::Null => "unlimited".into(),
        Value::Object(m) if m.is_empty() => "none".into(),
        Value::Array(a) if a.is_empty() => "none".into(),
        Value::Object(m) => m
            .iter()
            .map(|(k, v)| format!("{k}={}", nested(v)))
            .collect::<Vec<_>>()
            .join(", "),
        Value::Array(a) => a.iter().map(nested).collect::<Vec<_>>().join(", "),
        _ => nested(v),
    };
    safe_text(&text, 2000)
}

pub fn render_config(
    cfg: &LoadedConfig,
    as_json: bool,
    dry_run: bool,
    kind: Option<&str>,
) -> Result<String, String> {
    let path = cfg.path.as_ref().map(|p| p.to_string_lossy());
    let policy = policy_document(cfg);
    let explanation = kind
        .map(|k| explain_type(&cfg.policy, k))
        .transpose()?
        .map(|e| json!(e));
    if as_json {
        let mut doc = json!({"schema_version":1,"config_path":path.as_ref().map(|s|json_text(s,1000)),"config":policy});
        if let Some(e) = explanation {
            doc["effective_type"] = e;
        }
        if dry_run {
            doc["dry_run"] = true.into();
        }
        return Ok(serde_json::to_string_pretty(&doc).expect("config report is JSON"));
    }
    let mut lines = vec![format!(
        "source: {}",
        path.map_or_else(|| "built-in defaults".into(), |s| safe_text(&s, 1000))
    )];
    if dry_run {
        lines.push("mode: dry-run (no file created)".into());
    }
    for (key, v) in policy.as_object().expect("policy is an object") {
        let value = if key.starts_with("allowed_") && v.is_null() {
            "any".to_owned()
        } else {
            text_value(v)
        };
        lines.push(format!("{}: {value}", key.replace('_', "-")));
    }
    if let Some(e) = explanation {
        lines.push("Effective type requirements:".into());
        for (key, v) in e.as_object().expect("type explanation is an object") {
            lines.push(format!("  {}: {}", key.replace('_', "-"), text_value(v)));
        }
    }
    Ok(lines.join("\n"))
}
