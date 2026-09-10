# Codex review gate

The retained Codex review gate is exposed through both `yaga gate codex-review <operation>` and
the supported composite Action adapter. It is an experimental, quota-sensitive integration.

## Operations

The closed operations are `invalidate`, `prepare`, `authorize`, `observe`, `request`, and
`finalize`. `prepare` emits only a closed route capability and pull-request number; candidate
identity never crosses a job output.

The workflow is split so untrusted pull-request CI can provide prerequisites while trusted
default-branch workflows own lifecycle invalidation, authorization, requests, and terminal status
publication. The exact current-boundary request is required before accepting any eyes reaction or
Codex outcome, including the initial `opened` boundary.

## Evidence and recovery

Accepted evidence is limited to a clean connector comment with the current reviewed commit, a
formal findings review with the current native commit ID and marker, or the connector's `+1`
reaction. Evidence must be strictly later than the exact YAGA request. Malformed, incomplete,
ambiguous, stale, displaced, or over-budget evidence fails closed.

The gate revalidates live PR state and status capacity around every write. It bounds comments,
reviews, reactions, statuses, event data, requests, and polling. A new commit or PR is required
when the visible status or evidence history reaches its ceiling.

## Quota firewall

Configure `YAGA_CODEX_OWNER_ID` as a repository variable. Direct owner requests use that immutable
ID; other authors require the protected `codex-review-approval` environment with the exact marker
`codex-review-approval:v1`. Store no secrets in that environment, prevent self-review, and disable
administrator bypass.

Disable automatic reviews and drain existing Codex tasks before enabling the publisher. YAGA
cannot prevent a separate human or app from posting a direct review request, so provider-side
controls and repository permissions remain part of the deployment boundary.
