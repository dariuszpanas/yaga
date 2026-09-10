# Changelog

All notable changes to YAGA are documented in this file. The format follows [Keep a
Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A direct `agent-review` gate contract with a provider-neutral public name, renamed workflow
  example, and initial named-lens configuration documentation.
- A bounded named-lens policy model and `yaga gate agent-review policy check` validator for local
  configuration feedback without contacting a review provider.
- An explicit `commit.typos = "check"` mode for bounded commit-message spelling checks through an
  installed Typos CLI, with stable `typos.word` diagnostics.
- Status-only `--quiet`/`-q` output for the standalone branch, change, mode, path, size, and tree
  providers.
- Detailed documentation for usage modes, configuration, repository checks, GitHub Actions, and
  adoption recipes.

<!-- Release links will be added when version tags are published. -->
