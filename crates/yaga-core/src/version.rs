//! The bounded final-release comparison grammar used by the policy schemas.

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const INVALID: &str = "required-version expects comma-separated >=, >, <=, <, == or != comparisons against final MAJOR.MINOR.PATCH releases";
pub const REQUIREMENT_TYPE_ERROR: &str =
    "required-version must be a string of 1 through 256 characters";

fn release(value: &str) -> Option<[u32; 3]> {
    let mut parts = value.split('.');
    let mut numbers = [0; 3];
    for number in &mut numbers {
        let part = parts.next()?;
        if !(1..=9).contains(&part.len())
            || !part.bytes().all(|byte| byte.is_ascii_digit())
            || (part.len() > 1 && part.starts_with('0'))
        {
            return None;
        }
        *number = part.parse().ok()?;
    }
    parts.next().is_none().then_some(numbers)
}

pub fn check_requirement(requirement: &str) -> Result<(), String> {
    check_requirement_for(requirement, VERSION)
}

/// Check the same release grammar against an explicit installed or target version.
pub fn check_requirement_for(requirement: &str, version: &str) -> Result<(), String> {
    if requirement.len() > 4 * 256 || !(1..=256).contains(&requirement.chars().count()) {
        return Err(REQUIREMENT_TYPE_ERROR.into());
    }
    let mut clauses = Vec::new();
    if requirement.split(',').count() > 8 {
        return Err("required-version permits at most eight comparisons".into());
    }
    for raw in requirement.split(',') {
        // str.strip also accepts the four ASCII file/group/record/unit separators.
        let clause =
            raw.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c));
        let op = [">=", "<=", "==", "!=", ">", "<"]
            .into_iter()
            .find(|op| clause.starts_with(op))
            .ok_or(INVALID)?;
        let rest = clause[op.len()..].trim_start_matches([' ', '\t', '\r', '\n', '\x0b', '\x0c']);
        clauses.push((op, release(rest).ok_or(INVALID)?));
    }
    let current =
        release(version).ok_or("cannot evaluate required-version for a non-final YAGA version")?;
    for (op, expected) in clauses {
        let satisfied = match op {
            ">=" => current >= expected,
            ">" => current > expected,
            "<=" => current <= expected,
            "<" => current < expected,
            "==" => current == expected,
            "!=" => current != expected,
            _ => unreachable!(),
        };
        if !satisfied {
            let quoted = crate::text::python_repr(requirement);
            return Err(format!(
                "YAGA {version} does not satisfy required-version {quoted}; update YAGA using its installation manager"
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_release_boundaries() {
        assert!(check_requirement(&format!(">= 0.0.0, =={VERSION}")).is_ok());
        for invalid in [
            "> 01.1.0",
            "=1.0.0",
            ">0.1",
            ">=0.1.0, >1.2.3a1",
            ">\u{a0}1.2.3",
        ] {
            assert_eq!(check_requirement(invalid).unwrap_err(), INVALID);
        }
        assert!(
            check_requirement(">=999999999.0.0")
                .unwrap_err()
                .contains("does not satisfy")
        );
        assert_eq!(check_requirement("<999.0.0, bad").unwrap_err(), INVALID);
    }
}
