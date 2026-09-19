# GitHub workflow checks

`yaga workflow check` catches mutable third-party Action, reusable-workflow, job-container, and
service-container references before they become a supply-chain regression. With no paths it checks
direct `.yml` and `.yaml` children of `.github/workflows`; explicit file or directory selections
make examples and other workflow sets checkable with the same policy:

```bash
yaga workflow check
yaga workflow check .github/workflows examples --format github
```

External GitHub references must use a full lowercase 40-character commit SHA. Step-level local
Actions may use `./` or `$/` repository paths, while job-level local references must select one
direct `.github/workflows/*.yml` or `.yaml` reusable workflow. Docker Actions must use a lowercase
SHA-256 image digest. The scalar and mapping forms of `jobs.<job>.container` and every
`jobs.<job>.services.<service>.image` use that same literal digest policy. A quoted empty service
image is allowed because GitHub treats it as disabled; dynamic expressions remain unverifiable and
fail closed. Expressions, ambiguous paths, tags, branches, abbreviated or uppercase SHAs, and
references in the wrong step/job context fail policy. This is a lexical immutability check: it does
not verify registry availability, signatures, provenance, or vulnerability status. See GitHub's
[job and service container syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idcontainer)
and Docker's [digest-pinning behavior](https://docs.docker.com/reference/cli/docker/image/pull/#pull-an-image-by-digest-immutable-identifier).

The `$/` self-repository syntax requires github.com and Actions runner 2.336.0 or newer; use `./`
for GitHub Enterprise Server or older self-hosted runners. Prefer `$/` when it is available: `./`
executes from the checked-out workspace, so its integrity depends on an exact trusted checkout and
it must not consume untrusted pull-request code in a write-capable workflow.

The checker strictly bounds selected files, bytes, YAML nodes, depth, aliases, scalar sizes,
references, and diagnostics. Its reported reference count includes selected `uses` values and
container images. It composes a YAML node graph without constructing Python objects, so duplicate
keys and merge keys remain visible and fail closed. Text, versioned JSON, and escaped GitHub
annotations use exit `0` for success, `1` for policy findings, and `2` for discovery, input, YAML,
or resource-limit errors.

`yaga workflow security` applies a narrow, pure-Python trust policy to the same bounded workflow
selection. The default, frozen `recommended-v1` profile requires an explicit read-only top-level
`permissions` boundary, keeps write scopes at individual jobs, rejects `write-all`, requires named
secret handoff instead of `secrets: inherit`, and hardens `actions/checkout` under privileged
`pull_request_target` or `workflow_run`. In those workflows, `ref` and `repository` selections must
be literal rather than dynamic expressions, while `allow-unsafe-pr-checkout` must be absent or the
literal value `false`. These rules follow GitHub's
[secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use) and
[permission model](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions),
but intentionally remain a small policy checker rather than a replacement for CodeQL, Scorecard,
or a broader Actions security scanner.

The frozen `recommended-v1` profile exposes five independently selectable rule IDs:

- `permissions.explicit` requires a top-level permission boundary.
- `permissions.top_level_write` moves mapping write scopes from the workflow to individual jobs.
- `permissions.write_all` rejects scalar `write-all` at workflow or job scope.
- `secrets.inherit` requires reusable-workflow jobs to name each forwarded secret.
- `checkout.untrusted_ref` applies the privileged-checkout restrictions above.

The opt-in `recommended-v2` profile contains those same five rules and adds
`checkout.persist_credentials`. Every direct step that the runner resolves to the
`actions/checkout` repository must provide exactly one scalar `with.persist-credentials` value with
the literal spelling `false`; plain and quoted values are accepted. A missing input, another value,
an expression, a non-scalar value, or an ambiguous duplicate fails closed at the checkout step.
Input names must be scalar ASCII values so Unicode or environment-name collisions cannot shadow
the required setting. This rule covers direct `actions/checkout` calls only: a wrapper Action must
enforce and document its own credential handling.

