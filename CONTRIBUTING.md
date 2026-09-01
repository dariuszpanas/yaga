# Contributing

YAGA is an extensible repository-policy CLI with a security-sensitive GitHub Action surface. Keep
changes focused, test-backed, and explicit about the public command, configuration, diagnostic, or
gate boundary they affect. The first local/CI policy is `commit check`; the retained write-capable
gate is `codex-review`.

## Development

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are recommended:

```bash
uv sync --group dev
uv run make ci
```

The full gate invokes `yaga repo check --plan .yaga/checks/ci.toml --commit HEAD`; that explicit
versioned plan selects the same providers and workflow paths locally and in CI, including the
Docker-backed `workflow-lint` provider. The installed CLI uses the locked Typer dependency. The
write-capable root Action import graph—`__main__` in gate mode, `action_cli`, `action`, `codex`, and
the generic GitHub primitives—must remain Python-standard-library-only and must not import the
installed CLI, command, commit-policy, or presentation modules. The separate read-only commit
Action may import dependency-light commit modules, but never Typer, command modules, Codex policy,
GitHub REST transport, site packages, or token handling.

The package build gate must execute the fresh wheel outside the checkout. Export the publishable
runtime closure from `uv.lock`, require its hashes without building dependencies, install the wheel
with dependency resolution disabled, run `uv pip check`, and execute the installed `yaga` entrypoint
with project and Python environment leakage removed.

## CLI and commit policy contract

The public installed command groups are `branch`, `change`, `commit`, `config`, `github`, `repo`,
`tree`, `workflow`, and `gate`.
Keep Typer declarations in `src/yaga/commands/`; keep commit parsing, policy, Git selection,
configuration, GitHub event adaptation, and reporting in focused dependency-light modules under
`src/yaga/commits/`. Domain behavior must remain directly testable without invoking Typer.

`commit check` accepts exactly one of a message, UTF-8 file, standard input, Git commit, or Git
range; with none it checks `HEAD`. Preserve full messages, deterministic oldest-first range order,
hard message/config/output/count bounds, shell-free Git invocation, and explicit failure for missing
or shallow history. Commit messages and Git output are untrusted terminal input: sanitize and bound
anything displayed. The installed entrypoint must configure UTF-8 standard output and error before
Typer renders user-controlled text; keep the legacy-console-encoding subprocess regression.

Configuration is schema version 1 in `[tool.yaga]` plus `[tool.yaga.commit]`, or in the standalone
`.yaga.toml` root plus `[commit]`. Load exactly one nearest or explicit file, reject unknown keys and
wrong types, and do not silently merge policies. Stable diagnostic identifiers and JSON schema
fields are public pre-release interfaces; change them deliberately and test both text and JSON.
`scope-policy-by-type` is an empty-default schema-v1 mapping of safe type tokens to the closed
presence-policy values. Match override keys case-insensitively and let an override replace the
global `scope-policy` for both complete commits and header-only pull-request title checks; keep
`scope-case` and `allowed-scopes` independent when a scope is present. Bound the mapping at 128
entries, reject normalized duplicates and keys outside a configured `allowed-types`, and validate
allowed-scope reachability. Header-limit validation is deliberately a conservative structural
lower bound because Unicode case-fold equivalents may be shorter than configured spellings;
actual headers remain subject to every normal check. Preserve the existing `scope.required` and
`scope.forbidden` diagnostics.
`breaking-markers` is a schema-v1 enum with `either` as its default. `either` accepts a header `!`,
a recognized final `BREAKING CHANGE:` or `BREAKING-CHANGE:` footer, or both; `paired` requires both
markers or neither and reports `breaking.marker-pair` for exactly one. Apply marker pairing only to
complete commit messages: the pull-request title adapter deliberately uses the header-only checker.
`body-min-words` is a schema-v1 integer from zero through 100000 with a zero default. It counts only
Unicode-whitespace-delimited prose-body tokens containing a Unicode alphanumeric character;
recognized final footers and punctuation-only or emoji-only tokens are excluded. A nonzero value
does not require an absent optional body, and it is invalid with a forbidden body. Keep its
`body.word-count` diagnostic stable across text, JSON, and GitHub reports.
`required-footer-tokens` and `forbidden-footer-tokens` are schema-v1 presence policies for complete
commits only; pull-request titles remain header-only. Match exact tokens case-insensitively in both
`Token: value` and `Token #value` forms, allow repeats, and do not infer identity, DCO compliance,
or signature validity from `Signed-off-by`. Preserve the one-to-128-character ASCII token grammar,
the 128-entry combined limit, case-insensitive duplicate and overlap rejection, and the reserved
breaking-marker spellings.

