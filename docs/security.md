# Security model

YAGA has two surfaces: an installed CLI for repository policy and a dependency-free composite
Action runtime for the retained Codex review gate. The trust boundary is intentionally narrow.

## Explicit and bounded inputs

Git revisions, paths, workflow text, event payloads, API responses, comments, reviews, reactions,
and status metadata are treated as untrusted. Providers bound input sizes, disable Git prompts and
replacement refs, invoke Git without a shell, and fail closed on malformed or ambiguous evidence.

Checks do not fetch history or infer state from the current checkout. Revision-bound providers
operate on one explicit commit-ish and enumerate a bounded committed tree. `repo check` requires
an explicit plan and keeps provider failures distinct.

## Actions trust split

Pull-request CI runs without trusted write credentials and can only save quota or report quality
findings. Write-capable gate operations run from trusted default-branch workflows. The root Action
uses a standard-library-only bootstrap and never accepts a GitHub token as an argument, output, or
log value.

Consumers must pin the Action to an audited full commit SHA. Review the example workflows and
[SECURITY.md](https://github.com/dariuszpanas/yaga/blob/main/SECURITY.md) before deployment.

## Safe operational defaults

- immutable action and container references are required by policy
- workflow linting uses a pinned actionlint image with no network and no host mount
- local reusable workflows are resolved within the trusted checkout boundary
- diagnostics are sanitized and bounded before text, JSON, or GitHub reporting
- exit `2` is reserved for operational or input failures, including incomplete evidence

These checks support merge security but do not replace GitHub branch protection, required review,
least-privilege workflow permissions, or an audit of every writer in the repository.
