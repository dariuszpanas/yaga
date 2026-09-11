# Commands

The installed command groups are deliberately separated. Commands have bounded inputs, stable
reports, and exit codes `0` for pass, `1` for policy findings, and `2` for operational errors.
Report formats are command-specific: provider checks, standalone `commit check`, and `commit quality`
support text/JSON/GitHub reporting where documented. Configuration reports remain text/JSON. Use
`yaga <group> --help` for the exact current option surface.

| Command | Purpose | Typical source |
| --- | --- | --- |
| `yaga branch check` | Validate one short branch name. | `--policy ... --name ...` |
| `yaga change check` | Enforce changed-path coupling. | `--policy ... --range A...B` |
| `yaga commit check` | Check one message, commit, or range. | `--message ...` or `--range ...` |
| `yaga commit quality` | Advisory-check message quality with an optional Hugging Face or Bedrock provider. | `--message ...` or `--range ...` |
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
| `yaga gate agent-review policy check --file <path>` | Validate named review lenses without contacting a provider. | local policy validation; `--format text` or `json` |
| `yaga gate agent-review policy plan --file <path>` | Expand lenses into provider-neutral adapter work and emit its digest. | local plan inspection |
| `yaga gate agent-review policy evaluate --file <path> --results <path>` | Validate and aggregate adapter results. | local result evaluation; `--format text`, `json`, or `github` |

## Selection rules

`commit check` accepts exactly one explicit source—message, UTF-8 file, standard input, commit,
or range—and defaults to `HEAD` only when no source is supplied. A commit range is evaluated
oldest first. Git-backed commit checks require complete history and never fetch.

Use `--format github` for ordinary CI when the checked range is already known. It emits escaped
error annotations for each bounded policy finding, followed by a pass/fail summary and compact
rule counts. This mode does not inspect GitHub events or call the API; use
`github pull-request check --format github` when the title, exact event boundary, and Dependabot
identity contract also need to be validated.

`commit quality` accepts the same source selection and defaults to `HEAD`. It is deliberately
separate from `commit check`: the deterministic Conventional Commit policy remains authoritative,
while a model is an optional advisory signal. The provider is explicit and bounded:

Use `--provider huggingface` for local classification or seq2seq inference, or
`--provider bedrock` for AWS-hosted classification. The default provider is Hugging Face; the
Bedrock adapter does not use `--offline`, `--revision`, or Hugging Face input-token settings.
These modes inspect the complete selected message subject to the provider's bounded input window;
the report exposes the selected message shape and any measured truncation. Use `--format github` in
Actions to emit escaped warning annotations for flagged advisory results; provider failures emit an
error annotation and exit `2`. For exact structure,
scope, body, footer, paragraph, or Typos enforcement, run `commit check` as well.

### Quality input modes

Select exactly one source, or let the command use `HEAD` when no source option is present. The
source determines which complete commit message reaches the provider; it does not change the
quality policy or model settings:

| Source | Example | Use it when |
| --- | --- | --- |
| Direct message | `--message "fix: reject invalid values"` | Checking text before creating a commit. |
| UTF-8 file | `--file .git/COMMIT_EDITMSG` | Integrating with a commit editor or hook. |
| Standard input | `--stdin` | Passing generated text without creating a temporary file. |
| One commit | `--commit HEAD~1` | Rechecking one committed message. |
| Commit range | `--range origin/main..HEAD` | Reviewing several commits oldest first. |
| Default | no source option | Checking `HEAD` in a repository. |

For example, these invocations all select one message source and use the same configured provider:

```bash
yaga commit quality --message "fix(parser): reject malformed trailers"
yaga commit quality --file .git/COMMIT_EDITMSG --offline
printf 'docs: explain the cache boundary\n' | yaga commit quality --stdin --offline
yaga commit quality --commit HEAD~1 --format json
yaga commit quality --range origin/main..HEAD --format json
```

