use super::{MAX_CHARACTERS, Prediction, split_lines};
use crate::{
    commit_source::CommitTarget,
    commits::{
        self, FooterSyntax, MessageFormat, QualityInputMode, QualityPolicy, QualityProvider,
        QualityTask,
    },
    text::{json_text, safe_text},
};
use serde_json::{Value, json};

pub struct ResultItem {
    pub target: CommitTarget,
    pub prediction: Prediction,
    pub input_characters: usize,
    pub input_character_truncated: bool,
    pub sha: String,
}
pub struct Report {
    pub settings: QualityPolicy,
    pub results: Vec<ResultItem>,
    pub offline: bool,
    pub selected_input_set_sha256: Option<String>,
}

impl ResultItem {
    fn header(&self) -> String {
        safe_text(
            split_lines(&self.target.message)
                .first()
                .copied()
                .unwrap_or(""),
            160,
        )
    }
    fn body_lines(&self) -> usize {
        if let Some(parsed) = commits::parse_message(
            &self.target.message,
            MessageFormat::Conventional,
            FooterSyntax::Conventional,
        ) {
            return parsed.body_lines.len();
        }
        let lines = split_lines(&self.target.message);
        lines
            .iter()
            .position(|s| s.is_empty())
            .map_or(0, |i| lines.len() - i - 1)
    }
    fn char_coverage(&self) -> String {
        format!(
            "{}/{MAX_CHARACTERS} chars{}",
            self.input_characters,
            if self.input_character_truncated {
                ", character-truncated"
            } else {
                ""
            }
        )
    }
    fn token_coverage(&self, limit: Option<usize>) -> Option<(String, &'static str)> {
        self.prediction.input_tokens.zip(limit).map(|(n, m)| {
            (
                format!("{n}/{m}"),
                if n > m { "truncated" } else { "complete" },
            )
        })
    }
}
impl Report {
    pub fn flagged(&self) -> usize {
        self.results.iter().filter(|r| r.prediction.flagged).count()
    }
    pub fn valid(&self) -> bool {
        self.flagged() == 0
    }
    fn max_input_tokens(&self) -> Option<usize> {
        (self.settings.provider == QualityProvider::Huggingface)
            .then_some(self.settings.max_input_tokens)
    }
    fn threshold(&self) -> Option<f64> {
        (!self.settings.provider.requires_explicit_model()
            && self.settings.task == QualityTask::Classification)
            .then_some(self.settings.threshold)
    }
    fn summary(&self) -> String {
        format!(
            "{} commit(s): {} passed, {} flagged.",
            self.results.len(),
            self.results.len() - self.flagged(),
            self.flagged()
        )
    }
    pub fn document(&self) -> Value {
        let q = &self.settings;
        let max = self.max_input_tokens();
        let commits=self.results.iter().map(|r|{
            let target=&r.target; let p=&r.prediction;
            json!({"source":json_text(&target.source,1000),"sha":target.sha,"message":json_text(&target.message,160),
                "message_lines":target.message.bytes().filter(|&b|b==b'\n').count()+1,"message_body_lines":r.body_lines(),
                "model_input_truncated":p.input_tokens.zip(max).map(|(n,m)|n>m),"model_input_tokens":p.input_tokens,
                "model_input_characters":r.input_characters,"model_input_character_truncated":r.input_character_truncated,"selected_input_sha256":r.sha,
                "status":if p.flagged{"flagged"}else{"passed"},"score":p.score,"reason":p.reason.as_deref().map(|s|json_text(s,500)),"threshold":self.threshold()})
        }).collect::<Vec<_>>();
        json!({"schema_version":1,"valid":self.valid(),"checked":self.results.len(),"flagged":self.flagged(),
            "provider":q.provider,"task":q.task,"model":{"id":json_text(&q.model,256),"revision":q.revision},"region":q.region,"offline":self.offline,"max_input_tokens":max,
            "input_mode":q.input_mode,"max_input_characters":MAX_CHARACTERS,"selected_input_set_sha256":self.selected_input_set_sha256,"commits":commits})
    }
    pub fn text(&self) -> String {
        let q = &self.settings;
        let mut lines = Vec::new();
        for r in &self.results {
            let target = &r.target;
            let p = &r.prediction;
            let identity = target
                .sha
                .as_deref()
                .map_or(target.source.as_str(), |s| s.get(..12).unwrap_or(s));
            let n = target.message.bytes().filter(|&b| b == b'\n').count() + 1;
            let c = r.body_lines();
            let body = match c {
                0 => "no body included".into(),
                1 => "1 body line included".into(),
                _ => format!("{c} body lines included"),
            };
            let tokens = r
                .token_coverage(self.max_input_tokens())
                .map_or(String::new(), |(n, state)| {
                    format!("; model input {n} tokens, {state}")
                });
            lines.push(format!(
                "{:7} {}  ({n} {} checked; {body}{tokens}) [model input: {}] model input {} {}",
                if p.flagged { "FLAGGED" } else { "PASSED" },
                safe_text(identity, 80),
                if n == 1 { "line" } else { "lines" },
                if q.input_mode == QualityInputMode::Title {
                    "title only"
                } else {
                    "full message"
                },
                r.char_coverage(),
                r.header()
            ));
            let mut details = vec![];
            if let Some(n) = p.score {
                details.push(format!("score: {n:.3}"))
            }
            if let Some(n) = self.threshold() {
                details.push(format!("threshold: {n:.3}"))
            }
            if let Some(s) = &p.reason {
                details.push(format!("reason: {}", safe_text(s, 500)))
            }
            details.push(format!("input sha256: {}", r.sha));
            lines.push(format!("         {}", details.join("; ")));
        }
        lines.extend([
            format!("Checked {}", self.summary()),
            format!("Provider: {}", q.provider.as_str()),
            format!("Task: {}", q.task.as_str()),
            format!("Model: {}", safe_text(&q.model, 1000)),
        ]);
        if let Some(s) = &q.revision {
            lines.push(format!("Revision: {s}"))
        }
        if let Some(s) = &q.region {
            lines.push(format!("Region: {s}"))
        }
        if self.offline {
            lines.push("Mode: offline".into())
        }
        if let Some(n) = self.max_input_tokens() {
            lines.push(format!("Max input tokens: {n}"))
        }
        lines.push(format!("Input mode: {}", q.input_mode.as_str()));
        lines.push(format!("Max input characters: {MAX_CHARACTERS}"));
        if let Some(s) = &self.selected_input_set_sha256 {
            lines.push(format!("Selected input set sha256: {s}"))
        }
        lines.join("\n")
    }
    pub fn github(&self) -> String {
        let q = &self.settings;
        let mut lines = vec![];
        let count = self.results.len();
        for r in self
            .results
            .iter()
            .filter(|r| r.prediction.flagged)
            .take(50)
        {
            let body = r.body_lines();
            let mut detail = format!(
                "{}: {}; {body} {}; input {}",
                safe_text(&r.target.source, 1000),
                r.header(),
                if body == 1 { "body line" } else { "body lines" },
                r.char_coverage()
            );
            if let Some((n, state)) = r.token_coverage(self.max_input_tokens()) {
                detail.push_str(&format!("; tokens {n}, {state}"))
            }
            if let Some(n) = r.prediction.score {
                detail.push_str(&format!("; score {n:.3}"))
            }
            if let Some(s) = &r.prediction.reason {
                detail.push_str(&format!("; {}", safe_text(s, 500)))
            }
            detail.push_str(&format!("; input sha256 {}", r.sha));
            lines.push(format!(
                "::warning title=YAGA commit quality::{}",
                detail.replace('%', "%25")
            ));
        }
        let omitted = self.flagged().saturating_sub(50);
        if omitted > 0 {
            lines.push(format!(
                "::warning title=YAGA commit quality::{omitted} additional finding(s) omitted"
            ))
        }
        let mut fields = vec![
            format!("Provider: {}", q.provider.as_str()),
            format!("Task: {}", q.task.as_str()),
            format!("Model: {}", safe_text(&q.model, 1000)),
        ];
        if self.offline {
            fields.push("Mode: offline".into())
        }
        fields.push(format!("Input: {}", q.input_mode.as_str()));
        if let Some(s) = &q.revision {
            fields.push(format!("Revision: {s}"))
        }
        if let Some(s) = &q.region {
            fields.push(format!("Region: {s}"))
        }
        if let Some(n) = self.max_input_tokens() {
            fields.push(format!("Max tokens: {n}"))
        }
        fields.push(format!("Max chars: {MAX_CHARACTERS}"));
        if count > 0 {
            let truncated = self
                .results
                .iter()
                .filter(|r| r.input_character_truncated)
                .count();
            fields.push(format!(
                "Character coverage: {} complete, {truncated} truncated, 0 unmeasured",
                count - truncated
            ))
        }
        let tokens: Vec<_> = self
            .results
            .iter()
            .filter_map(|r| r.prediction.input_tokens)
            .collect();
        if !tokens.is_empty() {
            let truncated = tokens
                .iter()
                .filter(|&&n| self.max_input_tokens().is_some_and(|m| n > m))
                .count();
            fields.push(format!(
                "Token coverage: {} measured, {truncated} truncated, {} unmeasured",
                tokens.len() - truncated,
                count - tokens.len()
            ))
        }
        if count > 0
            && let Some(n) = self.threshold()
        {
            fields.push(format!("Threshold: {n:.3}"))
        }
        if let Some(s) = &self.selected_input_set_sha256 {
            fields.push(format!("Input set sha256: {s}"))
        }
        lines.push(format!(
            "::notice title=YAGA commit quality::YAGA quality checked {} {}",
            self.summary(),
            fields.join("; ").replace('%', "%25")
        ));
        lines.join("\n")
    }
}
