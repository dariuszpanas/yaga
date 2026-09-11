# Commands

The installed command groups are deliberately separated. Every command has bounded inputs,
stable text/JSON/GitHub reporting, and exit codes `0` for pass, `1` for policy findings, and `2`
for operational errors. Use `yaga <group> --help` for the exact current option surface.

| Command | Purpose | Typical source |
| --- | --- | --- |
| `yaga branch check` | Validate one short branch name. | `--policy ... --name ...` |
| `yaga change check` | Enforce changed-path coupling. | `--policy ... --range A...B` |
| `yaga commit check` | Check one message, commit, or range. | `--message ...` or `--range ...` |
| `yaga commit quality` | Advisory-check message quality with an optional local model. | `--message ...` or `--range ...` |
| `yaga config init` | Create a starter commit policy. | `--repo .` |
| `yaga config show` | Show effective policy and source. | `--repo .` |
| `yaga github pull-request check` | Check one exact GitHub PR event. | event and checkout context |
| `yaga mode check` | Enforce committed entry modes. | `--policy ... --revision ...` |
| `yaga path check` | Check committed path portability. | `--policy ... --revision ...` |
| `yaga repo check` | Run an explicit provider plan. | `--plan ... --revision ...` |
| `yaga size check` | Enforce committed blob/total limits. | `--policy ... --revision ...` |
| `yaga tree check` | Enforce required/forbidden tree paths. | `--policy ... --revision ...` |
| `yaga workflow check` | Require immutable action references. | explicit workflow paths |
| `yaga workflow security` | Apply the versioned Actions trust policy. | explicit workflow paths |
| `yaga workflow lint` | Run pinned actionlint in a hardened container. | explicit workflow paths |
| `yaga gate agent-review <operation>` | Run the Agent review-gate operation. | trusted Action context |
| `yaga gate agent-review policy check --file <path>` | Validate named review lenses without contacting a provider. | local policy validation |
| `yaga gate agent-review policy plan --file <path>` | Expand lenses into provider-neutral adapter work. | local plan inspection |
| `yaga gate agent-review policy evaluate --file <path> --results <path>` | Validate and aggregate adapter results. | local result evaluation |

## Selection rules

`commit check` accepts exactly one explicit source—message, UTF-8 file, standard input, commit,
or range—and defaults to `HEAD` only when no source is supplied. A commit range is evaluated
oldest first. Git-backed commit checks require complete history and never fetch.

`commit quality` accepts the same source selection and defaults to `HEAD`. It is deliberately
separate from `commit check`: the deterministic Conventional Commit policy remains authoritative,
while a model is an optional advisory signal. The provider is explicit and bounded:

The command also reads non-secret defaults from `[tool.yaga.commit.quality]` or `[commit.quality]`
in the discovered configuration. Use `--config` to select one explicit file; CLI options override
the configured provider, task, model, revision, threshold, region, and generation bound.

```bash
# Pinned local classifier (default)
uv sync --extra quality
yaga commit quality --message "fix: update parser behavior" --offline

# Configurable local instruction model; use a pinned commit of the model
yaga commit quality --task seq2seq --model google/flan-t5-small --revision <sha>

# AWS credentials and region come from the normal AWS SDK chain
uv sync --extra quality-bedrock
yaga commit quality --provider bedrock --region us-east-1
```

The Hugging Face provider supports `classification` and `seq2seq`. Classification uses the
low-quality probability and `--threshold`; seq2seq produces a strict `PASS` or `FLAG` decision
with a bounded reason. The Bedrock adapter uses the Converse API and defaults to Amazon Nova
Micro; `--model` can select another compatible model. YAGA never accepts provider credentials in
policy files or command output. `--offline` is available only for local Hugging Face models.

A model finding returns exit `1`, while missing dependencies, unavailable credentials/models, or
invalid provider output returns exit `2`. Use `--format json` when another tool needs the stable
provider, task, model, decision, score, and reason fields.

### Interpreting quality findings

The default classifier is an advisory signal about how much useful change description a message
resembles. It is not a second Conventional Commit parser and it does not know the repository's
actual diff. For example, `fix: update parser behavior` is structurally valid, but the classifier
may flag it because the subject does not identify what changed or why. A more concrete subject such
as `fix(parser): reject malformed trailer values` is more likely to pass. Adding a body that
explains the behavior change usually lowers the score further.

Classification scores are low-quality probabilities: a larger score is more suspicious, and the
default `0.70` threshold flags the message. The classifier can catch vague subjects, missing
structure, and low-information bodies, but it can also flag intentionally concise messages and
pass fluent but generic text. It cannot reliably enforce configured types, scopes, body minimums,
footers, line limits, or any requirement that depends on the repository diff; use `commit check`
for those exact rules.

Classification providers do not produce an explanation beyond the score. Use `--format json` to
retain the score for diagnostics, or use a seq2seq/Bedrock provider when a bounded natural-language
reason is more useful than a stable probability. Treat model findings as review prompts rather than
proof that a message is invalid.

The standalone `branch`, `change`, `mode`, `path`, `size`, and `tree` providers also accept
`--quiet` (or `-q`) when a caller needs only the exit status. Policy findings are suppressed, while
operational errors still go to standard error and return exit `2`; `commit check` has the same
behavior.

The branch provider takes one explicit short branch name and one explicit policy. The change
provider takes one exact `A..B` or `A...B` range and one explicit policy. The committed-tree
providers (`mode`, `path`, `size`, and `tree`) take one exact commit-ish and one explicit policy;
only `--repo` may default to the current directory. Workflow providers take explicit paths or
their documented default workflow selection.

## Aggregate checks

Repository checks do not imply an `all` provider set. Select providers explicitly, or use a
versioned plan checked into the repository:

```bash
uv run yaga repo check \
  --plan .yaga/checks/ci.toml \
  --commit HEAD \
  --revision HEAD
```

Committed-tree providers require an exact runtime `--revision`. The aggregate resolves that
identity once and keeps each provider's report separate.

Plans are opt-in and versioned. Plan v1 selects only the original commit, workflow,
workflow-security, and workflow-lint providers. Plan v2 can additionally select `mode`, `path`,
`size`, and `tree`, with one repository-relative policy path for each selected committed-tree
provider. There is no implicit `all` selection, plan discovery, includes, expressions, commands,
or environment interpolation.

## CI integration

For pull requests, check the exact event SHAs and use a complete checkout. Pass untrusted values
through quoted environment variables rather than interpolating them into shell source:

```yaml
- name: Check branch policy
  env:
    YAGA_BRANCH_NAME: ${{ github.head_ref }}
  run: >-
    uv run yaga branch check
    --policy .yaga/branch-policy.toml
    --name "$YAGA_BRANCH_NAME"
    --format github
```

The unprivileged pull-request workflow is a quality signal, not a security authority.
