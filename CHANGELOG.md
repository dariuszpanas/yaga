# Changelog

All notable changes to YAGA are documented in this file. The format follows [Keep a
Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Allow a quality classification threshold of `0`, matching the CLI's inclusive `0`-through-`1`
  option range for intentionally flagging every nonnegative score.
- Preserve diagnostic line and column locations in standalone and pull-request text reports,
  matching the detail already available in JSON and GitHub output.
- Add readable `GITHUB_STEP_SUMMARY` sections to both repository commit-quality workflow passes
  while preserving the quality command's original exit status.
- Add provider, task, model, mode, and aggregate counts to the bounded GitHub notice emitted by
  `commit quality --format github`.
- Dogfood `commit quality --format github` in the repository workflow for visible advisory warning
  annotations in both online and offline cache-replay steps.
- Add `--format github` to `yaga commit quality`, emitting bounded warning annotations for flagged
  advisory results and an error annotation for provider failures.
- Refine `body-paragraph-splitting = "check"` so punctuation-free one-line notes remain valid;
  only stronger continuation signals such as continuation punctuation or a lowercase next paragraph
  are reported.
- An opt-in `yaga commit quality` advisory using a pinned local Hugging Face model, with offline
  cache support and versioned text/JSON reports.
- Make commit body line length an opt-in policy so longer explanations are accepted by default;
  repositories can still configure `body-max-line-length` when wrapping is part of their style.
- A direct `agent-review` gate contract with a provider-neutral public name, renamed workflow
  example, and initial named-lens configuration documentation.
- A bounded named-lens policy model and `yaga gate agent-review policy check` validator for local
  configuration feedback without contacting a review provider.
- Trusted composite Actions can validate a bounded Agent review policy from the default-branch
  workspace before making their first GitHub API request.
- Add `agent-review policy plan` to expose deterministic named-lens adapter work in text or JSON.
- Add closed per-lens publication modes for comments, inline reviews, reactions, native reviews,
  and check-style provider output.
- Add a bounded JSON adapter-result contract and `agent-review policy evaluate` for local
  provider-neutral aggregation with meaningful exit codes.
- Add a provider-neutral named-lens evaluator with deterministic `all-required` and `any-required`
  aggregation and non-blocking advisory outcomes.
- An explicit `commit.typos = "check"` mode for bounded commit-message spelling checks through an
  installed Typos CLI, with stable `typos.word` diagnostics.
- Status-only `--quiet`/`-q` output for the standalone branch, change, mode, path, size, and tree
  providers.
- Detailed documentation for usage modes, configuration, repository checks, GitHub Actions, and
  adoption recipes.
- Quality troubleshooting guidance for CPU-only installation, cache/offline replay, Bedrock
  credentials, and model input coverage.

### Changed

- Cache the verified Cargo registry used to install the pinned Typos CLI, reducing repeated network
  downloads without caching an executable supplied by pull-request code.
- Clarify the command synopsis so `commit quality` is identified as a provider-backed advisory,
  including the Hugging Face and Bedrock modes documented below.
- Enable the bounded Typos commit-message check in YAGA's own policy and install the pinned CLI in
  the hosted commit-policy and CI workflows.
- Document classification, seq2seq, and Bedrock quality modes, including Hugging Face cache reuse,
  offline replay, credential boundaries, and configuration precedence.
- Use CPU-only PyTorch wheels on Linux for the optional quality workflow, avoiding unnecessary CUDA
  runtime downloads while retaining platform-specific wheels elsewhere.
- Align the repository and hosted workflows on uv `0.12.7`, the runtime supported by Dependabot.
- Keep the public review lifecycle provider-neutral as the Agent review gate while retaining
  provider-specific adapters behind configurable presets and named lenses.

### Fixed

- Reject unsupported Bedrock quality tasks and model revisions during configuration or invocation,
  and keep Bedrock reports from claiming Hugging Face revision provenance.
- Allow one intentional one-line prose paragraph while still flagging repeated blank-line sentence
  splitting; YAGA's own policy now uses that bound.
- Add a bounded findings-by-rule summary to GitHub commit-policy output so failed rule codes are
  visible in workflow logs and step summaries.

### Security

- Update the optional Hugging Face `transformers` extra to the patched 5.x line and refresh the
  lockfile, closing the active model-initialization and model-save security advisories.

<!-- Release links will be added when version tags are published. -->
