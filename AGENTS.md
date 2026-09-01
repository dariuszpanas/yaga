# YAGA repository agent guidance

Read `CONTRIBUTING.md` before changing code or opening a pull request. Start with `git status
--short`, `git branch --show-current`, and `git log -1 --oneline`; preserve unrelated work, stage
explicit paths, and inspect both working and cached diffs. Feature branches use `feat/<topic>` and
fixes use `fix/<topic>`.

YAGA is an extensible CLI for checks that work locally and in CI. Keep the Conventional Commit
checker under `src/yaga/commits/`, installed Typer commands under `src/yaga/commands/`, and the
retained Codex review gate under `src/yaga/codex/`. Do not discard the gate experiment: expose its
operations through `yaga gate` and keep the composite Action as a supported adapter.

## Preserve the CLI contract

- `commit check` accepts one explicit source or defaults to `HEAD`; never silently combine sources,
  fetch history, reduce a Git selection, or ignore a missing base.
- Treat commit messages, Git output, revisions, paths, and displayed labels as untrusted. Bound
  inputs and output, invoke Git without a shell, reject option-like/split revisions, and sanitize
  terminal text.
- Load exactly one nearest `.yaga.toml` or `pyproject.toml`, or one explicit `--config`; reject
  unknown keys, invalid types, duplicate normalized tokens, and unsupported schema versions.
- Keep parser structure separate from configurable policy. Stable diagnostic codes, exit codes
  0/1/2, and the versioned JSON document are public pre-release contracts.
- Keep `scope-policy-by-type` as an empty-default schema-v1 mapping with at most 128 safe,
  case-insensitively unique type keys and closed presence-policy values. Overrides replace the
  global scope policy for full commits and PR titles; `scope-case` and `allowed-scopes` remain
  independent. Reject keys outside configured allowed types and impossible allowed-scope
  reachability. Treat header-minimum validation as a conservative structural bound rather than an
  exact satisfiability proof, and preserve `scope.required` and `scope.forbidden`.
- Keep `breaking-markers` in configuration schema v1 with closed values `either` and `paired` and
  the default `either`. `paired` requires both the header `!` and a recognized final breaking
  footer, or neither; exactly one reports `breaking.marker-pair`. Apply it to complete commits but
  never to pull-request titles checked through the header-only boundary.
- Keep `body-min-words` in configuration schema v1 as a zero-default integer from zero through
  100000. Count only Unicode-whitespace-delimited prose-body tokens containing a Unicode
  alphanumeric character, exclude recognized final footers, and do not make an optional absent
  body required. Reject a nonzero minimum when body policy is forbidden, and preserve
  `body.word-count` in every report.
- Keep `required-footer-tokens` and `forbidden-footer-tokens` in schema v1 as empty-default,
  full-commit-only presence checks. Match exact tokens case-insensitively in `Token: value` and
  `Token #value`, allow repeats, and never imply that `Signed-off-by` proves identity, DCO
  compliance, or a signature. Preserve the one-to-128-character ASCII token grammar, 128-entry
  combined bound, duplicate/overlap and breaking-token rejection, paragraph-boundary start,
  multiline suffix parsing, and bounded `footer.required`/`footer.forbidden` diagnostics. PR
  titles remain header-only.
- Keep `workflow check`, `workflow security`, and `workflow lint` on the same default and explicit
  path-selection contract. They respectively own immutable-reference policy, a narrow pure-Python
  trust policy, and pinned actionlint syntax checks. Freeze the documented `recommended-v1`
  security rules and keep them as the no-option default. `recommended-v2` is the explicit opt-in
  superset that also requires every direct runner-resolved `actions/checkout` step to set exactly
  one literal `with.persist-credentials: false`; wrapper Actions remain outside that rule. Match the
  runner's remote-reference segmentation conservatively and reject ambiguous scalar tags or input
  environment-name collisions. `recommended-v3` is the explicit v2 superset that adds
  `permissions.pull_request_write`: whenever any `pull_request` trigger exists, reject exact
  `pull-requests: write` and `statuses: write` values at workflow and job scope. Mixed-event and
  conditionally guarded writer jobs fail; split trusted writers into `pull_request_target` or
  `workflow_run` publishers. This rule does not cover other scopes, alternate tokens, reusable
  permission inheritance, or expressions. Future defaults require a new profile. Do not merge the
  providers' diagnostics or imply that one replaces another. Immutable-reference policy includes
  `uses`, job-container images, and service-container images; container images require literal
  lowercase SHA-256 digests, except for GitHub's quoted-empty disabled service image.
- Keep privileged checkout analysis fail-closed without emulating GitHub expressions: on
  `pull_request_target` or `workflow_run`, dynamic `actions/checkout` `ref` or `repository` inputs
  fail, as does any `allow-unsafe-pr-checkout` value other than literal `false`.
- Keep `repo check` installed-only and explicitly driven by either arguments or one named,
  versioned plan. Require a unique provider set;
  run `commit`, `workflow`, `workflow-security`, and `workflow-lint` in canonical order; share one
  bounded workflow load and pure-Python parse; and keep child reports separate. Do not add an
  implicit `all`, silently skip unavailable Docker, or weaken exit precedence: provider errors
  require exit 2 even alongside findings.