Keep footer parsing source-located and bounded. The first valid token must start a content paragraph;
after that boundary, preserve the complete suffix as footer content, including blank lines in
multiline values, and recognize later token starts without requiring another blank line. Emit at
most one `footer.required` diagnostic for the first configured missing token and at most one
`footer.forbidden` diagnostic for the earliest forbidden occurrence, after existing body
diagnostics.

`config init` creates only a new standalone `.yaga.toml` with exclusive no-overwrite semantics. It
must refuse to shadow any effective discovered configuration, never edit `pyproject.toml`, and keep
its deterministic starter template round-trippable through the strict loader.

Branch-name policy lives under `src/yaga/branches/` and remains installed-only. `branch check`
requires one explicit versioned policy and one explicit short name; never inspect Git, infer the
current branch, discover policy, parse an event, normalize `refs/` or remote prefixes, or read an
environment variable in the domain layer. Keep schema v1 closed to an ordered list of one through
64 unique case-sensitive `allowed-patterns`, the bounded portable ASCII name grammar, component-
local `*`, and whole-component `**`. Names must pass syntax independently of wildcard admission.
Preserve first-match reporting, stable `branch.syntax` and `branch.allowed` findings, and exits `0`
for a match, `1` for findings, and `2` for input/policy errors. The 244-byte limit is a deliberate
YAGA portability boundary. CI adapters must select `github.head_ref` for pull requests or guarded
branch-only `github.ref_name` for pushes, copy it through an environment variable, and quote it as
one argument. This provider must not enter either dependency-free Action import graph.

Changed-path coupling lives under `src/yaga/changes/` and remains installed-only. Require one
explicit versioned policy and one explicit two-dot or three-dot Git range; never infer staged,
working-tree, event, branch, or remote state. Keep schema v1 closed to named `when-any` plus
`require-any` rules and its literal/`*`/component-`**` POSIX pattern grammar. Do not add regex,
exclusions, includes, expressions, environment interpolation, or commands without a new reviewed
contract. Resolve both endpoints without fetching, require non-shallow history without legacy graft
overlays, choose exactly one merge base for three-dot comparison, disable
rename/external-diff/textconv behavior, override submodule-ignore configuration so gitlink changes
stay visible, and strictly parse bounded NUL-delimited UTF-8 paths. Treat every revision, Git
response, path, and rule value as untrusted.
Compile and cache each unique pattern once and preserve the shared 10,000,000-unit matcher-work
ceiling. Keep exits `0` for pass, `1` for coupling findings, and `2` for
policy/Git/operational failure. This provider does not enter either dependency-free Action import
graph.

Committed-tree policy lives under `src/yaga/trees/` and remains installed-only. `tree check`
requires one explicit versioned policy and one explicit commit-ish; the CLI may default only the
repository path to `.`. Never infer `HEAD`, discover policy, fetch, parse an event, read tracked
content, inspect the worktree/index/untracked set, or recurse into a gitlink. Schema v1 requires exact
`required-paths` and anchored case-sensitive `forbidden-patterns`; either array may be empty but
their combined bounded total must not be. Reject any required path admitted by a forbidden pattern
as an impossible policy. Preserve canonical UTF-8 POSIX path validation, first-pattern attribution,
one shared 10,000,000-unit matcher-work ceiling, and stable `tree.required` and `tree.forbidden`
findings.

