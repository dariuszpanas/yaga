use regex::Regex;
use std::{collections::HashMap, ffi::OsString, path::Path, process::ExitCode, sync::LazyLock};

const LIMIT: usize = 65_536;
static MODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9-]{0,31}$").unwrap());
static GROUP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^YAGA repository check:\s*(\S+)\s*\(([^)]+)\)\s*$").unwrap());
static ANNOTATION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^::(error|warning|notice)(?:\s+([^:]*))?::(.*)$").unwrap());
const HEADLINES: [&str; 4] = [
    "YAGA repository checks: ",
    "YAGA branch policy: ",
    "YAGA change policy: ",
    "PR description: ",
];

fn unescape(value: &str, property: bool) -> String {
    let mut text = value
        .replace("%0D", "")
        .replace("%0d", "")
        .replace("%0A", " · ")
        .replace("%0a", " · ");
    if property {
        text = text
            .replace("%3A", ":")
            .replace("%3a", ":")
            .replace("%2C", ",")
            .replace("%2c", ",");
    }
    text.replace("%25", "%")
}
fn clean(value: &str) -> String {
    value
        .chars()
        .filter_map(|c| match c {
            '\r' | '\n' | '\t' => Some(' '),
            c if c.is_control() => None,
            c => Some(c),
        })
        .collect()
}
fn html(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}
fn cell(value: &str, max: usize, table: bool, code: bool) -> String {
    let mut text = clean(value)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if text.chars().count() > max {
        text = text
            .chars()
            .take(max - 1)
            .collect::<String>()
            .trim_end()
            .to_owned()
            + "…";
    }
    text = html(&text).replace('`', "&#96;");
    if !code {
        for c in ['\\', '*', '_', '[', ']', '~'] {
            text = text.replace(c, &format!("\\{c}"));
        }
    }
    if table {
        text = text.replace('|', "\\|");
    }
    if code { format!("`{text}`") } else { text }
}
fn truncate(value: &str, bytes: usize) -> &str {
    if value.len() > bytes {
        value[..value.floor_char_boundary(bytes)].trim_end()
    } else {
        value
    }
}
type Finding = (String, String, String, String);
struct Provider {
    name: String,
    result: String,
    highlight: String,
    lines: Vec<String>,
}
impl Provider {
    fn new(title: &str) -> Self {
        let (name, result) = GROUP
            .captures(title)
            .map(|c| (c[1].to_owned(), c[2].to_owned()))
            .unwrap_or_else(|| {
                (
                    if title.is_empty() { "check" } else { title }.into(),
                    "info".into(),
                )
            });
        Self {
            name,
            result,
            highlight: "—".into(),
            lines: Vec::new(),
        }
    }
    fn add(&mut self, raw: &str) {
        let line = raw.trim();
        if !line.is_empty() && !line.starts_with("Config: ") {
            self.highlight = [
                "Mode policy: ",
                "Path policy: ",
                "Size policy: ",
                "Tree policy: ",
            ]
            .iter()
            .find_map(|pre| line.strip_prefix(pre))
            .unwrap_or(line)
            .to_owned();
        } else if !line.is_empty()
            && self
                .lines
                .iter()
                .all(|s| s.trim().is_empty() || s.trim().starts_with("Config: "))
        {
            self.highlight = line.to_owned();
        }
        self.lines.push(raw.to_owned());
    }
}

