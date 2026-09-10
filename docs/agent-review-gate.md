# Agent review gate design

YAGA’s current write-capable integration is named `codex-review`, but the lifecycle it protects is
broader than one review vendor. The target model is an integration-neutral `agent-review` gate:
the gate owns request authorization, exact-head correlation, bounded observation, and terminal
publication; an adapter owns the provider-specific request and evidence vocabulary.

The existing Codex implementation remains the compatibility baseline while this boundary is
introduced incrementally. This page describes the intended configuration contract; it does not
claim that arbitrary providers are accepted by the current runtime yet.

## One gate, many named lenses

A repository may run several agents against the same pull request. Each agent has a stable name,
a distinct lens, and an independent result. For example, one lens can focus on correctness, another
on security, and a third on API compatibility:

```toml
[agent-review]
version = 1
required = ["correctness", "security"]
aggregation = "all-required"

[agent-review.agents.correctness]
preset = "codex"
instruction = "Review behavior, tests, and backwards compatibility."
outcome = "review"

[agent-review.agents.security]
preset = "codex"
instruction = "Review trust boundaries, input validation, and least privilege."
outcome = "review"

[agent-review.agents.documentation]
preset = "codex"
instruction = "Review public documentation and developer ergonomics."
outcome = "advisory"
```

The configuration describes policy, not credentials. `preset` selects a reviewed adapter profile;
`instruction` is bounded context for that lens; `required` controls which named results can block
the aggregate; and `outcome` distinguishes a required gate result from an advisory report. A
future implementation must reject unknown keys, duplicate names, unbounded instructions, unknown
presets, circular aggregation, and agents that are both required and advisory.

## Adapter boundary

An adapter should provide only provider-specific behavior:

| Adapter responsibility | Gate responsibility |
| --- | --- |
| Request syntax and provider identity | Exact pull-request, base, and head binding |
| Accepted comment, review, reaction, or check evidence | Current-boundary request provenance |
| Provider result mapping to `passed`, `failed`, or `pending` | Pagination, timestamps, quotas, and write leases |
| Provider-specific status text | Authorization, stale-state recovery, and terminal publication |

This prevents each new integration from reimplementing the security-sensitive lifecycle. A provider
can use a clean comment, inline findings, a native review, a reaction, or a check run, but the
adapter must expose one bounded normalized result to the gate. Evidence from one agent must never
 satisfy another agent’s request merely because it has the same pull-request number or commit.

## Aggregation and failure behavior

The normalized result for each named agent should be independently correlated to:

- the exact pull-request number, base commit, and head commit;
- the exact YAGA request and lifecycle boundary that authorized it;
- the configured agent name and adapter preset; and
- a bounded, strictly later provider event.

The aggregate should remain pending while any required agent is in flight, pass only when every
required agent passes, and fail when a required agent has a trusted failure or an operational
error. Advisory agents may publish findings without blocking the aggregate. Missing, malformed,
ambiguous, stale, unsolicited, or over-budget evidence must fail closed rather than being treated
as an advisory result.

## Migration from the Codex gate

The migration should be staged:

1. Keep `yaga gate codex-review` and the existing Codex Action adapter unchanged.
2. Introduce a provider-neutral normalized result and configuration parser with a frozen schema.
3. Run the Codex preset through that adapter without changing its current evidence or trust rules.
4. Add `agent-review` as the generic public name only after parity tests prove identical lifecycle
   behavior for the Codex preset.
5. Add other adapters one at a time, with their own identity, evidence, quota, and failure tests.

Until those steps are complete, consumers should continue using the documented Codex workflow and
must not assume that a generic provider can be enabled by changing a string in an Action.
