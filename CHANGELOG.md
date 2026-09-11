# Changelog

All notable changes to YAGA are documented in this file. The format follows [Keep a
Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

<!-- Release links will be added when version tags are published. -->
