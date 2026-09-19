# Agent review plans and receipts

Use these provider-neutral commands to describe review work for an external adapter and evaluate
its results. YAGA validates policies and receipts; it does not execute the configured agents.
The adapter is responsible for running each lens and authenticating its evidence.

For the experimental fixed Codex request/completion protocol, see the separate
[GitHub review adapter](github-review-adapter.md). That adapter cannot execute configured lens
instructions or bind outcomes to a policy digest. Neither surface is required for ordinary CI checks.

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
yaga gate agent-review policy template --file .yaga.toml > review-results.json
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

When an adapter needs a result file before all providers have completed, use `policy template` to
create a complete JSON scaffold:

```bash
yaga gate agent-review policy template --file .yaga.toml > review-results.json
```

The scaffold contains the current policy version, matching `plan_digest`, and exactly one explicit
`pending` result for every configured lens in policy order. Replace each pending outcome with the
adapter's `passed`, `failed`, or `pending` result and optionally add a bounded `summary`; do not
remove lenses or edit the digest. The command is read-only and prints only JSON, so it is suitable
for a temporary handoff file or a job artifact. Evaluation still revalidates the file against the
current policy.

## Adapter integration checklist

An adapter can be implemented in any language or workflow system. Keep provider-specific execution
outside YAGA and make the YAGA boundary a small, deterministic handoff:

1. Load the trusted policy and run `policy check` before contacting a provider.
2. Render `policy plan --format json` and retain its `plan_digest` with the adapter run.
3. For each ordered lens, select the adapter named by `preset`, pass its `instruction` as data,
   and preserve the requested `outcome` and `publication` mode. Do not treat the instruction as
   executable shell or workflow source.
4. Correlate every provider result to the exact pull request, base SHA, head SHA, lifecycle
   boundary, and adapter request before accepting it. An uncorrelated result is `pending` or an
   operational failure, never an implicit pass.
5. Start from `policy template` or construct the same complete receipt: one result per lens, in
   any order, with the exact plan digest. Use `passed` for a completed clean lens, `failed` for a
   completed finding, and `pending` when the provider has not completed.
6. Run `policy evaluate --format github` in the publishing step. Exit `0` means the configured
   aggregation passed: every required lens for `all-required`, or at least one for `any-required`.
   Exit `1` means the aggregate failed or remains pending, and `2` means the policy, receipt,
   provider handoff, or invocation could not be validated safely.

The adapter may publish comments, inline findings, reactions, reviews, or checks while it runs,
but those provider artifacts are not the receipt itself. Keep the receipt summary short and
bounded; store detailed provider output in the provider's own bounded artifact or review surface.
Never put credentials, access tokens, or unbounded provider responses in the policy, plan, receipt,
GitHub outputs, or error text. If a lens fails before a provider result can be correlated, retain
the explicit `pending` result and report the operational cause separately rather than fabricating a
`failed` review outcome.

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

`policy evaluate` rejects malformed, duplicate, unknown, incomplete, oversized, or plan-mismatched
results. An adapter must explicitly return `pending` when a lens has not completed; omission is an
invalid handoff rather than an implicit pending state. Evaluation applies the configured
aggregation and returns exit `0` only when all blocking policy requirements pass.
Operational and configuration failures honor the selected output format too: JSON emits the
versioned `error` document on standard error, while GitHub output emits one escaped error command.
Advisory failures remain visible without changing that exit state. Use `--format github` when the
adapter runs in a GitHub Actions step: it emits escaped error annotations for blocking failures and
pending lenses, warning annotations for advisory findings, and one bounded notice summary. The
format never prints the full untrusted result summary and keeps the same exit codes as text and
JSON.
An explicitly returned result without a summary is reported as `provider returned no summary`; the
distinct `no result was returned` detail is reserved for internal incomplete-state rendering.

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
read credentials. Run the installed `policy check`, `plan`, `template`, and `evaluate` commands in
that adapter's trusted workflow. The fixed GitHub composite Action does not accept configured policies. PR-controlled policy or receipt content must
never be used as security authority.

## Evidence responsibility

An external adapter must correlate results to the exact pull request, base and head commits,
named lens, adapter preset, lifecycle boundary, and request. The local receipt evaluator validates
policy identity and aggregates outcomes; it does not independently authenticate provider evidence.
PR-controlled policy or receipt content must never be treated as security authority.
