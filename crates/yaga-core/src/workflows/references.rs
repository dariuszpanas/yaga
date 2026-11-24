//! Lexical checks for step actions, job workflow calls and container images.
use super::ReferenceContext;
use regex::Regex;
use std::{net::Ipv6Addr, sync::LazyLock};

type Finding = Option<(&'static str, &'static str)>;
const SYNTAX: Finding = Some(("uses.syntax", "uses reference has invalid syntax"));
const CONTEXT: Finding = Some((
    "uses.context",
    "uses reference is not permitted in this workflow context",
));
const PIN: Finding = Some((
    "uses.pin",
    "uses reference must use an immutable lowercase digest",
));
const IMAGE_SYNTAX: Finding = Some(("image.syntax", "container image has invalid syntax"));
const IMAGE_PIN: Finding = Some((
    "image.pin",
    "container image must use an immutable lowercase digest",
));
static IMAGE_COMPONENT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$").unwrap());

fn segment(value: &str, plus: bool) -> bool {
    !matches!(value, "" | "." | "..")
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b) || (plus && b == b'+'))
}
fn unsafe_value(v: &str) -> bool {
    // The complete grammar for all uses and image payloads is ASCII.
    v.is_empty()
        || v.len() > 1024
        || !v.is_ascii()
        || v.contains('\\')
        || v.contains("${{")
        || v.contains("}}")
        || v.matches('@').count() > 1
        || v.bytes()
            .any(|b| b.is_ascii_whitespace() || b.is_ascii_control())
}
fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn workflow_path(parts: &[&str]) -> bool {
    matches!(parts,[".github","workflows",name] if
        (name.strip_suffix(".yml").or_else(||name.strip_suffix(".yaml"))).is_some_and(|stem|!stem.is_empty())
        && segment(name,false))
}
fn port(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit())
}
fn domain(value: &str) -> bool {
    if let Some(literal) = value.strip_prefix('[') {
        let Some((addr, suffix)) = literal.split_once(']') else {
            return false;
        };
        return !addr.contains('.')
            && addr.parse::<Ipv6Addr>().is_ok()
            && (suffix.is_empty() || suffix.strip_prefix(':').is_some_and(port));
    }
    let host = if let Some((host, p)) = value.split_once(':') {
        if !port(p) {
            return false;
        }
        host
    } else {
        value
    };
    host.split('.').all(|label| {
        !label.is_empty()
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    })
}
fn image_name(input: &str) -> bool {
    let name = if input.rfind(':') > input.rfind('/') {
        let (name, tag) = input.rsplit_once(':').unwrap();
        if tag.is_empty()
            || tag.len() > 128
            || !tag
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
            || tag.starts_with(['-', '.'])
        {
            return false;
        }
        name
    } else {
        input
    };
    if !(1..=255).contains(&name.len()) {
        return false;
    }
    let mut parts = name.split('/').peekable();
    let first = parts.next().unwrap();
    let registry = parts.peek().is_some()
        && (first.contains(['.', ':'])
            || first.eq_ignore_ascii_case("localhost")
            || first.bytes().any(|b| b.is_ascii_uppercase()));
    let head_valid = if registry {
        domain(first)
    } else {
        IMAGE_COMPONENT.is_match(first)
    };
    head_valid && parts.all(|p| IMAGE_COMPONENT.is_match(p))
}
fn image_issue(value: &str) -> Finding {
    let (image, digest) = value
        .split_once('@')
        .map_or((value, None), |(i, d)| (i, Some(d)));
    if digest.is_some_and(|d| {
        d.is_empty()
            || !d
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b":._+-".contains(&b))
    }) || !image_name(image)
    {
        return IMAGE_SYNTAX;
    }
    if !digest
        .and_then(|d| d.strip_prefix("sha256:"))
        .is_some_and(|h| hex(h, 64))
    {
        return IMAGE_PIN;
    }
    None
}
/// Return the stable reference-policy diagnostic for one container image.
pub fn check_image(value: &str, tag: &str, quoted: bool, service: bool) -> Finding {
    if tag != "tag:yaml.org,2002:str" {
        return IMAGE_SYNTAX;
    }
    if service && quoted && value.is_empty() {
        return None;
    }
    if unsafe_value(value) {
        return IMAGE_SYNTAX;
    }
    image_issue(value)
}

/// Return the stable reference-policy diagnostic for one literal job or step call.
pub fn check_uses(value: &str, context: ReferenceContext) -> Finding {
    if unsafe_value(value) {
        return SYNTAX;
    }
    if let Some(image) = value.strip_prefix("docker://") {
        let issue = image_issue(image);
        if issue == IMAGE_SYNTAX {
            return SYNTAX;
        }
        if context == ReferenceContext::Job {
            return CONTEXT;
        }
        return if issue == IMAGE_PIN { PIN } else { None };
    }
    if let Some(relative) = value
        .strip_prefix("./")
        .or_else(|| value.strip_prefix("$/"))
    {
        let parts: Vec<_> = relative.split('/').collect();
        if value.contains('@') || (!relative.is_empty() && !parts.iter().all(|p| segment(p, true)))
        {
            return SYNTAX;
        }
        return if context == ReferenceContext::Step || workflow_path(&parts) {
            None
        } else {
            CONTEXT
        };
    }
    let (path, revision) = value
        .split_once('@')
        .map_or((value, None), |(p, r)| (p, Some(r)));
    if revision.is_some_and(|r| !r.split('/').all(|s| segment(s, true))) {
        return SYNTAX;
    }
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() < 2
        || !parts.iter().all(|p| segment(p, true))
        || !parts[..2].iter().all(|p| segment(p, false))
    {
        return SYNTAX;
    }
    if (context == ReferenceContext::Step && parts.get(2..4) == Some(&[".github", "workflows"][..]))
        || (context == ReferenceContext::Job && !workflow_path(&parts[2..]))
    {
        return CONTEXT;
    }
    if !revision.is_some_and(|r| hex(r, 40)) {
        return PIN;
    }
    None
}