- Keep repository check plans opt-in, closed, portable, and bounded. They may select only providers,
  workflow paths, and a workflow-security profile or exact rule set; commit source/configuration and
  output format remain runtime options. Reject unknown keys/versions, duplicate selections, unsafe
  paths, selection-flag mixing, and inconsistent provider arguments. Resolve plan paths under the
  runtime repository rather than beside the plan. Never add plan discovery, includes, environment
  interpolation, expressions, commands, or secrets. A PR-controlled plan is quality policy only and
  never trusted security authority.
- Keep `.pre-commit-hooks.yaml` as one direct Python `commit-msg` adapter to
  `yaga commit check --file`. Do not add a wrapper, filters, policy arguments, or hook-only
  dependencies; local hooks are bypassable and cannot enforce merge parent policy.
- Keep `change check` installed-only and require an explicit versioned policy plus exact `A..B` or
  `A...B` range. Schema v1 is closed to bounded named `when-any`/`require-any` rules and
  case-sensitive repository-relative POSIX patterns with literals, `*`, and component-level `**`.
  Never infer working-tree, staged, event, branch, or remote state; never fetch. Require non-shallow
  history without legacy graft overlays, an unambiguous three-dot merge base, bounded NUL-delimited
  Git paths, visible gitlink changes, disabled rename/external-diff/textconv behavior,
  unique-pattern caching, and the shared 10,000,000-unit matcher-work ceiling. Keep it out of both
  Action import graphs.
- Keep `branch check` installed-only and require an explicit versioned policy plus exact short
  branch name. Never inspect Git, infer checkout or event state, discover policy, normalize full
  refs/remotes, or read environment in the domain layer. Schema v1 is a bounded ordered list of
  unique case-sensitive `allowed-patterns` over the portable ASCII name grammar, with component-
  local `*` and whole-component `**`. Validate names independently, report only `branch.syntax` or
  `branch.allowed`, preserve the first matching pattern, and keep the 244-byte portability cap.
  CI may pass guarded `github.head_ref` or branch-only `github.ref_name` through a quoted environment
  variable. Keep the provider out of both Action import graphs.
- Keep `tree check` installed-only and require an explicit versioned policy plus exact commit-ish;
  only `--repo` may default to `.`. Never infer HEAD, discover policy, fetch, parse events, read
  tracked file contents, inspect worktree/index/untracked state, or recurse into gitlinks. Schema v1 is
  bounded exact `required-paths` plus anchored case-sensitive `forbidden-patterns`, with
  component-local `*` and whole-component `**`; reject required/forbidden overlap and preserve
  first-pattern attribution, the shared 10,000,000-unit matcher-work ceiling, `tree.required`, and
  `tree.forbidden`. Resolve one commit and tree, then use shell-free bounded NUL-delimited
  full-tree `ls-tree` with an absolute external Git, global `--no-lazy-fetch`, a minimal
  no-prompt/no-replacement environment, 30-second timeout, 64 MiB output, and 50,000 canonical
  UTF-8 path bounds. Validate Git against the outermost enclosing repository even when `--repo`
  names a worktree subdirectory, including bounded `.git`, linked-worktree `commondir`, and external
  object-directory resolution plus cycle-safe local alternate chains capped at 128 directories;
  unsupported older Git clients fail closed. Shallow repositories are allowed when the selected
  objects exist; reject legacy graft overlays before resolution and recheck after enumeration. Keep
  the provider out of both Action graphs and repository-plan v1 until explicitly added.
- Keep `size check` installed-only and require an explicit versioned policy plus exact commit-ish;
  only `--repo` may default to `.`. Never infer HEAD, discover policy, fetch, parse events, inspect
  worktree/index/untracked state, or recurse into gitlinks. Schema v1 uses a required nonnegative
  default blob limit, optional total limit, and at most 128 ordered first-match path overrides with
  the canonical anchored literal/component-`*`/whole-component-`**` grammar and one shared
  10,000,000-unit work ceiling. Keep every individual and aggregate byte count at or below the
  2^53 - 1 portable-integer limit. Resolve one commit and tree, then strictly parse bounded
  recursive full-tree `ls-tree` object metadata. Count regular, executable, and symlink blob bytes; exclude
  gitlinks; count duplicate OIDs per path; treat LFS pointers as stored pointer bytes; preserve
  `size.blob` then `size.total` ordering, 50,000-entry and 64 MiB caps, and shallow support only when
  all selected objects exist. Reject legacy graft overlays before resolution and recheck after
  enumeration. Keep it out of both Action graphs and repository-plan v1.
- The installed CLI may use the locked Typer dependency. The write-capable root Action may not
  import Typer, CLI command modules, commit-policy modules, Rich, Click, or site packages. Preserve
  the fixed dependency-free `YAGA_ACTION_RUNTIME=1` bootstrap and shared gate dispatcher. Keep the
  read-only commit Action on its separate `YAGA_COMMIT_ACTION_RUNTIME=1` bootstrap; it may import
  commit modules but not Typer, commands, Codex policy, GitHub REST/token code, or site packages.

