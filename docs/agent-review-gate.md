# Agent review gate

The write-capable review integration is exposed as `yaga gate agent-review <operation>` and the
`agent-review` composite Action selector. The gate owns the trusted lifecycle around agent review;
the configured review adapter owns provider-specific request and evidence details.

## Operations

The closed operations are `invalidate`, `prepare`, `authorize`, `observe`, `request`, and
`finalize`. `prepare` emits only a closed route capability and pull-request number; candidate
identity never crosses a job output.

The workflow is split so untrusted pull-request CI can provide prerequisites while trusted
default-branch workflows own lifecycle invalidation, authorization, requests, and terminal status
publication. The exact current-boundary request is required before accepting any agent reaction,
comment, review, or check result, including the initial `opened` boundary.

## Named lenses

One pull request can be reviewed by multiple named agents, each with a distinct lens and blocking
policy. The configuration is policy, not credentials:

```toml
[agent-review]
version = 1
required = ["correctness", "security"]
aggregation = "all-required"

[agent-review.agents.correctness]
preset = "codex"
instruction = "Review behavior, tests, and compatibility."
outcome = "review"

[agent-review.agents.security]
preset = "codex"
instruction = "Review trust boundaries and least privilege."
outcome = "review"

[agent-review.agents.documentation]
preset = "codex"
instruction = "Review public documentation and developer ergonomics."
outcome = "advisory"
```

Required lenses participate in the aggregate gate; advisory lenses can publish findings without
blocking it. Every lens must have a unique name, a bounded instruction, and a known adapter preset.
Credentials, tokens, and provider-specific secrets stay in the trusted workflow environment.

Validate the policy before wiring it into a workflow:

```bash
yaga gate agent-review policy check --file .yaga.toml
```

The validator requires schema version `1`, at least one required lens, unique lower-case lens names,
known outcomes (`review` or `advisory`), bounded instructions, and a bounded preset name. It does
not contact an agent or read credentials. The trusted publisher will consume this same validated
model when provider adapters are enabled. The composite Action can validate the same file before
its first GitHub API request:

```yaml
with:
  agent-review-policy-file: .yaga/agent-review.toml
```

The trusted publisher must check out the default branch with `persist-credentials: false` before
passing that relative path. YAGA rejects paths outside `GITHUB_WORKSPACE`; PR-controlled checkout
content must never be used as security authority.

## Evidence and recovery

An adapter may use a clean comment, inline findings, a native review, a reaction, or a check run.
The gate normalizes that provider result to `passed`, `failed`, or `pending` and correlates it to
the exact pull request, base commit, head commit, named lens, adapter preset, lifecycle boundary,
and current YAGA request.

Evidence must be strictly later than the exact request. Missing, malformed, ambiguous, stale,
displaced, unsolicited, or over-budget evidence fails closed. The gate revalidates live pull-request
state and status capacity around every write, and bounds comments, reviews, reactions, statuses,
event data, requests, and polling.

## Quota firewall

Configure the owner identity and protected approval route required by the selected adapter. Store no
secrets in the approval environment, prevent self-review, and disable administrator bypass. Disable
automatic reviews and drain existing agent tasks before enabling the publisher.

YAGA cannot prevent a separate human or app from posting a direct review request, so provider-side
controls and repository permissions remain part of the deployment boundary.
