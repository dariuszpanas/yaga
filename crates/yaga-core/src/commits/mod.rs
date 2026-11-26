//! Commit policy parsing, message checking, configuration and authoring.

mod authoring;
mod checker;
mod config;
mod content;
mod glob;
mod parser;
mod policy;
mod reporting;
mod starter;
mod unicode;
mod validate;

pub use authoring::{TemplateComment, append_template_guidance, explain_type, message_template};
pub use checker::{CheckResult, Diagnostic, Severity, check_message, check_message_with_unicode};
pub use config::{LoadedConfig, MAX_CONFIG_BYTES, QualityPolicy, parse_config, parse_root};
#[cfg(feature = "process")]
pub use config::{global_config_path, load_config};
pub use content::has_issue_reference;
pub use parser::{
    CommitFooter, MAX_MESSAGE_BYTES, ParsedCommit, footer_start, normalize_message, parse_message,
};
pub use policy::*;
pub use reporting::render_config;
pub use starter::ConfigStarter;
#[cfg(feature = "process")]
pub use starter::initialize_config;
pub use unicode::{CommitUnicode, ScalarProperties};

#[cfg(test)]
mod tests;
