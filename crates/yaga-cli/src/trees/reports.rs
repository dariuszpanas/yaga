use super::{Failure, annotation, git_error, input_error};
use crate::Format;
use serde_json::{Value, json};
use yaga_core::paths::{
    modes::ModePolicy,
    portability::PathPolicy,
    records::{self, ModeKind, NameSelection},
    sizes::SizePolicy,
    tree::TreePolicy,
};
use yaga_core::text::{json_text, safe_text};

pub(super) struct Report {
    pub document: Value,
    pub count: usize,
    pub items: Vec<Item>,
    pub summary: String,
}
pub(super) struct Item {
    pub document: Value,
    pub text: String,
    pub github: String,
}

fn visible(format: Format, count: usize) -> usize {
    match format {
        Format::Text => 8,
        Format::Json => 256,
        Format::Github if count > 32 => 31,
        Format::Github => 32,
    }
}

fn text_pattern(pattern: Option<&str>) -> String {
    pattern
        .map(|p| format!(" (pattern: {})", safe_text(p, 1000)))
        .unwrap_or_default()
}
fn annotation_pattern(pattern: Option<&str>) -> String {
    pattern
        .map(|p| format!("; first matching pattern: {}", safe_text(p, 1000)))
        .unwrap_or_default()
}
fn names(modes: &[ModeKind]) -> Vec<&'static str> {
    modes.iter().map(|m| m.name()).collect()
}

pub(super) fn tree(policy: &TreePolicy, bytes: &[u8], format: Format) -> Result<Report, Failure> {
    let paths = records::parse_names(bytes, NameSelection::Tree).map_err(git_error)?;
    let found = policy.check(&paths).map_err(input_error)?;
    let missing = found
        .iter()
        .take_while(|f| f.code == "tree.required")
        .count();
    let forbidden = found.len() - missing;
    let summary = format!(
        "{} tree entries; {missing} missing required; {forbidden} forbidden.",
        paths.len()
    );
    let document = json!({"entries_checked":paths.len(),"required_paths":policy.required_paths(),
        "forbidden_patterns":policy.forbidden_patterns().collect::<Vec<_>>(),"missing_required":missing,"forbidden_paths":forbidden});
    let items = found.iter().take(visible(format,found.len())).map(|f| {
        let path = safe_text(f.path,1000);
        let text = format!("FAILED  [{}] {path}: {}{}",f.code,f.message,text_pattern(f.pattern));
        let github = if f.pattern.is_some() { annotation(f.code, Some(f.path), &format!("{}{}",f.message,annotation_pattern(f.pattern))) }
            else { annotation(f.code, None, &format!("{path}: {}",f.message)) };
        Item { document: json!({"code":f.code,"message":f.message,"path":json_text(f.path,4096),"pattern":f.pattern}), text, github }
    }).collect();
    Ok(Report {
        document,
        count: found.len(),
        items,
        summary,
    })
}

pub(super) fn path(policy: &PathPolicy, bytes: &[u8], format: Format) -> Result<Report, Failure> {
    let paths = records::parse_names(bytes, NameSelection::Portability).map_err(git_error)?;
    let report = policy.check(&paths).map_err(input_error)?;
    let count = report.finding_count();
    let components: usize = paths
        .iter()
        .map(|p| p.bytes().filter(|&b| b == b'/').count() + 1)
        .sum();
    let summary = format!(
        "{} path(s), {components} component(s), {count} finding(s).",
        paths.len()
    );
    let by_code = report
        .rule_counts
        .iter()
        .filter(|(_, n)| *n > 0)
        .map(|(rule, n)| (rule.code().to_string(), json!(n)))
        .collect::<serde_json::Map<_, _>>();
    let document = json!({"policy":{"path_policy_version":1,"profile":policy.profile(),"rules":policy.rules().iter().map(|r|r.name()).collect::<Vec<_>>()},
        "counts":{"paths":paths.len(),"components":components,"findings":count,"by_code":by_code}});
    let items = report.diagnostics.iter().take(visible(format,count)).map(|f| {
        let (text_suffix, github_suffix) = if let Some(n) = f.component {
            (format!(" (component {n})"),format!("; component {n}"))
        } else {
            let other = safe_text(f.related_path.expect("collision has a related path"),1000);
            (format!(" (conflicts with: {other})"),format!("; conflicts with {other}"))
        };
        Item { document:json!({"code":f.code,"message":f.message,"path":json_text(f.path,4096),"component":f.component,"related_path":f.related_path.map(|p| json_text(p,4096))}),
            text:format!("FAILED  [{}] {}: {}{text_suffix}",f.code,safe_text(f.path,1000),f.message),
            github:annotation(f.code,Some(f.path),&format!("{}{github_suffix}",f.message)) }
    }).collect();
    Ok(Report {
        document,
        count,
        items,
        summary,
    })
}

