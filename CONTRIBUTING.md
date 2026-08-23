# Contributing

YAGA (Yet Another Gate Action) is security-sensitive workflow infrastructure. Changes should be
small, test-backed, and explicit about the event and permission boundary they affect. Its first gate
is `codex-review`; generic infrastructure must not assume that it is the only possible gate.

## Development

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are recommended:

```bash
uv sync --group dev
uv run make ci
```

The full gate requires Docker for the immutable actionlint container. Use `uv run ruff format .` to
apply formatting. The action runtime uses only the Python standard library; build, test, type, and
lint tools are development dependencies.

## Action contract

The `gate` input selects a gate implementation. Only `codex-review` is accepted today; add a new gate
as a separate package with its own evidence grammar and tests instead of adding speculative generic
configuration.

The Codex gate has six internal orchestration modes:

| Mode | Responsibility |
| --- | --- |
| `invalidate-boundary` | Persist a pending boundary before any potentially stale success can be trusted. |
| `repair-boundaries` | Reassert every detected missed boundary as pending, then emit bounded scheduled terminal work. |
| `reconcile-boundary` | Validate a lifecycle boundary and reconcile its exact candidate. |
| `resolve` | Read trusted wake-up evidence and emit a bounded candidate matrix without writing status. |
| `reconcile-candidate` | Re-read and reconcile one head-serialized candidate from that matrix. |
| `reconcile-repair-candidate` | Reconcile scheduled terminal work under the smaller fixed request budget. |

`resolve` and `repair-boundaries` emit `eligible` and `candidates`. Boundary processing may emit
`candidate`; modes that do not use an output leave it empty. Candidate JSON is a capability-like
value, not proof by itself: scheduled repair must persist every observed unsafe boundary before
terminal selection, then a terminal writer must revalidate the live pull request, unique head
ownership, trusted boundary, and current status before publishing or restoring success.

The three terminal reconcile modes require a declared `job-timeout-minutes` success window of at
least 15 minutes. In the shipped workflows YAGA is the first and only job step, and that input equals
the job's `timeout-minutes`. A custom workflow with preceding steps must leave their complete budget
outside the declared YAGA window; the action cannot observe time consumed before its process starts.

The token is accepted only through the `github-token` input and passed to Python via the environment.
Callers must grant the minimum permissions for the selected mode. Unprivileged observer workflows
must not receive write permissions or secrets.

## Design rules

- A commit status is authoritative only for the exact pull request, head SHA, base SHA, and trusted
  lifecycle boundary encoded by its publisher.
- A newer boundary invalidates earlier success before serialized reconciliation begins.
- Only exact connector identities and closed, tested outcome grammars can satisfy the gate.
- A displayed reaction without a commit binding cannot prove review of a synchronized head.
- Publisher reruns and scheduled repair are bounded and idempotent; operational uncertainty remains
  pending instead of being reported as a review failure.
- Workflows must stop cleanly for closed pull requests and must not request a post-merge review.

## Pull requests

Use Conventional Commit subjects such as `feat(action): add candidate reconciliation`. Describe the
security boundary, behavior, failure mode, validation performed, and any migration requirement in
the retained commit and pull request. Fold review fixes and CI repairs into the logical commit they
correct.
