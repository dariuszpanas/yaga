//! Native YAGA policy checks for Rust applications and language adapters.

#![deny(unsafe_code)]

pub mod actionlint;
pub mod agent_review;
pub mod branch;
pub mod cli_action;
pub mod commit_source;
pub mod commits;
pub mod completion;
pub mod descriptions;
pub mod fs;
pub mod gh;
pub mod git;
pub mod github_commit;
pub mod json;
pub mod paths;
pub mod policies;
pub mod quality;
pub mod repository;
pub mod review;
pub mod test_planning;
pub mod text;
mod timestamp;
pub mod triage;
pub mod typos;
pub mod version;
pub mod workflows;
