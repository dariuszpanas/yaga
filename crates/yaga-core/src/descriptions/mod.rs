//! Pull-request description policy and bounded Markdown checks.
mod checker;
mod markdown;
pub mod policy;
pub mod sources;
pub use checker::{CheckError, Diagnostic, Report, check_description, unicode_name};

/// Format the bounded, single-line display fields of a checked description.
/// Callers sanitize source and messages using their host's Unicode tables.
/// Diagnostic codes come from the closed description policy vocabulary.
pub fn render_findings(
    source: &str,
    word_count: usize,
    diagnostic_count: usize,
    diagnostics: impl IntoIterator<Item = (impl AsRef<str>, usize, impl AsRef<str>)>,
    github: bool,
) -> String {
    let mut lines = Vec::new();
    if !github {
        lines.push(format!("Source: {source}"));
    }
    let mut shown = 0;
    for (code, line, message) in diagnostics.into_iter().take(if github { 50 } else { 256 }) {
        let code = code.as_ref();
        let message = message.as_ref();
        if github {
            let mut data = format!("description line {line}:");
            if !message.is_empty() {
                data.push(' ');
                data.push_str(message);
            }
            let data = crate::text::bound(&data, 500).replace('%', "%25");
            lines.push(format!("::error title=YAGA {code}::{data}"));
        } else {
            lines.push(format!("FAILED [{code}] line {line}: {message}"));
        }
        shown += 1;
    }
    let omitted = diagnostic_count.saturating_sub(shown);
    if omitted > 0 {
        lines.push(format!("{omitted} additional finding(s) omitted."));
    }
    let status = if diagnostic_count == 0 {
        "passed"
    } else {
        "failed"
    };
    lines.push(format!(
        "PR description: {status}; {word_count} words; {diagnostic_count} finding(s)."
    ));
    lines.join("\n")
}
