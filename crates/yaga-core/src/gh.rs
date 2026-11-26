//! Exact GitHub PR selectors and bounded authenticated gh transport.
use crate::{
    git::process::{ProcessError, run_bounded},
    json::strict_value,
};
use regex::Regex;
use serde_json::Value;
use std::{
    collections::HashSet, env, path::PathBuf, process::Command, sync::LazyLock, time::Duration,
};
#[derive(Clone, Debug)]
pub struct PullRequest {
    host: String,
    owner: String,
    repo: String,
    number: i64,
}
impl PullRequest {
    fn new(host: &str, owner: &str, repo: &str, number: i64) -> Result<Self, String> {
        static HOST: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$").unwrap()
        });
        static OWNER: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$").unwrap());
        static REPO: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_.-]{1,100}$").unwrap());
        if host.len() > 253
            || !host.split('.').all(|s| HOST.is_match(s))
            || !OWNER.is_match(owner)
            || !REPO.is_match(repo)
            || matches!(repo, "." | "..")
        {
            return Err("pull request host, owner, or repository has an unsupported format".into());
        }
        Ok(Self {
            host: host.to_ascii_lowercase(),
            owner: owner.into(),
            repo: repo.into(),
            number,
        })
    }
    fn selector(v: &str, label: &str) -> Result<(), String> {
        if !(1..=1024).contains(&v.len()) || !v.bytes().all(|b| matches!(b, 33..=126)) {
            return Err(format!(
                "{label} must be bounded printable ASCII without whitespace"
            ));
        }
        Ok(())
    }
    pub fn parse(value: &str, repository: Option<&str>) -> Result<Self, String> {
        Self::selector(value, "--pr")?;
        static NUMBER: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[1-9][0-9]{0,18}$").unwrap());
        let number = |s: &str| {
            s.parse()
                .map_err(|_| "pull request number exceeds the supported positive integer range")
        };
        if value.bytes().all(|b| b.is_ascii_digit()) {
            if !NUMBER.is_match(value) {
                return Err(
                    "--pr number must be positive ASCII digits without leading zeroes".into(),
                );
            }
            let n = number(value)?;
            let repo = repository.ok_or("a numeric --pr requires --repo [HOST/]OWNER/REPO")?;
            Self::selector(repo, "--repo")?;
            return match repo.split('/').collect::<Vec<_>>().as_slice() {
                [owner, name] => Self::new("github.com", owner, name, n),
                [host, owner, name] => Self::new(host, owner, name, n),
                _ => Err("--repo must use [HOST/]OWNER/REPO".into()),
            };
        }
        if repository.is_some() {
            return Err("--repo is supported only with a numeric --pr, not a PR URL".into());
        }
        static URL: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(
                r"^(?i:https)://([^/@:\[\]?#%]+)/([^/?#%]+)/([^/?#%]+)/pull/([1-9][0-9]{0,18})/?$",
            )
            .unwrap()
        });
        let fields=URL.captures(value).ok_or("--pr must be a positive ASCII number with --repo, or an HTTPS HOST/OWNER/REPO/pull/NUMBER URL without credentials, ports, query or fragment")?;
        Self::new(&fields[1], &fields[2], &fields[3], number(&fields[4])?)
    }
    pub fn repository(&self) -> String {
        format!("{}/{}/{}", self.host, self.owner, self.repo)
    }
    pub fn url(&self) -> String {
        format!("https://{}/pull/{}", self.repository(), self.number)
    }
    pub fn number(&self) -> i64 {
        self.number
    }
    pub fn host(&self) -> &str {
        &self.host
    }
    pub fn owner(&self) -> &str {
        &self.owner
    }
    pub fn name(&self) -> &str {
        &self.repo
    }
}
pub(crate) fn gh_executable() -> Result<PathBuf, String> {
    let invalid = "gh must resolve to an absolute regular executable (gh.exe on Windows) installed on PATH outside the current directory";
    let cwd = env::current_dir()
        .and_then(|p| p.canonicalize())
        .map_err(|_| invalid)?;
    let lookup = env::var_os("PATH").unwrap_or_default();
    for dir in env::split_paths(&lookup) {
        let path = dir.join(if cfg!(windows) { "gh.exe" } else { "gh" });
        let Ok(info) = path.metadata() else { continue };
        if !info.is_file() {
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if info.permissions().mode() & 0o111 == 0 {
                continue;
            }
        }
        let resolved = path.canonicalize().map_err(|_| invalid)?;
        if !path.is_absolute()
            || resolved.parent() == Some(&cwd)
            || dir.canonicalize().map_err(|_| invalid)? == cwd
        {
            return Err(invalid.into());
        }
        #[cfg(windows)]
        if !resolved
            .extension()
            .is_some_and(|s| s.eq_ignore_ascii_case("exe"))
        {
            return Err(invalid.into());
        }
        return Ok(resolved);
    }
    Err("checking --pr requires the GitHub CLI (gh); install gh and authenticate".into())
}

pub(crate) fn configure(command: &mut Command) {
    let excluded: HashSet<_> = ["GH_DEBUG", "DEBUG", "GH_FORCE_TTY", "GH_HOST", "GH_REPO"]
        .into_iter()
        .collect();
    for (key, _) in env::vars_os() {
        if excluded.contains(key.to_string_lossy().to_ascii_uppercase().as_str()) {
            command.env_remove(key);
        }
    }
    command.envs([
        ("GH_PROMPT_DISABLED", "1"),
        ("GH_NO_UPDATE_NOTIFIER", "1"),
        ("GH_NO_EXTENSION_UPDATE_NOTIFIER", "1"),
        ("GH_PAGER", ""),
        ("PAGER", ""),
        ("GH_TELEMETRY", "false"),
        ("NO_COLOR", "1"),
        ("CLICOLOR", "0"),
        ("CLICOLOR_FORCE", "0"),
    ]);
}

