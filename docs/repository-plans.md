# Aggregate repository checks

`yaga repo check` runs a closed, explicitly selected set of the existing providers and keeps their
reports separate. Repeat `--check` with `commit`, `workflow`, `workflow-security`,
`workflow-lint`, `mode`, `path`, `size`, or `tree`; at least one is required. There is deliberately
no implicit `all` selection, so adding a future YAGA provider never changes an existing local
command or CI gate.

```bash
yaga repo check \
  --check commit \
  --check workflow \
  --check workflow-security \
  --check workflow-lint \
  --check mode \
  --check path \
  --check size \
  --check tree \
  --commit HEAD \
  --revision HEAD \
  --workflow-path .github/workflows \
  --workflow-path examples \
  --mode-policy .yaga/mode-policy.toml \
  --path-policy .yaga/path-policy.toml \
  --size-policy .yaga/size-policy.toml \
  --tree-policy .yaga/tree-policy.toml
```

For a stable local-and-CI selection, put only the repository policy in an explicit, versioned plan:

```toml
# .yaga/checks/ci.toml
plan-version = 2
checks = [
  "commit",
  "workflow",
  "workflow-security",
  "workflow-lint",
  "mode",
  "path",
  "size",
  "tree",
]
workflow-paths = [".github/workflows", "examples"]
workflow-security-profile = "recommended-v3"
mode-policy = ".yaga/mode-policy.toml"
path-policy = ".yaga/path-policy.toml"
size-policy = ".yaga/size-policy.toml"
tree-policy = ".yaga/tree-policy.toml"
```

Then keep invocation-specific state on the command line:

```bash
yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD
```

The native CLI can preview that selection before you run it. When inspecting a plan, YAGA reads
only the named TOML file. You can inspect it outside a Git checkout, including on a machine
without Docker.

```bash
yaga repo inspect --plan .yaga/checks/ci.toml
yaga repo inspect --plan .yaga/checks/ci.toml --format json
```

The version-one `repository_plan` report lists the plan version, providers in execution order,
declared policy and workflow paths, effective workflow security profile and rules, required command
options, and external tools used by those providers. An empty workflow path list uses the ordinary
workflow default when a workflow provider runs. The commit provider's default source appears
separately from the required tree revision. The check itself resolves paths and verifies that
inputs and tools are available. Inspection exits 0 for a valid plan or 2 for an input error.

YAGA never discovers a plan implicitly. `--plan` replaces the selection flags `--check`,
`--workflow-path`, `--workflow-security-profile`, `--workflow-security-rule`, and each committed-
tree `--*-policy`; combining them is an input error. `--repo`, `--config`, `--commit`, `--range`,
`--revision`, and `--format` remain runtime options. Plan version 1 remains frozen to the original
four providers and workflow keys. Plan version 2 adds the four committed-tree providers and the
exact `mode-policy`, `path-policy`, `size-policy`, and `tree-policy` keys. A policy key is required
exactly when its provider is selected. Files, arrays, strings, and portable forward-slash paths are
strictly bounded; unknown keys, unknown versions, absolute or parent-traversing paths, duplicate
selections, inconsistent policy/provider pairs, and profile/rule conflicts fail closed. Every plan
path resolves under `--repo`, never beside the plan. Plans have no includes, discovery, environment
interpolation, expressions, commands, or secret fields. Existing symlinks and junctions are
resolved before policy loading and cannot carry a plan policy path outside the runtime repository.

To migrate an existing version 1 plan, save the JSON report from your usual
`yaga repo check --plan .yaga/checks/ci.toml --format json` invocation. Change just
`plan-version = 1` to `plan-version = 2` and rerun the command against the same inputs. Both
versions run the same checks and produce the same report. Keep your workflow paths and security
settings.
Before the edit, `yaga policy check .yaga/checks/ci.toml` accepts the valid plan and reports it as
deprecated, while `--strict` exits 1. After the edit, the strict audit exits 0. Both checks leave
your files untouched. Once on v2, you can opt into a minimum YAGA version with
`required-version = ">=0.5.0"`. To adopt a committed-tree check, add its provider and matching
policy path and pass `--revision` at runtime.

A plan changed by an untrusted pull request controls only that pull request's unprivileged quality
check. It is not a security authority and cannot replace trusted default-branch workflow,
permissions, ruleset, or merge-policy enforcement.

Providers execute once in canonical `commit`, `workflow`, `workflow-security`, `workflow-lint`,
`mode`, `path`, `size`, `tree` order regardless of option order. The commit provider accepts one
`--commit` or `--range` and defaults to `HEAD`; it never fetches or weakens history. Selecting any
committed-tree provider requires one exact `--revision`, while using `--revision` without one is an
input error. The aggregate resolves that commit and tree identity once, then lets each selected
provider perform its own strict enumeration and policy evaluation so provider-specific parsing and
resource contracts remain intact. It never infers `HEAD` for these providers or reuses `--commit`
as their revision. The reference and security checks share a bounded parse of the workflow files
selected by repeatable `--workflow-path` options. Lint adds its bounded transitive support
snapshot. Only plans that select `workflow-lint` require Docker. The standalone CLI and the
composite Action's `check: repo` mode both run repository plans. Provider-specific arguments
are rejected unless their provider is selected. Use
`--workflow-security-profile recommended-v1` for the frozen default,
`--workflow-security-profile recommended-v2` to require checkout credential persistence to be
disabled, `--workflow-security-profile recommended-v3` to additionally reject pull-request
workflows with status or pull-request write authority, or repeat `--workflow-security-rule` for an
exact custom selection in the aggregate command.

Text preserves one section per provider. Versioned JSON embeds each existing provider document
under a `repository_check` envelope. GitHub output uses balanced provider groups and one shared
50-annotation budget. YAGA continues independent providers after expected operational errors: exit
`0` means all passed, exit `1` means findings with no operational error, and exit `2` means at least
one operational error even if other providers found policy violations. Exit `0` and `1` reports go
to standard output; aggregate exit `2` reports go to standard error.

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
