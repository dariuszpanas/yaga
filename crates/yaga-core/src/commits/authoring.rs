use super::{
    checker::resolve_presence,
    unicode::{fold, is_space},
    *,
};
use regex::Regex;
use serde::Serialize;
use std::{collections::HashSet, sync::LazyLock};

#[derive(Clone, Copy, Debug)]
pub enum TemplateComment {
    Hash,
    Semicolon,
}

#[derive(Debug, Serialize)]
pub struct TypeExplanation<'a> {
    #[serde(rename = "type")]
    pub commit_type: &'a str,
    pub listed_type_allowed: bool,
    pub scope_policy: Presence,
    pub scope_source: String,
    pub body_policy: Presence,
    pub body_source: String,
    pub note: &'static str,
}

pub fn explain_type<'a>(
    policy: &CommitPolicy,
    kind: &'a str,
) -> Result<TypeExplanation<'a>, String> {
    if policy.message_format == MessageFormat::Plain {
        return Err("plain message-format has no commit type to explain".into());
    }
    static UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{C}()!:]").unwrap());
    if !(1..=128).contains(&kind.chars().count())
        || kind.chars().any(is_space)
        || UNSAFE.is_match(kind)
    {
        return Err(
            "configuration type must be one safe type token of at most 128 characters".into(),
        );
    }
    let (scope_policy, scope_key) =
        resolve_presence(policy.scope_policy, &policy.scope_policy_by_type, kind);
    let (body_policy, body_key) =
        resolve_presence(policy.body_policy, &policy.body_policy_by_type, kind);
    let kind_folded = fold(kind);
    Ok(TypeExplanation {
        commit_type: kind,
        listed_type_allowed: policy
            .allowed_types
            .as_ref()
            .is_none_or(|items| items.iter().any(|k| fold(k) == kind_folded)),
        scope_policy,
        scope_source: scope_key.map_or_else(
            || "scope-policy".into(),
            |key| format!("scope-policy-by-type.{key}"),
        ),
        body_policy,
        body_source: body_key.map_or_else(
            || "body-policy".into(),
            |key| format!("body-policy-by-type.{key}"),
        ),
        note: "Body policy applies to full messages only; all other configured rules still apply.",
    })
}

pub fn message_template(
    policy: &CommitPolicy,
    kind: Option<&str>,
    scope: Option<&str>,
    comment: TemplateComment,
) -> Result<String, String> {
    let (header, body_policy) = match policy.message_format {
        MessageFormat::Plain => {
            if kind.is_some() || scope.is_some() {
                return Err("plain message templates do not accept --type or --scope".into());
            }
            (String::new(), policy.body_policy)
        }
        MessageFormat::Conventional => {
            let kind = kind.ok_or("conventional message templates require an explicit --type")?;
            let effective = explain_type(policy, kind)?;
            if scope.is_some_and(|s| !(1..=128).contains(&s.chars().count())) {
                return Err("template scope must contain 1 to 128 characters".into());
            }
            let header = match scope {
                Some(s) => format!("{kind}({s}): "),
                None => format!("{kind}: "),
            };
            let candidate = format!("{header}description");
            let parsed = parse_message(
                &candidate,
                MessageFormat::Conventional,
                FooterSyntax::Conventional,
            );
            if parsed.as_ref().is_none_or(|p| {
                p.commit_type.as_deref() != Some(kind) || p.scope.as_deref() != scope
            }) {
                return Err("template type or scope is not one safe component".into());
            }
            let mut strict = policy.clone();
            strict.ignored_headers.clear();
            if check_message(&candidate, &strict, false, true)
                .diagnostics
                .iter()
                .any(|d| {
                    d.severity == Severity::Error
                        && ["syntax.", "type.", "scope."]
                            .iter()
                            .any(|p| d.code.starts_with(p))
                })
            {
                return Err(
                    "template type or scope does not satisfy configured component policy".into(),
                );
            }
            (header, effective.body_policy)
        }
    };
    let mut output = header;
    let limits = [
        policy.header_max_length,
        (policy.body_min_words != 0).then_some(policy.body_min_words),
        (policy.body_min_length != 0).then_some(policy.body_min_length),
    ];
    append_template_guidance(&mut output, policy, body_policy, comment, limits);
    Ok(output)
}

/// Append the body instructions and empty footer fields after an already
/// validated header. Limits are the optional header maximum, prose word
/// minimum and prose length minimum. A host can supply display placeholders
/// instead of converting arbitrary-width integers to the native word size.
pub fn append_template_guidance<T: std::fmt::Display>(
    output: &mut String,
    policy: &CommitPolicy,
    body_policy: Presence,
    comment: TemplateComment,
    [header_limit, min_words, min_length]: [Option<T>; 3],
) {
    let marker = match comment {
        TemplateComment::Hash => '#',
        TemplateComment::Semicolon => ';',
    };
    let mut notes=vec![
        "Write your description on the first line; this incomplete template must not be committed as-is.".into(),
        format!("Comment character: {marker}. Match your Git editor cleanup setting or remove these lines."),
        format!("Body policy: {}. Blank lines separate the header, prose, and final footers.",body_policy.as_str())
    ];
    if let Some(max) = header_limit {
        notes.push(format!(
            "Header limit: {max} {} units.",
            policy.length_unit.as_str()
        ));
    }
    if body_policy != Presence::Forbidden {
        if let Some(min) = min_words {
            notes.push(format!("Prose requires at least {min} words when present."));
        }
        if let Some(min) = min_length {
            notes.push(format!(
                "Prose minimum: {min} {} units when present.",
                policy.length_unit.as_str()
            ));
        }
    }
    if !policy.required_issue_prefixes.is_empty() {
        notes.push(format!(
            "Include a real work-item reference using one prefix: {}",
            policy.required_issue_prefixes.join(", ")
        ));
    }
    for (key, values) in &policy.footer_values {
        notes.push(format!("Allowed values for {key}: {}", values.join(" | ")));
    }
    if !policy.warning_rules.is_empty() {
        notes.push(format!(
            "Warning-only rules: {}",
            policy.warning_rules.join(", ")
        ));
    }
    notes.push(
        "Other configured rules still apply; validate the completed message before committing."
            .into(),
    );
    output.push_str("\n\n");
    for note in notes {
        output.push_str(&format!("{marker} {note}\n"));
    }
    output.push('\n');
    let mut seen = HashSet::new();
    for key in policy
        .required_colon_footer_tokens
        .iter()
        .chain(&policy.required_footer_tokens)
    {
        if seen.insert(key.to_ascii_lowercase()) {
            output.push_str(&format!("{key}: \n"));
        }
    }
}