## Preserve the trust boundary

- Treat events, inputs, API responses, comments, reviews, reactions, statuses, and run metadata as
  untrusted until strictly parsed and bounded.
- Keep both composite-Action import graphs dependency-free and never expose `github-token` in
  arguments, output, status, logs, or exceptions. Keep the commit Action's pinned Python bootstrap
  token explicitly empty.
- Keep PR CI unprivileged. Write-capable YAGA operations run only from trusted default-branch
  `pull_request_target` or `workflow_run` workflows and never consume PR code, artifacts, or cache.
- Treat PR CI as a quota-saving heuristic, not a security authority. Merge security also relies on
  maintainer approval, required review, and audited write-capable workflows/integrations.
- Require the native lifecycle check, then validate live state and status capacity before lifecycle
  pending writes. Request and terminal paths revalidate live state after any queue/environment wait
  and immediately around their write.
- Keep external authorization split: cancel stale per-PR `authorize-external` waits, record only a
  non-triggering protected approval marker, then let the separately PR-serialized request worker run
  with `cancel-in-progress: false`. Only the literal protected `codex-review-approval` route may
  authorize a non-owner PR.
- Require repository variable `YAGA_CODEX_OWNER_ID`. The environment must have that owner as sole
  required reviewer, prevent self-review, disable admin bypass, store no secrets, and provide
  `YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. Verify plan support and canary behavior.
- Require the exact current-boundary Actions-owned YAGA request before every accepted eyes reaction
  or outcome, including `opened`, with evidence strictly later than the request. Visible unsolicited
  connector activity fails closed without a duplicate request, and `observe` requires that request.
- Fail closed on malformed, incomplete, ambiguous, stale, displaced, or over-budget evidence.
- Bound pagination, responses, request bodies, event files, descriptions, polling, and all
  attacker-controlled strings.
- Treat 100 comments, reviews, or reactions as incomplete and require a new PR. Require a new commit
  before 100 total visible statuses across all contexts or the per-SHA/context status ceiling.
- Reserve `Codex Review` and `CI Gate` for classic statuses; workflow/job/check names must differ.
  Audit every `statuses: write` and `pull-requests: write` path because the beta uses the shared
  Actions identity for both statuses and PR conversation comments.
- Preserve both authenticated `CI` and `YAGA Review Policy` completion wakes, deterministic
  later-completion election before writes (CI wins timestamp ties), the two-minute CI-first prepare
  wait, and exact workflow-path/event entry guards. Lifecycle completions on the `main` base branch
  must run, while post-merge `push` completions skip every publisher job before YAGA runs.
- Disable automatic reviews before enabling the v2 publisher. Prove the owner request and
  protected-external approval plus request paths before normal contributor traffic. Freeze new PRs,
  reach zero open PRs, and drain existing Codex tasks before activation; all evidence correlation
  also requires preventing overlapping direct or integration-triggered reviews and trusted success
  lineage for every older YAGA request.
- External approval authorizes only the exact YAGA request and never reuses unsolicited evidence.
- Ordinary metadata edits use non-required `Review Policy Metadata`; real lifecycle events use
  required `Review Policy Boundary`. The beta assumes GitHub delivers those configured events.
- Never promise zero post-close writes: close can race the last live read and a comment/status POST.
  YAGA cannot stop direct human/app `@codex review` comments from consuming provider quota.

## Make reviewable changes

Keep generic GitHub/model/status primitives under `src/yaga/` and gate policy under its provider
package. The public operations include `authorize`; `prepare` emits closed `route` values including
`approved` plus `pull_request_number`. Prefer focused modules and tests. Pin every third-party
example action to an audited full SHA. Run `uv run make ci` before push; it covers locked
dependencies, Ruff, ty, pinned actionlint, tests, and package build. Use Conventional Commits and
fold review/CI fixes into the logical commit. Keep the installed CLI entrypoint UTF-8-safe before
Typer renders untrusted text. The build gate must install hash-locked runtime wheels, add the fresh
YAGA wheel without dependency resolution, and execute it outside the checkout.

Keep installed workflow-policy parsing and reporting under `src/yaga/workflows/`. Its YAML boundary
must remain resource-bounded, preserve duplicate mapping pairs, and never enter either
dependency-free Action runtime. `$/` local references are commit-bound; `./` depends on the
caller's trusted checkout and must not be described as independently immutable.

Keep `workflow lint` installed-only and out of both Action import graphs. Its actionlint image
digest and Docker hardening flags are fixed: no shell or host mount; use a deterministic bounded
synthetic repository in a private labeled volume, a stopped staging container, and a read-only lint
mount.
Include transitive local reusable workflows, at most one native actionlint config, the
`action.yaml`-before-`action.yml` local manifest, and only zero-byte file or empty-directory runtime
presence entries. Keep `$/` translation exact and source-marked. Disable network and logging; bound
CPU, memory, PIDs, process trees, time, archive, and output; and verify container/volume cleanup.
Expose only sanitized text, versioned JSON, and escaped GitHub reports with exits 0/1/2.