pub(super) fn mode(
    policy: &ModePolicy,
    bytes: &[u8],
    oid_length: usize,
    format: Format,
) -> Result<Report, Failure> {
    let selection = records::ModeSelection::parse(bytes, oid_length).map_err(git_error)?;
    let report = policy.check(&selection).map_err(input_error)?;
    let count = report.finding_count;
    let mut counts = serde_json::Map::new();
    let mut labels = Vec::new();
    for kind in [
        ModeKind::Regular,
        ModeKind::Executable,
        ModeKind::Symlink,
        ModeKind::Gitlink,
    ] {
        let n = selection
            .entries()
            .iter()
            .filter(|e| e.kind == kind)
            .count();
        counts.insert(kind.name().to_owned(), json!(n));
        labels.push(format!("{} {n}", kind.name()));
    }
    let overrides: Vec<_> = policy
        .path_overrides()
        .map(|(pattern, modes)| json!({"pattern":pattern,"allowed_modes":names(modes)}))
        .collect();
    let document = json!({"policy":{"mode_policy_version":1,"default_allowed_modes":names(policy.default_allowed_modes()),"path_overrides":overrides},
        "counts":{"entries":selection.entries().len(),"by_mode":counts,"findings":count}});
    let summary = format!(
        "{} entry(s); {}; {count} finding(s).",
        selection.entries().len(),
        labels.join(", ")
    );
    let items = report.diagnostics.iter().take(visible(format,count)).map(|f| {
        let allowed = names(f.allowed_modes);
        let list = allowed.join(", ");
        let kind = f.actual_kind.name();
        Item {document:json!({"code":f.code,"message":f.message,"path":json_text(f.path,4096),"actual_mode":f.actual_mode,"actual_kind":kind,"allowed_modes":allowed,"pattern":f.pattern}),
            text:format!("FAILED  [{}] {}: {} (actual: {} {kind}; allowed: {list}){}",f.code,safe_text(f.path,1000),f.message,f.actual_mode,text_pattern(f.pattern)),
            github:annotation(f.code,Some(f.path),&format!("{}; actual mode {} ({kind}); allowed modes: {list}{}",f.message,f.actual_mode,annotation_pattern(f.pattern)))}
    }).collect();
    Ok(Report {
        document,
        count,
        items,
        summary,
    })
}

pub(super) fn size(
    policy: &SizePolicy,
    bytes: &[u8],
    oid_length: usize,
    format: Format,
) -> Result<Report, Failure> {
    let selection = records::SizeSelection::parse(bytes, oid_length).map_err(git_error)?;
    let report = policy.check(&selection).map_err(input_error)?;
    let blobs = selection.blobs().len();
    let gitlinks = selection.gitlinks().len();
    let count = report.diagnostics.len();
    let exceeded = report
        .diagnostics
        .last()
        .is_some_and(|f| f.code == "size.total");
    let oversized = count - usize::from(exceeded);
    let total = report.total_bytes;
    let limit = policy.max_total_blob_bytes();
    let label = match (limit, exceeded) {
        (None, _) => "not configured",
        (_, true) => "exceeded",
        (_, false) => "not exceeded",
    };
    let summary = format!(
        "{blobs} blob(s), {gitlinks} gitlink(s); {total} byte(s); {oversized} oversized blob(s); total limit {label}."
    );
    let mut document = Value::Null;
    if matches!(format, Format::Json) {
        let mut by_size: Vec<_> = selection.blobs().iter().zip(&report.decisions).collect();
        by_size.sort_unstable_by_key(|((entry, size), _)| (std::cmp::Reverse(*size), entry.path));
        let largest: Vec<_> = by_size.iter().take(32).map(|((entry,size),decision)|
            json!({"path":json_text(entry.path,4096),"oid":entry.oid,"mode":entry.kind.mode(),"size_bytes":size,"max_blob_bytes":decision.max_blob_bytes,"pattern":decision.pattern})
        ).collect();
        let overrides: Vec<_> = policy
            .path_limits()
            .map(|(pattern, n)| json!({"pattern":pattern,"max_blob_bytes":n}))
            .collect();
        document = json!({"policy":{"size_policy_version":1,"default_max_blob_bytes":policy.default_max_blob_bytes(),"max_total_blob_bytes":limit,"path_limits":overrides},
            "counts":{"blobs":blobs,"gitlinks":gitlinks,"oversized_blobs":oversized},
            "total":{"blob_bytes":total,"max_total_blob_bytes":limit,"exceeded":exceeded},
            "largest":{"blobs":largest,"omitted":blobs-largest.len()}});
    }
    let limit = visible(format, count);
    let mut selected: Vec<_> = report.diagnostics.iter().take(limit).collect();
    if exceeded && count > limit {
        selected[limit - 1] = report.diagnostics.last().unwrap();
    }
    let items = selected.iter().map(|f| {
        let size = format!("{} > {} bytes",f.size,f.limit);
        let detail = match f.path {
            Some(p) => format!("{}: {} ({size}){}",safe_text(p,1000), f.message,text_pattern(f.pattern)),
            None => format!("{}: {size}",f.message),
        };
        Item {document: json!({"code":f.code,"message":f.message,"path":f.path.map(|p|json_text(p,4096)),"size_bytes":f.size,"max_bytes":f.limit,"pattern":f.pattern}),
            text:format!("FAILED  [{}] {detail}",f.code),
            github:annotation(f.code,f.path,&format!("{}; {size}{}",f.message,annotation_pattern(f.pattern))) }
    }).collect();
    Ok(Report {
        document,
        count,
        items,
        summary,
    })
}
