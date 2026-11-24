//! Closed CLI-provider input selection for the composite Action and its Python adapter.

use std::collections::HashMap;
use std::path::PathBuf;

/// A validated command. Only a live pull-request operation can carry a credential.
/// Keep its explicit token in the child environment, never in arguments or logs.
pub struct Invocation<W = PathBuf> {
    pub arguments: Vec<String>,
    pub workspace: W,
    pub gh_token: Option<String>,
}

pub const INPUTS: &[&str] = &[
    "policy",
    "template",
    "file",
    "pr",
    "repo",
    "wait-seconds",
    "submit-review",
    "triage-policy",
    "publish-comment",
    "branch",
    "range",
    "revision",
    "commit",
    "config",
    "plan",
    "checks",
    "workflow-paths",
    "workflow-security-profile",
    "workflow-security-rules",
    "mode-policy",
    "path-policy",
    "size-policy",
    "tree-policy",
];
pub const NON_CLI: &[&str] = &[
    "trusted-config",
    "gate",
    "operation",
    "prerequisite-workflow",
    "lifecycle-workflow",
    "owner-id",
    "approval-marker",
    "request-timeout",
    "job-timeout-minutes",
    "agent-review-policy-file",
];

pub const MAX_INPUT_BYTES: usize = 65_536;
pub const MAX_LIST_ITEMS: usize = 256;
const COMMITTED_TREE_INPUTS: &[&str] = &["policy", "revision", "repo"];
const WORKFLOW_INPUTS: &[&str] = &["workflow-paths", "repo"];
const CHECKS: &[(&str, &[&str])] = &[
    ("branch", &["policy", "branch"]),
    ("change", &["policy", "range", "repo"]),
    ("mode", COMMITTED_TREE_INPUTS),
    ("path", COMMITTED_TREE_INPUTS),
    ("size", COMMITTED_TREE_INPUTS),
    ("tree", COMMITTED_TREE_INPUTS),
    ("policy", &["policy", "repo"]),
    (
        "pr-description",
        &["policy", "template", "file", "pr", "repo"],
    ),
    (
        "pr-review",
        &[
            "policy",
            "file",
            "pr",
            "repo",
            "wait-seconds",
            "submit-review",
            "triage-policy",
        ],
    ),
    (
        "pr-triage",
        &["policy", "file", "pr", "repo", "publish-comment"],
    ),
    (
        "repo",
        &[
            "plan",
            "checks",
            "repo",
            "config",
            "commit",
            "range",
            "revision",
            "workflow-paths",
            "workflow-security-profile",
            "workflow-security-rules",
            "mode-policy",
            "path-policy",
            "size-policy",
            "tree-policy",
        ],
    ),
    ("workflow", WORKFLOW_INPUTS),
    ("workflow-lint", WORKFLOW_INPUTS),
    (
        "workflow-security",
        &[
            "workflow-paths",
            "repo",
            "workflow-security-profile",
            "workflow-security-rules",
        ],
    ),
];

pub fn checks() -> impl Iterator<Item = &'static str> {
    CHECKS.iter().map(|&(check, _)| check)
}

fn input(
    get: &mut impl FnMut(&str, Option<usize>) -> Option<(String, usize)>,
    name: &str,
) -> Result<String, String> {
    let key = format!("YAGA_INPUT_{}", name.to_ascii_uppercase().replace('-', "_"));
    let (raw, bytes) = get(&key, Some(MAX_INPUT_BYTES)).unwrap_or_default();
    if raw.contains('\0') {
        return Err(format!("action input '{name}' contains a NUL byte"));
    }
    if bytes > MAX_INPUT_BYTES {
        return Err(format!("action input '{name}' exceeds 64 KiB"));
    }
    Ok(trim(&raw).to_owned())
}

pub fn split_lines(value: &str) -> impl Iterator<Item = &str> {
    value.split([
        '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
        '\u{2029}',
    ])
}

fn list<'a>(value: &'a str, name: &str, paths: bool) -> Result<Vec<&'a str>, String> {
    let mut values = vec![];
    let noun = if paths { "entries" } else { "items" };
    for line in split_lines(value).map(trim).filter(|v| !v.is_empty()) {
        // Newline-separated paths can contain a literal comma or internal whitespace.
        for item in line.split(|ch| !paths && ch == ',').map(trim) {
            if item.is_empty() {
                return Err(format!("action input '{name}' contains an empty list item"));
            }
            if !paths && item.chars().any(whitespace) {
                return Err(format!(
                    "action input '{name}' items must not contain whitespace"
                ));
            }
            if item.starts_with('-') {
                return Err(format!(
                    "action input '{name}' {noun} must not start with '-'"
                ));
            }
            values.push(item);
            if values.len() > MAX_LIST_ITEMS {
                return Err(format!(
                    "action input '{name}' exceeds {MAX_LIST_ITEMS} {noun}"
                ));
            }
        }
    }
    Ok(values)
}

// Python's str.strip and str.isspace also recognize the ASCII information separators.
fn whitespace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

