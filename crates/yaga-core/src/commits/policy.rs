use serde::{Deserialize, Serialize};

macro_rules! choices {
    ($name:ident { $($variant:ident => $value:literal),+ $(,)? }) => {
        #[derive(Clone, Copy, Debug, Deserialize, Serialize, Eq, PartialEq)]
        pub enum $name {
            $(#[serde(rename = $value)] $variant),+
        }
        impl $name {
            pub fn as_str(self) -> &'static str {
                match self { $(Self::$variant => $value),+ }
            }
        }
    }
}
choices!(MessageFormat { Conventional => "conventional", Plain => "plain" });
choices!(FooterSyntax { Conventional => "conventional", ColonWhitespace => "colon-whitespace" });
choices!(Presence { Optional => "optional", Required => "required", Forbidden => "forbidden" });
choices!(Case { Any => "any", Lower => "lower", Upper => "upper" });
choices!(Ending { Allow => "allow", Require => "require", Forbid => "forbid", ForbidPeriod => "forbid-period" });
choices!(DescriptionCase { Any => "any", ForbidInitialUppercase => "forbid-initial-uppercase" });
choices!(LengthUnit { Codepoints => "codepoints", Utf16 => "utf16" });
choices!(LineLengthUrls { Check => "check", Exempt => "exempt" });
choices!(MergePolicy { Ignore => "ignore", Reject => "reject", Check => "check" });
choices!(BreakingMarkers { Either => "either", Paired => "paired" });
choices!(ParagraphSplitting { Skip => "skip", Check => "check" });
choices!(TyposPolicy { Skip => "skip", Check => "check" });
choices!(TyposConfig { Repository => "repository", Isolated => "isolated" });
choices!(DependabotPullRequests { Check => "check", Skip => "skip" });
choices!(PullRequestMessage { TitleOnly => "title-only", TitleAndBody => "title-and-body" });
choices!(QualityProvider {
    Huggingface => "huggingface", Bedrock => "bedrock", Openai => "openai",
    Anthropic => "anthropic", Gemini => "gemini", OpenaiCompatible => "openai-compatible"
});
impl QualityProvider {
    pub fn requires_explicit_model(self) -> bool {
        !matches!(self, Self::Huggingface | Self::Bedrock)
    }
}
pub const EXPLICIT_QUALITY_MODEL: &str = "Hosted quality requires an explicit model identifier for openai, anthropic, gemini, or openai-compatible.";
choices!(QualityTask { Classification => "classification", Seq2seq => "seq2seq" });
choices!(QualityInputMode { Message => "message", Title => "title" });

/// Effective settings consumed by the pure checker. Configuration adapters are
/// responsible for schema validation, cross-field constraints and versioning.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct CommitPolicy {
    pub allowed_types: Option<Vec<String>>,
    pub type_case: Case,
    pub scope_policy: Presence,
    pub scope_policy_by_type: Vec<(String, Presence)>,
    pub scope_case: Case,
    pub allowed_scopes: Option<Vec<String>>,
    pub header_max_length: Option<usize>,
    pub description_min_length: usize,
    pub description_max_length: Option<usize>,
    pub description_ending: Ending,
    pub description_case: DescriptionCase,
    pub length_unit: LengthUnit,
    pub footer_syntax: FooterSyntax,
    pub line_length_urls: LineLengthUrls,
    pub footer_max_line_length: Option<usize>,
    pub required_colon_footer_tokens: Vec<String>,
    pub breaking_markers: BreakingMarkers,
    pub message_format: MessageFormat,
    pub warning_rules: Vec<String>,
    pub required_issue_prefixes: Vec<String>,
    pub footer_values: Vec<(String, Vec<String>)>,
    pub required_footer_tokens: Vec<String>,
    pub forbidden_footer_tokens: Vec<String>,
    pub body_policy: Presence,
    pub body_policy_by_type: Vec<(String, Presence)>,
    pub body_max_length: Option<usize>,
    pub body_min_length: usize,
    pub body_min_words: usize,
    pub body_max_line_length: Option<usize>,
    pub body_paragraph_splitting: ParagraphSplitting,
    pub ignored_headers: Vec<String>,
    pub merge_commits: MergePolicy,
}

impl Default for CommitPolicy {
    fn default() -> Self {
        Self {
            allowed_types: None,
            type_case: Case::Any,
            scope_policy: Presence::Optional,
            scope_policy_by_type: Vec::new(),
            scope_case: Case::Any,
            allowed_scopes: None,
            header_max_length: None,
            description_min_length: 1,
            description_max_length: None,
            description_ending: Ending::Allow,
            description_case: DescriptionCase::Any,
            length_unit: LengthUnit::Codepoints,
            footer_syntax: FooterSyntax::Conventional,
            line_length_urls: LineLengthUrls::Check,
            footer_max_line_length: None,
            required_colon_footer_tokens: Vec::new(),
            breaking_markers: BreakingMarkers::Either,
            message_format: MessageFormat::Conventional,
            warning_rules: Vec::new(),
            required_issue_prefixes: Vec::new(),
            footer_values: Vec::new(),
            required_footer_tokens: Vec::new(),
            forbidden_footer_tokens: Vec::new(),
            body_policy: Presence::Optional,
            body_policy_by_type: Vec::new(),
            body_max_length: None,
            body_min_length: 0,
            body_min_words: 0,
            body_max_line_length: None,
            body_paragraph_splitting: ParagraphSplitting::Skip,
            ignored_headers: Vec::new(),
            merge_commits: MergePolicy::Ignore,
        }
    }
}
