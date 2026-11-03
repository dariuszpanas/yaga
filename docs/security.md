# Security model

YAGA exposes an installed CLI for repository policy and a unified root composite Action
(`action.yml`) with isolated runtimes for read-only commit checks, installed CLI checks, and the
Agent review gate. The trust boundary is intentionally narrow.

## Explicit and bounded inputs

Git revisions, paths, workflow text, event payloads, API responses, comments, reviews, reactions,
and status metadata are treated as untrusted. Providers bound input sizes, disable Git prompts and
replacement refs, invoke Git without a shell, and fail closed on malformed or ambiguous evidence.

Git-backed checks do not fetch history. Commit checks default to `HEAD` only when no explicit
source is supplied; committed-tree providers require an explicit revision. `repo check` requires
an explicit provider selection or a named plan and keeps provider failures distinct.

## Actions trust split

Pull-request CI runs without trusted write credentials and can only save quota or report quality
findings. Write-capable gate operations run from trusted default-branch workflows. In the root Action, both `check: commit` and `check: gate` use standard-library-only bootstraps,
installed-CLI modes isolate their hash-locked `--no-dev` virtual environment outside the checkout,
and no mode accepts a GitHub token as an argument, output, or log value.

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