Resolve exactly one bounded non-option commit-ish and its tree using type-checked `rev-parse`, then
enumerate leaf paths with recursive, full-tree, name-only, NUL-delimited `ls-tree`. Keep the Git
executable absolute and outside the repository; use a minimal no-prompt/no-fetch/no-replacement
environment, shell-free bounded execution, strict identities and path records, a 30-second timeout,
a 64 MiB output cap, and 50,000-path limit. Pass global `--no-lazy-fetch` on every Git invocation so
unsupported older clients and missing promised objects fail closed. Validate Git against the
outermost enclosing repository boundary even when the requested directory is a worktree
subdirectory, and resolve bounded `.git` files, linked-worktree `commondir`, and external object
directories plus cycle-safe local alternate chains into that trust boundary; allow at most 128
object directories. Shallow history is valid when the selected objects are present. The policy path
is a runtime input and carries no provenance claim. Bound JSON/text/GitHub diagnostics without
weakening exact aggregate counts, and keep this provider outside both Action import graphs and the
repository-plan v1 provider set until that contract is deliberately revised.

Workflow reference policy lives under `src/yaga/workflows/` and remains installed-CLI-only. Parse
untrusted YAML through the bounded pure-Python `SafeLoader` composition boundary without
constructing Python objects or collapsing mapping pairs. Preserve source marks and duplicate keys;
bound files, bytes, documents, nodes, depth, anchors, aliases, scalar sizes, expanded visits,
references, diagnostics, and displayed values. Keep immutable-reference checking separate from
actionlint and out of both dependency-free composite Action import graphs. Treat `$/` local
references as commit-bound; `./` is a compatibility path whose integrity depends on the caller's
trusted checkout and must not imply an immutable-reference guarantee. Apply the same literal
lowercase SHA-256 digest policy to Docker Actions, job-container images, and service-container
images. Preserve GitHub's exact quoted-empty service-image disable case, but reject dynamic image
expressions instead of approximating the Actions expression language.

`workflow security` is a separate pure-Python provider over immutable, source-located facts from
that same bounded composition. Keep `recommended-v1` frozen to its documented permission,
`write-all`, named-secret, and privileged-checkout rules, and keep it as the no-option default.
`recommended-v2` is the explicit opt-in superset that also requires direct runner-resolved
`actions/checkout` steps to set one literal `with.persist-credentials: false`; it does not inspect
wrapper Actions. Match the runner's slash and backslash path segmentation conservatively, and fail
closed on non-scalar, non-ASCII, duplicate, or runner-environment-colliding input evidence and on
unsupported scalar tags.
`recommended-v3` is the explicit opt-in superset of v2 that adds
`permissions.pull_request_write`. Whenever any `pull_request` trigger is present, reject exact
`pull-requests: write` and `statuses: write` values at workflow and job scope. Mixed-event
workflows and conditionally guarded writer jobs still fail; split the writer into a trusted
`pull_request_target` or `workflow_run` publisher. Those trusted triggers remain allowed by this
rule when `pull_request` is absent, subject to all other profile rules. Keep the rule deliberately
narrow: it does not cover other scopes, alternate tokens, reusable-workflow permission inheritance,
or expressions.
Future defaults require another versioned profile. Exact custom rules are closed, unique, and
canonical.
Do not duplicate actionlint's script injection or schema checks, infer arbitrary job permissions,
expose raw facts, or let this installed provider enter either dependency-free Action import graph.
The privileged-checkout rule deliberately rejects dynamic `ref` or `repository` expressions and
any non-false `allow-unsafe-pr-checkout` value instead of approximating GitHub's expression parser.
The persisted-credentials rule likewise rejects missing, dynamic, non-scalar, or ambiguous inputs
instead of approximating checkout or expression behavior.

