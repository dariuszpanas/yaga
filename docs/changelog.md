# Changelog

## [Unreleased]

### Added

- Added a source-mode matrix and copyable examples for direct, file, stdin, single-commit, range,
  and default-`HEAD` `commit quality` invocations.
- Added per-message quality coverage metadata so Hugging Face text and JSON reports identify
  the measured input-token count and whether the configured input-token window truncated the model
  input.
- Added configurable Hugging Face quality input windows through `max-input-tokens` and
  `--max-input-tokens`, with the effective bound included in text and JSON reports.
- Added concrete passing and failing examples for the explicit paragraph-splitting check, including
  its continuation-aware punctuation heuristic and valid one-line notes.

- Added JSON output for `agent-review policy check`, including the validated lens counts, required
  lens order, aggregation mode, and matching plan digest.
- Added deterministic Agent review plan digests so adapter result receipts cannot be evaluated
  against a different lens configuration.
- Added the opt-in `yaga commit quality` advisory using a pinned local Hugging Face model, with
  offline cache support and versioned text/JSON reports.
- Extended commit quality with configurable Hugging Face classification/seq2seq tasks and an
  optional Amazon Bedrock Converse adapter using Nova Micro by default.
- Added strict `[commit.quality]` configuration defaults with per-invocation CLI overrides; secrets
  remain outside YAGA policy files.
- Added configurable paragraph-splitting enforcement that detects likely sentence breaks across
  blank lines, leaving standalone short paragraphs and list items valid.

### Changed

- Documented the distinction between complete selected-message line counts and the 512-token
  Hugging Face model input window, so quality reports are not mistaken for full semantic coverage.
- Clarified `body.paragraph-format` diagnostics so failures identify the likely sentence split
  across blank lines instead of reporting an arbitrary paragraph count.

- Expanded Agent review documentation with the digest-bound adapter handoff, receipt lifecycle,
  generic preset-label semantics, and GitHub-format publishing example.
- Replaced the confusing numeric consecutive-single-line paragraph setting with explicit
  `body-paragraph-splitting = "skip" | "check"` configuration.
- Agent review plan digests are now visible in text plans and evaluation summaries as well as JSON.
- Quality reports now state how many body lines were included in each model invocation, making
  title-only and multiline checks distinguishable without printing the full commit message.
- Documented the classification, seq2seq, and Bedrock quality modes, including Hugging Face cache
  reuse, offline replay, credential boundaries, and configuration precedence.
- Aligned the repository and hosted workflows on uv `0.12.7`, the runtime supported by Dependabot.
- Kept the public review lifecycle provider-neutral as the Agent review gate while retaining
  provider-specific adapters behind configurable presets and named lenses.

### Fixed

- Refined paragraph enforcement to catch likely blank-line sentence splitting without treating every
  one-line prose paragraph as a failure; YAGA's own policy uses the consecutive-run bound.
- Added a bounded findings-by-rule summary to GitHub commit-policy output so failed rule codes are
  visible in workflow logs and step summaries.

### Security

- Updated the optional Hugging Face `transformers` extra to the patched 5.x line and refreshed the
  lockfile, closing the active model-initialization and model-save security advisories.

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
