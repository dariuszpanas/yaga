use super::{Failure, Format, P};
use crate::trees::ChildReport;
use serde_json::{Value, json};
use yaga_core::text::{github_data, safe_text};

type Check = (P, Result<ChildReport, Failure>);

fn status(result: &Result<ChildReport, Failure>) -> &'static str {
    match result {
        Err(_) => "error",
        Ok(child) if child.document["valid"] == true => "passed",
        _ => "failed",
    }
}
fn pretty(value: &Value) -> String {
    serde_json::to_string_pretty(value).expect("reports contain only JSON data")
}

pub(super) fn error(kind: &str, msg: &str, format: Format) -> String {
    let message = safe_text(msg, 500);
    let text = format!("YAGA {kind} error: {message}");
    match format {
        Format::Text => text,
        Format::Github => format!(
            "::error title=YAGA repository check::{}",
            github_data(&text, 500)
        ),
        Format::Json => pretty(
            &json!({"schema_version":1,"kind":"repository_check","status":"error","valid":false,"error":{"kind":kind,"message":message}}),
        ),
    }
}

fn diagnostics(provider: P, child: &ChildReport) -> usize {
    let d = &child.document;
    match provider {
        P::Mode | P::Path => d["counts"]["findings"]
            .as_u64()
            .expect("exact mode/path count") as usize,
        P::Size | P::Tree => d["diagnostics"]
            .as_array()
            .expect("provider diagnostics")
            .len(),
        _ => {
            let entries = if provider == P::Commit {
                "commits"
            } else {
                "workflows"
            };
            d[entries]
                .as_array()
                .expect("provider results")
                .iter()
                .map(|r| {
                    r["diagnostics"]
                        .as_array()
                        .expect("result diagnostics")
                        .len()
                })
                .sum()
        }
    }
}

pub(super) fn render(checks: &[Check], format: Format) -> (u8, String) {
    let selected = checks.len();
    let failed = checks.iter().filter(|(_, r)| status(r) == "failed").count();
    let errored = checks.iter().filter(|(_, r)| r.is_err()).count();
    let passed = selected - failed - errored;
    let (exit, overall) = if errored > 0 {
        (2, "error")
    } else if failed > 0 {
        (1, "failed")
    } else {
        (0, "passed")
    };
    if matches!(format, Format::Json) {
        let results: Vec<_> = checks
            .iter()
            .map(|(p, outcome)| {
                let (err, report) = match outcome {
                    Ok(child) => (Value::Null, &child.document),
                    Err((kind, msg)) => (
                        json!({"kind":kind,"message":safe_text(msg,500)}),
                        &Value::Null,
                    ),
                };
                json!({"provider":p.name(),"status":status(outcome),"report":report,"error":err})
            })
            .collect();
        return (
            exit,
            pretty(
                &json!({"schema_version":1,"kind":"repository_check","status":overall,"valid":exit==0,"selected":selected,
            "passed":passed,"failed":failed,"errored":errored,"checks":results}),
            ),
        );
    }
    let github = matches!(format, Format::Github);
    let mut lines = Vec::new();
    for (p, result) in checks {
        let state = status(result);
        let name = p.name();
        lines.push(if github {
            format!("::group::YAGA repository check: {name} ({state})")
        } else {
            format!("== {name} [{}] ==", state.to_uppercase())
        });
        lines.push(match result {
            Ok(child) => child.text.clone(),
            Err((kind, msg)) => error(kind, msg, Format::Text),
        });
        if github {
            lines.push("::endgroup::".into());
        }
    }
    if github {
        let total = checks
            .iter()
            .map(|(p, r)| r.as_ref().map_or(1, |c| diagnostics(*p, c)))
            .sum::<usize>();
        let limit = if total > 50 { 49 } else { total };
        let mut visible = 0;
        for (p, result) in checks {
            if visible == limit {
                break;
            }
            match result {
                Ok(child) => {
                    for line in child.annotations.iter().take(limit - visible) {
                        lines.push(line.clone());
                        visible += 1;
                    }
                }
                Err((kind, msg)) => {
                    let text = format!("YAGA {kind} error: {}", safe_text(msg, 1000));
                    lines.push(format!(
                        "::error title=YAGA {}::{}",
                        p.name(),
                        github_data(&text, 500)
                    ));
                    visible += 1;
                }
            }
        }
        if total > visible {
            let severity = if exit == 0 { "warning" } else { "error" };
            lines.push(format!("::{severity} title=YAGA repository check::{} additional repository diagnostic(s) omitted",total-visible));
        }
    }
    let prefix = if github {
        "YAGA repository checks"
    } else {
        "Repository checks"
    };
    lines.push(format!(
        "{prefix}: {selected} selected; {passed} passed, {failed} failed, {errored} errored."
    ));
    (exit, lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn global_annotation_limit_and_error_precedence() {
        let mode = ChildReport {
            document: json!({"valid":false,"counts":{"findings":64}}),
            text: "mode details".into(),
            annotations: (0..50)
                .map(|n| format!("::error title=YAGA mode::{n}"))
                .collect(),
        };
        let checks = vec![
            (P::Workflow, Err(("input", "missing workflow".into()))),
            (P::Mode, Ok(mode)),
        ];
        let (exit, output) = render(&checks, Format::Github);
        assert_eq!(exit, 2);
        assert_eq!(
            output
                .lines()
                .filter(|line| line.starts_with("::error "))
                .count(),
            50
        );
        assert!(output.contains("16 additional repository diagnostic(s) omitted"));
        let doc: Value = serde_json::from_str(&render(&checks, Format::Json).1).unwrap();
        assert_eq!(doc["status"], "error");
        assert_eq!(doc["failed"], 1);
        assert_eq!(doc["errored"], 1);
        assert!(doc["checks"][0]["report"].is_null());
    }
}