const MAX_GH_STDOUT_BYTES: usize = 512 * 1024;
const MAX_GH_STDERR_BYTES: usize = 64 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Context {
    Review,
    Triage,
}

impl Context {
    fn operation(self, is_write: bool) -> &'static str {
        match (self, is_write) {
            (Self::Review, true) => "pull-request review submission",
            (Self::Review, false) => "pull-request review",
            (Self::Triage, true) => "pull-request triage comment publication",
            (Self::Triage, false) => "pull-request triage",
        }
    }

    fn process_error(self, is_write: bool, err: ProcessError) -> String {
        let operation = self.operation(is_write);
        match err {
            ProcessError::Timeout => format!("gh exceeded the 30-second {operation} timeout"),
            ProcessError::StdoutLimit | ProcessError::StderrLimit => {
                format!("gh exceeded the bounded {operation} output limit")
            }
            _ => match (self, is_write) {
                (Self::Review, false) => {
                    "gh could not execute its bounded pull-request review request".into()
                }
                (Self::Review, true) => {
                    "gh could not execute its bounded pull-request review submission".into()
                }
                (Self::Triage, false) => {
                    "gh could not execute its bounded pull-request triage request".into()
                }
                (Self::Triage, true) => {
                    "gh could not execute its bounded pull-request triage comment publication"
                        .into()
                }
            },
        }
    }

    fn status_error(self, is_write: bool) -> &'static str {
        match (self, is_write) {
            (Self::Review, true) => {
                "gh could not submit the pull-request review; check gh authentication, pull-requests: write permission, and repository Actions approval settings"
            }
            (Self::Review, false) => {
                "gh could not fetch the pull-request review state; check gh authentication, repository access, PR existence, and network connectivity"
            }
            (Self::Triage, true) => {
                "gh could not publish the pull-request triage comment; check gh authentication and issues: write / pull-requests: write permission"
            }
            (Self::Triage, false) => {
                "gh could not fetch the pull-request triage state; check gh authentication, repository access, PR existence, and network connectivity"
            }
        }
    }
}

pub(crate) struct Client {
    executable: PathBuf,
    pub reference: PullRequest,
    context: Context,
}
impl Client {
    pub fn open(reference: PullRequest, context: Context) -> Result<Self, String> {
        Ok(Self {
            executable: gh_executable()?,
            reference,
            context,
        })
    }
    pub fn get(&self, endpoint: &str) -> Result<Value, String> {
        self.request(endpoint, None, &[], MAX_GH_STDOUT_BYTES)
    }
    pub fn get_with_stdout_limit(
        &self,
        endpoint: &str,
        stdout_limit: usize,
    ) -> Result<Value, String> {
        self.request(endpoint, None, &[], stdout_limit)
    }
    pub fn write(
        &self,
        endpoint: &str,
        method: &str,
        fields: &[(&str, &str)],
    ) -> Result<Value, String> {
        if !matches!(method, "POST" | "PATCH") {
            return Err("unsupported bounded GitHub write method".into());
        }
        self.request(endpoint, Some(method), fields, MAX_GH_STDOUT_BYTES)
    }
    fn request(
        &self,
        endpoint: &str,
        method: Option<&str>,
        fields: &[(&str, &str)],
        stdout_limit: usize,
    ) -> Result<Value, String> {
        let prefix = format!("repos/{}/{}/", self.reference.owner, self.reference.repo);
        if (!(method.is_none() && endpoint == "user") && !endpoint.starts_with(&prefix))
            || endpoint.len() > 1024
            || endpoint.split('/').any(|part| part == "..")
            || !endpoint.is_ascii()
        {
            return Err("invalid bounded GitHub API endpoint".into());
        }
        let mut command = Command::new(&self.executable);
        command.args(["api", "--hostname", self.reference.host()]);
        if let Some(m) = method {
            command.args(["--method", m]);
        }
        command.args([
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]);
        for &(key, value) in fields {
            command.args(["-f", &format!("{key}={value}")]);
        }
        command.arg(endpoint);
        configure(&mut command);
        let is_write = method.is_some();
        let out = run_bounded(
            command,
            None,
            stdout_limit,
            MAX_GH_STDERR_BYTES,
            Duration::from_secs(30),
        )
        .map_err(|e| self.context.process_error(is_write, e))?;
        if !out.status.success() {
            return Err(self.context.status_error(is_write).into());
        }
        strict_value(&out.stdout).map_err(|_| "gh returned invalid or ambiguous UTF-8 JSON".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn context_errors_distinguish_review_and_triage_operations() {
        assert!(
            Context::Review
                .process_error(false, ProcessError::Timeout)
                .contains("pull-request review timeout")
        );
        assert!(
            Context::Review
                .status_error(true)
                .contains("submit the pull-request review")
        );
        assert!(
            Context::Triage
                .process_error(false, ProcessError::Timeout)
                .contains("pull-request triage timeout")
        );
        assert!(
            Context::Triage
                .process_error(true, ProcessError::StdoutLimit)
                .contains("pull-request triage comment publication output limit")
        );
        assert!(
            Context::Triage
                .status_error(false)
                .contains("fetch the pull-request triage state")
        );
        assert!(
            Context::Triage
                .status_error(true)
                .contains("publish the pull-request triage comment")
        );
        assert!(!Context::Triage.status_error(false).contains("review"));
        assert!(!Context::Triage.status_error(true).contains("review"));
    }
}