fn render(mode: &str, status: i32, lines: &[String]) -> String {
    let mode = if MODE.is_match(mode) { mode } else { "action" };
    let mut providers = Vec::new();
    let mut group: Option<Provider> = None;
    let mut plain = Vec::new();
    let mut findings: Vec<Finding> = Vec::new();
    let mut notice = None;
    for line in lines {
        if let Some(title) = line.strip_prefix("::group::") {
            if let Some(old) = group.replace(Provider::new(title.trim())) {
                providers.push(old);
            }
        } else if let Some(gr) = group.as_mut() {
            if line.trim() == "::endgroup::" {
                providers.push(group.take().unwrap());
            } else {
                gr.add(line);
            }
        } else if let Some(c) = ANNOTATION.captures(line) {
            let msg = unescape(c[3].trim(), false);
            if &c[1] == "notice" {
                if !msg.is_empty() {
                    notice = Some(msg);
                }
                continue;
            }
            let props: HashMap<_, _> = c
                .get(2)
                .into_iter()
                .flat_map(|m| m.as_str().split(','))
                .filter_map(|p| p.split_once('='))
                .map(|(k, v)| (k.trim(), unescape(v.trim(), true)))
                .collect();
            let prop = |key| props.get(key).map(|s| s.trim()).unwrap_or("");
            let scope = prop("title")
                .strip_prefix("YAGA ")
                .unwrap_or(prop("title"))
                .trim();
            let (file, row, col) = (prop("file"), prop("line"), prop("col"));
            let location = match (file, row, col) {
                ("", "", _) => "—".into(),
                ("", row, _) => format!("line {row}"),
                (file, "", _) => file.into(),
                (file, row, "") => format!("{file}:{row}"),
                _ => format!("{file}:{row}:{col}"),
            };
            findings.push((
                if &c[1] == "error" { "Error" } else { "Warning" }.into(),
                if scope.is_empty() { "policy" } else { scope }.into(),
                location,
                msg,
            ));
        } else if !line.trim().is_empty() {
            plain.push(line.trim().to_owned());
        }
    }
    if let Some(g) = group {
        providers.push(g);
    }
    let has_plain = !plain.is_empty();
    let headline = if has_plain {
        let n = plain
            .iter()
            .position(|s| {
                HEADLINES.iter().any(|p| s.starts_with(p))
                    || s.starts_with("YAGA checked pull request ")
            })
            .unwrap_or(0);
        let s = plain.remove(n);
        Some(
            HEADLINES
                .iter()
                .find_map(|p| s.strip_prefix(p))
                .unwrap_or(&s)
                .to_owned(),
        )
    } else {
        notice.or_else(|| (findings.len() == 1).then(|| findings[0].3.clone()))
    };
    let status_text = match status {
        0 => "Passed",
        1 if mode == "gate" => "Failed",
        1 => "Findings reported",
        _ => "Error",
    };
    let mut line = format!(
        "**Status:** {status_text} (exit `{status}`) · **Version:** `{}`",
        env!("CARGO_PKG_VERSION")
    );
    if let Some(h) = headline.filter(|v| !v.is_empty()) {
        line.push_str(&format!(" · **Summary:** {}", cell(&h, 300, false, false)));
    }
    let mut parts = vec![
        format!("## `>_` YAGA Checks · `{mode}`"),
        String::new(),
        line,
        String::new(),
    ];
    if !providers.is_empty() {
        parts.extend([
            "| Provider | Result | Highlights |".into(),
            "| :--- | :--- | :--- |".into(),
        ]);
        for p in &providers {
            let badge = match p.result.trim().to_ascii_lowercase().as_str() {
                "passed" => "**Passed**".into(),
                "failed" => "**Failed**".into(),
                "error" => "**Error**".into(),
                "skipped" => "Skipped".into(),
                _ => format!("**{}**", cell(&p.result, 32, true, false)),
            };
            parts.push(format!(
                "| {} | {badge} | {} |",
                cell(&p.name, 40, true, true),
                cell(&p.highlight, 240, true, false)
            ));
        }
        parts.push(String::new());
    }
    if !findings.is_empty() && !(findings.len() == 1 && providers.is_empty() && !has_plain) {
        parts.extend([
            "| Level | Rule / Scope | Location | Message |".into(),
            "| :--- | :--- | :--- | :--- |".into(),
        ]);
        for (level, scope, location, body) in findings.iter().take(50) {
            let loc = if location == "—" {
                location.clone()
            } else {
                cell(location, 96, true, true)
            };
            parts.push(format!(
                "| **{level}** | {} | {loc} | {} |",
                cell(scope, 64, true, true),
                cell(body, 240, true, false)
            ));
        }
        if findings.len() > 50 {
            parts.push(format!(
                "\n*{} additional finding(s) omitted from summary table.*",
                findings.len() - 50
            ));
        }
        parts.push(String::new());
    }
    let mut blocks = Vec::new();
    for p in &providers {
        let mut lines = vec![html(&clean(&format!("[{} · {}]", p.name, p.result)))];
        let body = p
            .lines
            .iter()
            .map(|s| html(&clean(s)))
            .collect::<Vec<_>>()
            .join("\n");
        if !body.is_empty() {
            lines.push(body);
        }
        blocks.push(lines.join("\n"));
    }
    if !plain.is_empty() {
        blocks.push(
            plain
                .iter()
                .map(|s| html(&clean(s)))
                .collect::<Vec<_>>()
                .join("\n"),
        );
    }
    let base = parts.join("\n");
    if !blocks.is_empty() {
        let title = if providers.is_empty() {
            "Additional details".into()
        } else {
            format!("Detailed provider output ({})", providers.len())
        };
        let open = if status != 0 && providers.is_empty() && findings.is_empty() {
            " open"
        } else {
            ""
        };
        let prefix =
            format!("{base}\n<details{open}>\n<summary><b>{title}</b></summary>\n\n<pre>\n");
        let suffix = "\n</pre>\n\n</details>\n";
        if let Some(bytes) = LIMIT
            .checked_sub(prefix.len() + suffix.len())
            .filter(|n| *n > 64)
        {
            return format!("{prefix}{}{suffix}", truncate(&blocks.join("\n\n"), bytes));
        }
    }
    truncate(&(base + "\n"), LIMIT).into()
}
pub(super) fn run(args: &[OsString]) -> ExitCode {
    let invalid = || {
        eprintln!("YAGA failed: action summary arguments are invalid");
        ExitCode::from(2)
    };
    let [m, mode, s, status, r, report, e, errors] = args else {
        return invalid();
    };
    if m != "--mode" || s != "--status" || r != "--report" || e != "--error" {
        return invalid();
    }
    let Some(status) = status.to_str().and_then(|s| s.parse().ok()) else {
        return invalid();
    };
    let mut lines = Vec::new();
    for path in [report, errors] {
        if let Ok(raw) = yaga_core::fs::read_regular_prefix(Path::new(path), 262_144) {
            let text =
                String::from_utf8_lossy(&raw[..raw.len().min(262_144)]).replace("\r\n", "\n");
            let mut stream = super::cli::split_lines(&text).peekable();
            while let Some(line) = stream.next() {
                if !line.is_empty() || stream.peek().is_some() {
                    lines.push(line.to_owned());
                }
            }
        }
    }
    print!("{}", render(mode.to_str().unwrap_or(""), status, &lines));
    ExitCode::SUCCESS
}
