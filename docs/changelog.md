# Changelog

## [Unreleased]

### Added

- Added the opt-in `yaga commit quality` advisory using a pinned local Hugging Face model, with
  offline cache support and versioned text/JSON reports.
- Extended commit quality with configurable Hugging Face classification/seq2seq tasks and an
  optional Amazon Bedrock Converse adapter using Nova Micro by default.
- Added strict `[commit.quality]` configuration defaults with per-invocation CLI overrides; secrets
  remain outside YAGA policy files.
- Added configurable `body-max-single-line-paragraphs` enforcement for deterministic detection of
  bodies split into excessive one-line prose paragraphs; list items remain exempt.

### Changed

- Documented the classification, seq2seq, and Bedrock quality modes, including Hugging Face cache
  reuse, offline replay, credential boundaries, and configuration precedence.
- Aligned the repository and hosted workflows on uv `0.12.7`, the runtime supported by Dependabot.
- Kept the public review lifecycle provider-neutral as the Agent review gate while retaining
  provider-specific adapters behind configurable presets and named lenses.

### Fixed

- Allow one intentional one-line prose paragraph while still flagging repeated blank-line sentence
  splitting; YAGA's own policy now uses that bound.
- Added a bounded findings-by-rule summary to GitHub commit-policy output so failed rule codes are
  visible in workflow logs and step summaries.

YAGA keeps a human-readable [CHANGELOG.md](https://github.com/dariuszpanas/yaga/blob/main/CHANGELOG.md)
at the repository root. It follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
and uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) for releases.

## How entries are organized

Keep an `## [Unreleased]` section at the top while work is in progress. Group notable changes under
the standard headings:

| Heading | Use it for |
| --- | --- |
| `Added` | New features or supported interfaces. |
| `Changed` | Changes to existing behavior or configuration. |
| `Deprecated` | Supported behavior scheduled for removal. |
| `Removed` | Behavior or interfaces no longer present. |
| `Fixed` | Bug fixes and corrected regressions. |
| `Security` | Vulnerability fixes or security-boundary changes. |

Write entries for users and integrators, not one line per commit. Explain the observable behavior,
the affected command or configuration surface, and any migration or security consequence. Keep
entries concise, linkable, and in reverse chronological release order. Use ISO 8601 dates
(`YYYY-MM-DD`) when a version is released.

## YAGA release workflow

Before a release:

1. Review the `Unreleased` entries against the merged changes and remove implementation noise.
2. Move the entries into a version heading such as `## [0.1.0] - 2026-09-10`.
3. Add a fresh empty `Unreleased` section above the release.
4. Add comparison links at the bottom once the tag exists.
5. Run `uv run make ci`, verify the exact built artifact, and publish only when explicitly authorized.

The changelog complements commits and GitHub Releases: commits explain implementation steps,
whereas this file summarizes the notable result for people adopting a version.