fn trim(value: &str) -> &str {
    value.trim_matches(whitespace)
}

struct Inputs<'a> {
    check: &'a str,
    values: HashMap<&'static str, String>,
}
impl Inputs<'_> {
    fn get(&self, name: &str) -> &str {
        &self.values[name]
    }
    fn require(&self, name: &str) -> Result<(), String> {
        if self.get(name).is_empty() {
            return Err(format!("check '{}' requires input '{name}'", self.check));
        }
        Ok(())
    }
    fn option(&self, args: &mut Vec<String>, name: &str, option: &str) {
        let value = self.get(name);
        if !value.is_empty() {
            args.push(format!("--{option}={value}"));
        }
    }
    fn options(&self, args: &mut Vec<String>, names: &[&str]) {
        for name in names {
            self.option(args, name, name);
        }
    }
    fn pr(&self, event: &str) -> Result<Vec<String>, String> {
        self.require("policy")?;
        let mode = self.check.strip_prefix("pr-").unwrap();
        let file = !self.get("file").is_empty();
        let pr = !self.get("pr").is_empty();
        let mut args = vec!["pull-request".into(), mode.into(), "check".into()];
        self.options(&mut args, &["policy"]);
        match mode {
            "description" => {
                if file && pr {
                    return Err(
                        "check 'pr-description' accepts at most one of 'file' or 'pr'".into(),
                    );
                }
                if !pr && !self.get("repo").is_empty() {
                    return Err("check 'pr-description' accepts 'repo' only with 'pr'".into());
                }
                if !file && !pr {
                    if trim(event).is_empty() || event.contains('\0') {
                        return Err(
                            "GITHUB_EVENT_PATH is required for event-based 'pr-description' checks"
                                .into(),
                        );
                    }
                    args.insert(0, "github".into());
                    args.push(format!("--event-file={}", trim(event)));
                }
                self.options(&mut args, &["file", "pr", "repo", "template"]);
            }
            "triage" => {
                let publish = self.get("publish-comment");
                if !matches!(publish, "" | "true" | "false") {
                    return Err("check 'pr-triage' publish-comment must be true or false".into());
                }
                if file == pr {
                    return Err("check 'pr-triage' requires exactly one of 'file' or 'pr'".into());
                }
                if file && (!self.get("repo").is_empty() || publish == "true") {
                    return Err(
                        "check 'pr-triage' offline file cannot use repo or publish-comment".into(),
                    );
                }
                self.options(&mut args, &["file", "pr", "repo"]);
                if publish == "true" {
                    args.push("--publish-comment".into());
                }
            }
            "review" => {
                if file == pr {
                    return Err("check 'pr-review' requires exactly one of 'file' or 'pr'".into());
                }
                if file {
                    for name in ["repo", "wait-seconds", "submit-review", "triage-policy"] {
                        if !self.get(name).is_empty() {
                            return Err(format!(
                                "check 'pr-review' with 'file' does not accept input '{name}'"
                            ));
                        }
                    }
                }
                let submission = self.get("submit-review").to_ascii_lowercase();
                let triage = !self.get("triage-policy").is_empty();
                if !triage && matches!(submission.as_str(), "approve" | "both") {
                    return Err("check 'pr-review' with positive submit-review requires input 'triage-policy'".into());
                }
                if triage && matches!(submission.as_str(), "" | "none") {
                    return Err(
                        "check 'pr-review' input 'triage-policy' requires 'submit-review'".into(),
                    );
                }
                self.options(
                    &mut args,
                    &[
                        "file",
                        "pr",
                        "repo",
                        "wait-seconds",
                        "submit-review",
                        "triage-policy",
                    ],
                );
            }
            _ => unreachable!(),
        }
        args.push("--format=github".into());
        Ok(args)
    }
    fn arguments(&self, event: &str) -> Result<Vec<String>, String> {
        let check = self.check;
        if check.starts_with("pr-") {
            return self.pr(event);
        }
        let mut args = vec![check.to_owned(), "check".to_owned()];
        match check {
            "policy" => {
                self.options(&mut args, &["repo"]);
                args.extend(["--strict".into(), "--format=github".into()]);
                if !self.get("policy").is_empty() {
                    args.extend(["--".into(), self.get("policy").into()]);
                }
                return Ok(args);
            }
            "workflow" | "workflow-security" | "workflow-lint" => {
                args[0] = "workflow".into();
                args[1] = check.strip_prefix("workflow-").unwrap_or("check").into();
                let paths = list(self.get("workflow-paths"), "workflow-paths", true)?;
                self.options(&mut args, &["repo"]);
                self.option(&mut args, "workflow-security-profile", "profile");
                for rule in list(
                    self.get("workflow-security-rules"),
                    "workflow-security-rules",
                    false,
                )? {
                    args.push(format!("--rule={rule}"));
                }
                args.push("--format=github".into());
                if !paths.is_empty() {
                    args.push("--".into());
                    args.extend(paths.into_iter().map(String::from));
                }
                return Ok(args);
            }
            "repo" => {
                let checks = list(self.get("checks"), "checks", false)?;
                let paths = list(self.get("workflow-paths"), "workflow-paths", true)?;
                let rules = list(
                    self.get("workflow-security-rules"),
                    "workflow-security-rules",
                    false,
                )?;
                if checks.is_empty() && self.get("plan").is_empty() {
                    return Err("check 'repo' requires 'plan' or 'checks'".into());
                }
                self.options(&mut args, &["plan"]);
                for check in checks {
                    args.push(format!("--check={check}"));
                }
                self.options(
                    &mut args,
                    &["repo", "config", "commit", "range", "revision"],
                );
                for path in paths {
                    args.push(format!("--workflow-path={path}"));
                }
                self.options(&mut args, &["workflow-security-profile"]);
                for rule in rules {
                    args.push(format!("--workflow-security-rule={rule}"));
                }
                self.options(
                    &mut args,
                    &["mode-policy", "path-policy", "size-policy", "tree-policy"],
                );
            }
            _ => {
                self.require("policy")?;
                self.options(&mut args, &["policy"]);
                let name = match check {
                    "branch" => "branch",
                    "change" => "range",
                    _ => "revision",
                };
                self.require(name)?;
                self.option(
                    &mut args,
                    name,
                    if name == "branch" { "name" } else { name },
                );
                self.options(&mut args, &["repo"]);
            }
        }
        args.push("--format=github".into());
        Ok(args)
    }
}