The opt-in `recommended-v3` profile contains all six `recommended-v2` rules and adds
`permissions.pull_request_write`. Whenever a workflow has any `pull_request` trigger, this rule
rejects the exact `pull-requests: write` and `statuses: write` permissions at both workflow and job
scope. A mixed-event workflow remains pull-request-triggered, and a job-level `if` condition does
not prove that a writer is unreachable from untrusted pull-request code. Split such a writer into a
trusted publisher triggered by `pull_request_target` or `workflow_run`; those triggers are allowed
by this rule when the workflow has no `pull_request` trigger, while the profile's other security
rules still apply. This deliberately narrow rule does not cover other write scopes, alternate
tokens, reusable-workflow permission inheritance, or expressions.

```bash
yaga workflow security
yaga workflow security --profile recommended-v2
yaga workflow security --profile recommended-v3
yaga workflow security .github/workflows examples --format github
yaga workflow security \
  --rule permissions.explicit \
  --rule checkout.untrusted_ref
```

With no selection options, YAGA still selects the frozen `recommended-v1`; opting in to
`recommended-v2` or `recommended-v3` is an explicit policy migration. Adding another future
default requires another versioned profile instead of silently changing an existing gate. Repeat
`--rule` to replace the profile with one exact, unique custom rule set; do not combine custom rules
with `--profile`. Text, versioned JSON, and escaped GitHub output preserve the same `0`/`1`/`2`
success, finding, and operational-error contract as the immutable-reference checker.

`yaga workflow lint` complements that pure-Python reference policy with GitHub workflow syntax and
schema checks. It accepts the same paths as `workflow check`: no paths selects direct `.yml` and
`.yaml` children of `.github/workflows`, while explicit files or direct-child directories select
other workflow sets.

```bash
yaga workflow lint
yaga workflow lint .github/workflows examples --format github
```

Linting requires Docker and runs the exact
`rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667`
container. YAGA does not bind the checkout into that container. It builds a bounded synthetic
repository containing the selected workflows, their transitive local reusable workflows, the one
native actionlint configuration, and local Action metadata. For Action metadata, native precedence
is preserved (`action.yaml` before `action.yml`); declared runtime files are represented only by
zero-byte existence markers, so their source is neither copied nor parsed. At most one of
`.github/actionlint.yaml` and `.github/actionlint.yml` may exist, and YAGA passes that file to
actionlint explicitly. Ambiguous configurations fail instead of silently choosing one.

The deterministic snapshot is streamed as an archive into a private, labeled Docker volume through
a stopped staging container. Selected workflow content still reaches actionlint through standard
input, while the read-only snapshot lets native local-workflow, local-Action, and configuration
checks work without a host mount. Valid `$/` self-repository references are translated to `./` only
inside this private snapshot because pinned actionlint predates that GitHub syntax; source-marked
rewriting preserves locations and never changes the checked-out file or reported path. A scalar
spelling that cannot be translated exactly fails closed.

The lint container has networking disabled, a read-only root and snapshot, all capabilities
dropped, `no-new-privileges`, fixed CPU and memory ceilings, a PID limit, and no Docker log driver.
YAGA bounds discovery, support files, paths, YAML structure, archive entries and bytes, subprocess
trees, time, output, diagnostics, and decoded JSON. It force-removes and verifies every labeled
container and private volume, treating unconfirmed cleanup as an operational failure and naming the
generated resource that needs manual removal. The configured Docker daemon or context may be
remote and is therefore trusted with the bounded snapshot until cleanup is verified. An
uncatchable process termination can bypass cleanup; leftover resources can be found
with Docker's `label=io.yaga.actionlint.run` filter and should be inspected before removal. This is
a pinned adapter around native actionlint, not a pure-Python schema linter. Sanitized text,
versioned JSON, and escaped GitHub reports use exit `0` for success, `1` for lint findings, and `2`
for Docker, snapshot, cleanup, input, or malformed-tool-output failures.

All three workflow commands are installed-CLI providers, not composite Actions. Run them after
installing the locked YAGA environment in ordinary unprivileged CI. Keep immutable-reference,
security, and syntax diagnostics separate; none replaces another.

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