The source options are mutually exclusive. `--message`, `--file`, and `--stdin` do not read Git;
`--commit`, `--range`, and the default `HEAD` source use the bounded Git runtime and do not fetch.
Range results preserve oldest-first order, and a missing ref, empty range, shallow boundary, or
selection above the configured commit limit is an operational error with exit `2`.

The command also reads non-secret defaults from `[tool.yaga.commit.quality]` or `[commit.quality]`
in the discovered configuration. Use `--config` to select one explicit file; CLI options override
the configured provider, task, model, revision, threshold, region, generation bound, and Hugging
Face input-token bound.

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
Micro; `--model` can select another compatible model. Bedrock currently supports the
`classification` task only; `seq2seq` is a local Hugging Face mode. YAGA never accepts provider
credentials in policy files or command output. `--offline` is available only for local Hugging
Face models.

### Choosing a quality mode

Use classification when the repository wants a stable numeric signal and a cheap pass/fail
threshold. The default model is pinned by its immutable revision, and `--threshold` controls the
low-quality probability at which a message is flagged. Classification does not explain its score.

Use Hugging Face `seq2seq` when a locally cached instruction model should return a short reason. The
model must follow the bounded `PASS` or `FLAG` response contract; `--max-tokens` limits the generated
response. `--model` and `--revision` are independent, so pin both together when selecting a model
other than the default. A seq2seq result is still advisory and is not treated as a replacement for
the deterministic commit policy.

Use Bedrock when model execution should stay outside the runner. YAGA calls the Bedrock Converse
API for each selected message, obtains credentials from the normal AWS SDK credential chain, and
uses `--region` or the SDK's configured region. No Hugging Face files are downloaded in this mode;
`--offline` is therefore rejected. Keep AWS credentials in the workflow environment or its identity
provider, never in `.yaga.toml`.

All modes receive the complete selected message, including its body and final footer block. The
model tokenizer or provider may apply its own input limit; YAGA bounds the submitted prompt and
reports both the selected line count and the number of body lines included in text and JSON output.
This makes title-only versus multiline model runs visible in CI logs without printing the full
message. The model can therefore notice a vague body, but exact rules such as paragraph layout,
footer presence, or configured scopes remain the responsibility of `commit check`.
The default Hugging Face input window is 512 tokens; configure it with `--max-input-tokens` or
`max-input-tokens` when the selected model supports a different context size. The effective value
is included in text and JSON reports. Hugging Face results also report the measured input-token
count and whether each message was actually truncated at that window; `null` is used when the
provider cannot expose that fact.

### Hugging Face cache and offline replay

The first online Hugging Face invocation downloads the tokenizer and model for the exact revision.
The files are stored in the normal Hugging Face cache, or in `HF_HOME` when that variable is set.
An offline invocation sets the provider's local-files-only loading mode and never contacts the Hub:

```bash
export HF_HOME="$PWD/.yaga-huggingface"
yaga commit quality --range origin/main..HEAD --revision <model-sha>
yaga commit quality --range origin/main..HEAD --revision <model-sha> --offline
```

The second command succeeds only when the first command populated the same cache with the same
model revision and task. Cache the directory between CI runs, using a key that includes the runner
OS, Python/runtime family, task, and model revision. Do not cache credential directories or put
provider tokens in the cache. A cache miss should be handled by one intentional online warm-up
step followed by the offline proof step; this makes accidental network access visible in CI.

For a repository configuration, put non-secret defaults in the policy and keep runtime mode
selection explicit:

```toml
[tool.yaga.commit.quality]
provider = "huggingface"
task = "classification"
model = "saridormi/commit-message-quality-codebert"
revision = "30c7895b3eb0270a3246ef3db7b43c837d8e553d"
threshold = 0.70
max-tokens = 32
max-input-tokens = 512
```

The precedence is CLI option, then the selected configuration file, then the provider default.
`--config` changes which policy file supplies defaults; it does not merge files. In particular,
`--offline` is a per-invocation runtime choice and is not a policy-file credential or side effect.

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