/// Validate the inputs of one installed-CLI Action before allowing a child process.
pub fn prepare(mut get: impl FnMut(&str) -> Option<String>) -> Result<Invocation, String> {
    prepare_with_host(
        |key, _| {
            get(key).map(|value| {
                let bytes = value.len();
                (value, bytes)
            })
        },
        |raw| {
            let path = PathBuf::from(raw);
            path.is_dir().then_some(path)
        },
    )
}

/// Allow a host to preserve its native strings, byte counts and filesystem lookup.
/// Projected strings must preserve whitespace, ASCII syntax and NUL. For an
/// oversized value, an over-limit byte count and its NUL presence suffice.
#[doc(hidden)]
pub fn prepare_with_host<W>(
    mut get: impl FnMut(&str, Option<usize>) -> Option<(String, usize)>,
    directory: impl FnOnce(&str) -> Option<W>,
) -> Result<Invocation<W>, String> {
    let check = input(&mut get, "check")?;
    let (_, permitted) = CHECKS
        .iter()
        .find(|(name, _)| *name == check)
        .ok_or("action check mode is invalid")?;
    if get("YAGA_NON_CLI_INPUTS", None).is_some_and(|(v, _)| v != "0") {
        return Err(format!(
            "check '{check}' does not accept commit or gate inputs"
        ));
    }
    for &name in NON_CLI {
        if !input(&mut get, name)?.is_empty() {
            return Err(format!("check '{check}' does not accept input '{name}'"));
        }
    }
    let values = INPUTS
        .iter()
        .map(|&name| Ok((name, input(&mut get, name)?)))
        .collect::<Result<_, String>>()?;
    let inputs = Inputs {
        check: &check,
        values,
    };
    for &name in INPUTS {
        if !inputs.get(name).is_empty() && !permitted.contains(&name) {
            return Err(format!("check '{check}' does not accept input '{name}'"));
        }
    }
    let flag = get("YAGA_HAS_GITHUB_TOKEN", None);
    let flag = flag.as_ref().map_or("0", |(v, _)| v.as_str());
    if !matches!(flag, "0" | "1") {
        return Err("action token presence flag is invalid".into());
    }
    let (token, bytes) = get("YAGA_GH_TOKEN", Some(MAX_INPUT_BYTES)).unwrap_or_default();
    if token.contains('\0') || bytes > MAX_INPUT_BYTES {
        return Err("github-token is invalid".into());
    }
    let live = check.starts_with("pr-") && !inputs.get("pr").is_empty();
    if !live && (flag == "1" || !token.is_empty()) {
        return Err(format!(
            "check '{check}' does not accept input 'github-token' in this mode"
        ));
    }
    if live && trim(&token).is_empty() {
        return Err(format!(
            "check '{check}' with 'pr' requires input 'github-token'"
        ));
    }
    let (raw, _) = get("GITHUB_WORKSPACE", None).unwrap_or_default();
    if trim(&raw).is_empty() || raw.contains('\0') {
        return Err("GITHUB_WORKSPACE is required for the composite Action".into());
    }
    let workspace =
        directory(trim(&raw)).ok_or("GITHUB_WORKSPACE must reference an existing directory")?;
    let event = if check == "pr-description" && inputs.get("file").is_empty() && !live {
        get("GITHUB_EVENT_PATH", None).unwrap_or_default().0
    } else {
        String::new()
    };
    Ok(Invocation {
        arguments: inputs.arguments(&event)?,
        workspace,
        gh_token: live.then(|| trim(&token).to_owned()),
    })
}
