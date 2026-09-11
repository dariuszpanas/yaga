# Changelog

## [Unreleased]

### Added

- Add `config init --dry-run` to validate and report the generated policy without creating a file.
- Add regression coverage proving event-aware Dependabot skips remain valid for multiline commits.
- Clarify that `config init` requires lowercase commit types while keeping scopes optional and
  lowercased when present.
- Include aggregate character and token coverage counts in the GitHub quality notice, including
  complete, truncated, and unmeasured messages when per-message warnings are absent.
- Link the Agent review gate guide directly from the command reference table.
- Add a provider-neutral Agent review adapter checklist covering plan digests, multi-lens execution,
  evidence correlation, receipts, bounded summaries, and exit-code handling.
- Document Docker and actionlint troubleshooting, including bounded cleanup errors and safe
  inspection of the exact named workspace without broad Docker pruning.
- Count only parsed prose-body lines in quality coverage output, excluding recognized footer blocks.
- Include per-message body-line and model-input coverage in flagged GitHub quality annotations so
  complete-message versus title-only and truncation behavior is visible in CI output.
- Allow `commit quality` classification thresholds from `0` through `1` inclusively, including
  `0` for intentionally flagging every nonnegative classifier score.
- Added diagnostic line and column locations to standalone and pull-request text reports so local
  failures provide the same actionable position detail as JSON and GitHub output.
- Added a dedicated commit-quality guide covering input sources and modes, provider/task choices,
  configuration, model pinning, cache replay, and report interpretation.
- Added per-message quality input character counts and character-bound truncation metadata so
  provider coverage is auditable alongside token coverage.
- Made YAGA's own online and offline quality workflow explicitly select complete-message input.
- Bound the text submitted to every commit-quality provider mode to the documented 12,000
  characters, including classification input and title-only mode.
- Added configurable commit-quality `input-mode` selection between the complete message and title
  only, with the selected mode included in reports.
- Added `agent-review policy template` to emit a complete, plan-digest-bound pending receipt
  scaffold for multi-lens external adapters.
- Added escaped commit-quality output to the GitHub Actions step summary for both online and offline
  workflow passes without masking command failures.
- Added a bounded GitHub notice summarizing quality counts and provider metadata alongside warning
  annotations.
- Updated the repository commit-quality workflow to use GitHub annotations for online and offline
  model-cache checks.
- Added GitHub warning annotations for flagged `commit quality` advisory results through
  `--format github`, while provider failures remain error annotations with exit `2`.
- Refined the paragraph-splitting heuristic so punctuation-free one-line notes remain valid while
  likely sentence continuations still report `body.paragraph-format`.
- Added GitHub Actions guidance for installing the pinned Typos CLI when a repository enables
  commit-message spelling checks.
- Run the optional Typos commit-message adapter from the selected repository so `--repo` checks
  honor that checkout's `_typos.toml` configuration.
- Clarified the root README and command index so `commit quality` is described as a provider-neutral
  advisory with Hugging Face and Bedrock modes, and linked the detailed source, configuration, and
  reporting guidance.
- Added quality-provider troubleshooting guidance for CPU-only dependencies, Hugging Face cache
  reuse and offline replay, Bedrock credentials, and model coverage fields.
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

- Cache the verified Cargo registry used to install the pinned Typos CLI, reducing repeated network
  downloads without caching an executable supplied by pull-request code.
- Clarify the command synopsis so `commit quality` is identified as a provider-backed advisory,
  including the Hugging Face and Bedrock modes documented below.
- Enable the bounded Typos commit-message check in YAGA's own policy and install the pinned CLI in
  the hosted commit-policy and CI workflows.
- Resolve Linux quality dependencies from PyTorch's CPU wheel index so CPU-only model checks do not
  download the CUDA runtime stack.
- Added `--format github` to standalone `commit check`, producing bounded escaped annotations,
  pass/fail totals, and compact rule-count summaries for ordinary CI ranges.
- Isolated the GitHub/Codex connector under the Agent review provider namespace and routed the
  composite Action through a provider-neutral Agent review runtime entrypoint.
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

- Include a bounded sanitized Docker cause when actionlint container or workspace cleanup fails,
  making daemon and resource-state errors actionable in local and CI reports.
- Count body lines correctly in commit-quality reports when messages use Windows CRLF line
  endings.
- Reject invalid Bedrock `seq2seq` quality configuration while loading policy files instead of
  waiting until the advisory command runs.
- Keep Bedrock quality reports free of Hugging Face model revisions, which the Bedrock adapter does
  not consume.
- Refined paragraph enforcement to catch likely blank-line sentence splitting without treating every
  one-line prose paragraph as a failure; the check uses punctuation and paragraph-shape clues.
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
