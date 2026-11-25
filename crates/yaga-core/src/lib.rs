//! Native YAGA policy checks for Rust applications and language adapters.

#![deny(unsafe_code)]

#[cfg(feature = "process")]
pub mod actionlint;
pub mod agent_review;
pub mod branch;
#[cfg(feature = "process")]
pub mod cli_action;
#[cfg(feature = "process")]
pub mod commit_source;
pub mod commits;
#[cfg(feature = "process")]
pub mod completion;
pub mod descriptions;
pub mod fs;
#[cfg(feature = "process")]
pub mod gh;
#[cfg(feature = "process")]
pub mod git;
#[cfg(feature = "process")]
pub mod github_commit;
pub mod json;
pub mod paths;
pub mod policies;
#[cfg(feature = "process")]
pub mod quality;
pub mod repository;
pub mod review;
pub mod test_planning;
pub mod text;
mod timestamp;
pub mod triage;
#[cfg(feature = "process")]
pub mod typos;
pub mod version;
pub mod workflows;