`workflow lint` shares `workflow check`'s default and explicit path-selection contract, but it is an
installed-only adapter around the fixed
`rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667`
container rather than a pure-Python schema linter. Keep its digest and hardening flags fixed: invoke
Docker without a shell or host mount. Build only a bounded deterministic synthetic repository:
selected workflows, transitively called local reusable workflows, at most one native
`.github/actionlint.yaml` or `.yml`, and actionlint's chosen local Action manifest (`action.yaml`
before `action.yml`). Stage zero-byte file or empty-directory presence entries, never source
content, for the runtime paths that native Action metadata checks with `stat`.

Stream that archive through a stopped staging container into a private labeled volume. Mount the
volume read-only for linting; keep the container root read-only, disable networking and logging,
drop capabilities, forbid privilege escalation, and fix CPU, memory, swap, and PID ceilings. Keep
valid `$/` compatibility translation source-marked, length-preserving, and private to the snapshot;
never heuristically replace matching text elsewhere. Bound support-file reads, path depth, archive
entries and bytes, process trees, time, stdout, stderr, diagnostics, and decoded JSON. Strictly parse
the closed actionlint result, while accepting only its documented optional diagnostic fields.
Force-remove and verify labeled containers and the private volume on every catchable terminal path;
an uncertain late Docker create or unconfirmed cleanup is an operational failure. Preserve exits `0`
for success, `1` for lint findings, and `2` for operational failure. The lint command and its runner
must remain absent from both dependency-free composite Action import graphs.

The installed-only `repo check` aggregate lives under `src/yaga/repository/`. It requires an
explicit, unique provider list closed to `commit`, `workflow`, `workflow-security`, and
`workflow-lint`; never add an implicit `all` path that changes when a provider is introduced.
Execute in that canonical order, load workflow inputs once, share one bounded composition between
the pure workflow providers, keep provider reports separate, and continue independent providers
after expected operational errors. A pure parse error belongs to the selected pure providers but
must not suppress independent actionlint. Preserve one global GitHub annotation budget and exits
`0` for all passed, `1` for findings only, and `2` when any provider errors. Provider-specific
arguments must fail when their provider is absent. The aggregate remains outside both Action import
graphs and does not replace the event-bound commit Action.

Repository check plans are explicit saved provider selections, never implicit configuration. Keep
their TOML schema versioned, closed, portable, and strictly bounded; reject unknown keys and
versions, unsafe workflow paths, duplicate values, and inconsistent provider arguments. Plans may
hold only checks, workflow paths, and a workflow-security profile or exact rules. Keep repository,
commit configuration, commit/range selection, and output format as runtime CLI options. Do not add
includes, discovery, environment interpolation, expressions, commands, or secret fields. Resolve
plan workflow paths only under the runtime repository, never relative to the plan file. A
pull-request-controlled plan remains an unprivileged quality policy, not a security authority.

The pre-commit provider manifest exposes exactly one `commit-msg` hook. Keep it as a direct
`language: python` adapter to `yaga commit check --file`; do not add shell indirection, filename
filters, duplicated policy arguments, or extra dependencies. Validate the manifest and exercise a
real `pre-commit try-repo` installation when changing the hook or package metadata. Local hooks are
bypassable and cannot determine merge parent count, so they complement rather than replace CI range
checks.

The write-capable root Action uses the same `gate codex-review <operation>` command path through a fixed
`YAGA_ACTION_RUNTIME=1` standard-library bootstrap. It installs no package and makes no network
request for dependencies. Never accept `github-token` as argv, configuration, output, or logs.

The read-only Action uses its own closed `YAGA_COMMIT_ACTION_RUNTIME=1` bootstrap and the public
`github pull-request check` path. It accepts no token or caller-selected revisions, validates one
bounded event against the GitHub runner repository/ref identity, requires `HEAD` to equal the event
head, and never fetches Git history. Keep the pinned Python bootstrap token explicitly empty, and
keep its `edited`-aware workflow separate from the authenticated `CI` workflow used by the Codex
gate. Its PR-head configuration is contributor-controlled and is only a quality signal.

