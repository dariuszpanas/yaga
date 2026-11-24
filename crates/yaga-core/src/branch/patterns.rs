use super::{MAX_BRANCH_COMPONENTS, MAX_BRANCH_NAME_BYTES, SYNTAX};

pub(super) fn validate_transport(name: &str) -> Result<(), String> {
    if name.is_empty() {
        Err("branch name must not be empty".into())
    } else if name.len() > MAX_BRANCH_NAME_BYTES {
        Err(format!(
            "branch name exceeds {MAX_BRANCH_NAME_BYTES} UTF-8 bytes"
        ))
    } else {
        Ok(())
    }
}

fn literal(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
}

fn valid_start(byte: u8, index: usize) -> bool {
    byte.is_ascii_alphabetic() || (index > 0 && byte.is_ascii_digit())
}

fn lock_ending(component: &str) -> bool {
    component
        .as_bytes()
        .last_chunk::<5>()
        .is_some_and(|s| s.eq_ignore_ascii_case(b".lock"))
}

/// Check portable syntax after separately bounding the transport.
pub fn is_portable(name: &str) -> bool {
    !name.contains("..")
        && !name.eq_ignore_ascii_case("head")
        && !name
            .as_bytes()
            .first_chunk::<5>()
            .is_some_and(|s| s.eq_ignore_ascii_case(b"refs/"))
        && !(matches!(name.len(), 40 | 64) && name.bytes().all(|b| b.is_ascii_hexdigit()))
        && name.split('/').enumerate().all(|(index, component)| {
            let bytes = component.as_bytes();
            index < MAX_BRANCH_COMPONENTS
                && bytes.first().is_some_and(|&b| valid_start(b, index))
                && bytes.last().is_some_and(u8::is_ascii_alphanumeric)
                && bytes.iter().copied().all(literal)
                && !lock_ending(component)
        })
}

/// Validate the closed v1 branch grammar and return its components.
pub fn validate_branch_pattern(pattern: &str) -> Result<Vec<&str>, String> {
    if pattern.is_empty() {
        return Err("branch pattern must be a nonempty string".into());
    }
    if !pattern.is_ascii() {
        return Err("branch pattern must contain only portable ASCII characters".into());
    }
    if pattern.len() > MAX_BRANCH_NAME_BYTES {
        return Err(format!(
            "branch pattern exceeds {MAX_BRANCH_NAME_BYTES} bytes"
        ));
    }
    let components: Vec<_> = pattern.split('/').collect();
    if components.len() > MAX_BRANCH_COMPONENTS || components.iter().any(|value| value.is_empty()) {
        return Err(format!(
            "branch pattern requires 1 through {MAX_BRANCH_COMPONENTS} nonempty components"
        ));
    }
    if pattern.contains("..") {
        return Err("branch pattern must not contain '..'".into());
    }
    for (index, &component) in components.iter().enumerate() {
        if component == "**" {
            continue;
        }
        if component.contains("**") {
            return Err("branch pattern ** wildcard must occupy an entire component".into());
        }
        if !component.bytes().all(|byte| literal(byte) || byte == b'*') {
            return Err("branch pattern contains an unsupported literal character".into());
        }
        if lock_ending(component) {
            return Err("branch pattern component must not end with '.lock'".into());
        }
        let bytes = component.as_bytes();
        if bytes[0] != b'*' && !valid_start(bytes[0], index) {
            return Err("branch pattern component has an invalid literal start".into());
        }
        if !component.ends_with('*') && !bytes[bytes.len() - 1].is_ascii_alphanumeric() {
            return Err("branch pattern component has an invalid literal ending".into());
        }
    }
    Ok(components)
}

pub fn match_branch_pattern(pattern: &str, branch: &str) -> Result<bool, String> {
    validate_branch_pattern(pattern)?;
    validate_transport(branch)?;
    if !is_portable(branch) {
        return Err(SYNTAX.message.into());
    }
    crate::paths::match_pattern(pattern, branch).map_err(|error| error.to_string())
}
