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
publication = "inline"

[agent-review.agents.security]
preset = "codex"
instruction = "Review trust boundaries and least privilege."
outcome = "review"

[agent-review.agents.documentation]
preset = "codex"
instruction = "Review public documentation and developer ergonomics."
outcome = "advisory"
publication = "comment"
```

Required lenses participate in the aggregate gate; advisory lenses can publish findings without
blocking it. Every lens must have a unique name, a bounded instruction, a bounded adapter preset
label, and one publication mode: `comment`, `inline`, `reaction`, `review`, or `check`. The mode describes
the intended provider output; it does not grant credentials or bypass provider capabilities.
Credentials, tokens, and provider-specific secrets stay in the trusted workflow environment.

Adapters return only the closed outcomes `passed`, `failed`, or `pending`, keyed by lens name. The
provider-neutral evaluator applies `all-required` or `any-required` to required lenses, preserves
advisory failures and pending work for reporting, and never lets an advisory lens block the gate.
Unknown lens names and outcome values are configuration errors.

Validate the policy before wiring it into a workflow:

```bash
yaga gate agent-review policy check --file .yaga.toml
yaga gate agent-review policy check --file .yaga.toml --format json
```

The JSON check report is a validation summary for wrappers and CI tooling. It contains the schema
version, `passed` status, aggregation mode, lens and required counts, required lens order, and the
matching `plan_digest`; it does not contact a provider or include credentials. Use `policy plan
--format json` when an adapter needs the full instructions and provider-neutral lens items.

To inspect the exact provider-neutral work the policy describes, render its execution plan:

```bash
yaga gate agent-review policy plan --file .yaga.toml
yaga gate agent-review policy plan --file .yaga.toml --format json
yaga gate agent-review policy evaluate --file .yaga.toml --results review-results.json
yaga gate agent-review policy evaluate --file .yaga.toml --results review-results.json --format github
```

The plan preserves configuration order, marks every lens as `required` or `advisory`, and carries
the requested publication mode. Its `plan_digest` is a deterministic SHA-256 identity of the
version, aggregation, and ordered lens definitions. It does not contact an agent, resolve a preset,
or read credentials. Adapters can use the JSON document as their input and must copy its digest into
the result receipt while returning one closed outcome for each named lens.
Text plan output prints the same digest on a separate `Plan digest:` line; JSON plan output carries
it as `plan_digest`. Evaluation text, JSON, and GitHub notice output repeat the digest so CI logs
can be correlated with the exact configuration used.

Treat the plan and receipt as one versioned handoff. A trusted adapter should generate the plan from
the policy, pass each ordered lens to its selected provider, preserve the lens name and requested
publication mode in its own provider work, and write one result for every plan item. If the policy
file, lens order, aggregation, instruction, preset label, outcome role, or publication mode changes,
the digest changes and the adapter must generate a new receipt before evaluation.
The result contract is deliberately small:

```json
{
  "version": 1,
  "plan_digest": "8fab465b0b8ea9bf25e0c993387673ddde272405e885573f2094e09611c02cf0",
  "results": [
    {"lens": "correctness", "outcome": "passed", "summary": "No findings."},
    {"lens": "documentation", "outcome": "failed", "summary": "The setup example is stale."}
  ]
}
```

`policy evaluate` rejects malformed, duplicate, unknown, oversized, or plan-mismatched results,
applies the configured aggregation, and returns exit `0` only when all blocking policy requirements
pass.
Advisory failures remain visible without changing that exit state. Use `--format github` when the
adapter runs in a GitHub Actions step: it emits escaped error annotations for blocking failures and
pending lenses, warning annotations for advisory findings, and one bounded notice summary. The
format never prints the full untrusted result summary and keeps the same exit codes as text and
JSON.

## End-to-end adapter example

The repository contains a synthetic, provider-neutral example in
[`examples/agent-review-policy.toml`](https://github.com/dariuszpanas/yaga/blob/main/examples/agent-review-policy.toml)
and its matching result receipt in
[`examples/agent-review-results.json`](https://github.com/dariuszpanas/yaga/blob/main/examples/agent-review-results.json).
Run the complete local flow from the repository root:

```bash
yaga gate agent-review policy check --file examples/agent-review-policy.toml
yaga gate agent-review policy plan \
  --file examples/agent-review-policy.toml \
  --format json
yaga gate agent-review policy evaluate \
  --file examples/agent-review-policy.toml \
  --results examples/agent-review-results.json
```

The example exits `0`: both blocking lenses passed even though the advisory documentation lens
failed. A real adapter replaces the example result file after it runs its configured lenses. It
should translate provider-specific comments, inline findings, reactions, native reviews, or check
runs into exactly one result per configured lens, preserving a short human-readable summary.
YAGA does not execute the configured preset or instruction and does not grant provider credentials;
the trusted integration owns that work and must correlate its evidence to the current pull request,
commit, lifecycle boundary, and request before writing the receipt.

Use a blocking failure to exercise CI behavior:

```json
{"version":1,"plan_digest":"8fab465b0b8ea9bf25e0c993387673ddde272405e885573f2094e09611c02cf0","results":[
  {"lens":"correctness","outcome":"failed","summary":"A regression is present."},
  {"lens":"security","outcome":"passed"},
  {"lens":"documentation","outcome":"passed"}
]}
```

In that case `policy evaluate` exits `1` and prints the blocking lens. Exit `2` is reserved for
malformed policy/results or an operational error, so adapters can distinguish a review finding
from a broken handoff. The result document is a bounded local interchange format, not an
authorization token or security authority by itself.

The validator requires schema version `1`, at least one required lens, unique lower-case lens names,
known outcomes (`review` or `advisory`), publication modes, bounded instructions, and a bounded
preset label. The label is intentionally not a YAGA provider registry: an external adapter decides
which service, model, or human workflow it represents. The validator does not contact an agent or
read credentials. The trusted publisher will consume this same validated
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