## Action contract

The public inputs are exactly `gate`, `operation`, `github-token`, `prerequisite-workflow`,
`lifecycle-workflow`, `owner-id`, `approval-marker`, `request-timeout`, and `job-timeout-minutes`.
`operation` is closed to `invalidate`, `prepare`, `authorize`, `observe`, `request`, and `finalize`.
Only `prepare` emits the closed `route` capability and `pull_request_number` used by the template;
its routes are `skip`, `done`, `observe`, `owner`, `external`, and `approved`. Candidate identity
never crosses a job output.

YAGA is pre-release; remove obsolete interfaces rather than adding compatibility shims. Provider
identity, status names, polling cadence, and marker grammar are fixed policy rather than caller
inputs. Add a future gate as a separate package with its own closed evidence grammar.

## Consumer trust split

- The `pull_request_target` invalidator's native `Review Policy Boundary` check must be required.
  It verifies exact live state and status capacity, then writes pending for `Codex Review` and
  `CI Gate`. It excludes `closed`, executes no PR code, and posts no comment. Ordinary title/body
  edits instead use the non-required native name `Review Policy Metadata`.
- PR-controlled CI remains unprivileged and ends at `CI Prerequisites`. Its exact bounded run-name
  is `YAGA CI <action> for #<pull-request> at base <full-base-SHA>`, and its pull-request triggers
  are `opened`, `synchronize`, `reopened`, and `ready_for_review`. This PR-controlled result is an
  untrusted quota-saving prerequisite/heuristic, not a security authority.
- The `workflow_run` publisher accepts authenticated completions from both `CI` and
  `YAGA Review Policy`, then resolves the exact current CI/lifecycle pair. It uses no upstream
  artifact or cache. A CI-first `prepare` waits at most two minutes for lifecycle publication, and
  a later lifecycle completion is a second reconcile wake. The later completion is elected before
  any write, with CI winning an exact timestamp tie. Failed CI never requests Codex.
- `prepare` may observe only evidence following an exact current-boundary YAGA request and cannot
  comment. `request-owner` is direct.
  `authorize-external` uses the literal protected environment and records a non-triggering approval
  marker. The separately serialized `request-external` worker consumes that capability and never
  cancels an in-flight request. `observe` never comments. `finalize` independently revalidates
  source CI, live identity, authorization, Codex evidence, and status lineage.
- Every job invokes the pinned action as its sole step with the narrow permissions in `examples/`.

Consumers provide the immutable owner ID through required repository variable
`YAGA_CODEX_OWNER_ID`. Precreate `codex-review-approval` with that owner as the sole required
reviewer, prevent self-review, disable administrator bypass, store no secrets, and add environment
variable `YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. Required-reviewer protection is
plan-limited: verify the public-repository supported plan or private/internal Enterprise scope and
canary the wait before enabling it. Disable Codex automatic reviews before enabling the v2
publisher, then prove the owner request and protected-external approval plus request paths before
admitting normal contributor traffic.
The environment controls only YAGA: a direct human/app `@codex review` comment can still consume
provider quota. Visible unsolicited activity fails closed without a second YAGA request, but a
direct request can still race the final read/POST interval and create temporal ambiguity.

The publisher admits `pull_request` completions only from `.github/workflows/ci.yml` and
`pull_request_target` completions only from `.github/workflows/review-policy.yml`. That admits the
lifecycle wake associated with the default branch while post-merge `push` completions skip every
publisher job before YAGA runs. It also permits a fork head branch named `main`. There are no
schedule, issue-comment, review, merge-queue, or close triggers. A metadata-only edit has a unique
concurrency group and cannot cancel an active boundary. Consumers with another default branch need
to replace only `branches: [main]` in the lifecycle workflow. Keep the `CI` and `YAGA Review Policy`
display names in the publisher trigger synchronized with their authenticated paths.

## Design rules

- Treat event payloads, API responses, comments, reviews, reactions, statuses, and workflow runs as
  untrusted until every required field is parsed and bounded.
- Rely on the required native lifecycle check while validating live state and capacity before
  lifecycle pending writes. Before comments and terminal statuses, revalidate exact current CI,
  default branch, open/ready PR, head/base, unique ownership, lifecycle provenance, authorization,
  exact evidence capability, and latest status lease.
- Accept only exact connector identities and closed outcome grammars. Every eyes reaction and
  outcome, including on `opened`, requires the exact current-boundary Actions-owned YAGA request and
  a strictly later timestamp. `observe` is available only for that already-posted request. Visible
  unsolicited activity fails closed without a duplicate request. A PR-body reaction has no commit
  or base identity, and reviewed-commit evidence does not identify its triggering comment, so the
  request marker provides temporal correlation rather than a native provider binding. Require
  trusted successful status lineage for every older YAGA request before newer evidence or a request;
  a head changed while review is unresolved requires a fresh PR. Do not enable this beta action
  where automatic reviews were not disabled and drained or another review trigger can overlap YAGA.
- External authors require a YAGA request created after environment approval. Approval never reuses
  unsolicited evidence. Immediately before posting, revalidate and re-read the request and
  admissible evidence. The final read/POST interval is not atomic. CI reruns reuse one
  lifecycle-bound request.
- Ordinary metadata edits are no-ops; base edits and drafts revoke inherited success. Closure has
  no publisher trigger, while a post-merge `push` completion skips every publisher job before YAGA
  runs. An already-running job can still race its final live read with a comment or status POST, so
  a marked request/status can land after close and the request can consume quota. Treat this as a
  bounded residual, not a zero-post-close guarantee.
- Fail closed on malformed, incomplete, ambiguous, stale, displaced, or over-budget state. Shared
  heads require distinct commits. A comments, reviews, or reactions page with 100 records requires a
  new PR, as does more than eight older YAGA request boundaries; reaching the status-history ceiling
  requires a new commit.
- Bound request counts, pagination, bodies, event files, descriptions, polling, and all
  attacker-controlled strings. Never log the token or place it in arguments/outputs.
- Reserve `Codex Review` and `CI Gate` for classic statuses. Audit all `statuses: write` and
  `pull-requests: write` workflows because the beta uses the shared Actions identity.

Strict up-to-date `Review Policy Boundary`, `CI Prerequisites`, `Codex Review`, and `CI Gate`
requirements plus required conversation resolution are consumer prerequisites. Merge queues are
unsupported. Merge security also relies on `Maintainer Approval` and audited status/comment writers;
the protected environment and owner ID secure quota authorization. This beta assumes GitHub
delivers every configured lifecycle event, because there is no scheduled repair for a missed
same-head transition.

## Testing and pull requests

Add parser/checker tests for every new commit rule, strict configuration tests for every key,
shell-free Git integration tests for selection behavior, and Typer runner tests for command/exit
contracts. Keep a subprocess smoke proving `python -P -S -m yaga gate codex-review ...` reaches the
dependency-free Action boundary without importing Typer.

Keep generic transport/models/status primitives in `src/yaga/` and provider policy in
`src/yaga/codex/`. Add focused tests for success, failed CI, direct/protected routing, unsolicited
external outcomes, exact request idempotency, malformed/incomplete APIs, deadline reserves,
draft/ready/base/close races, same-head ambiguity, run supersession, status lineage, and
case-insensitive collisions. Repository-contract tests pin public inputs, outputs, permissions,
triggers, fixed environment names, action SHA, and absence of legacy writers.

Run `uv run make ci` before every push. Use Conventional Commit subjects. Preserve behavior,
motivation, security boundary, failure mode, migration impact, and validation in the retained commit
and PR. Fold review fixes and CI repairs into the logical commit they correct.
