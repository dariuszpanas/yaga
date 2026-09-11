# Troubleshooting

This page is organized around the boundary that most often explains a surprising result: what
YAGA was explicitly given, what it is allowed to inspect, and whether the result is a policy
finding or an operational error.

## Read the exit code first

Every provider uses the same three-way contract:

| Exit | Meaning | Typical response |
| --- | --- | --- |
| `0` | The selected input passed. | Continue the workflow. |
| `1` | The input was valid but violated policy. | Fix the input or change policy deliberately. |
| `2` | YAGA could not safely complete the check. | Fix invocation, configuration, checkout, Git, or resource limits. |

Use JSON when a script needs to distinguish a finding from an error. Human-readable output is
intended for people; GitHub output is escaped and bounded for workflow annotations:

```bash
yaga commit check --format json > report.json
status=$?
```

Do not treat every nonzero result as a policy failure.

## “Configuration not found” or unexpected defaults

Commit checks discover exactly one nearest `.yaga.toml` or `pyproject.toml`; files are not merged.
A same-directory `.yaga.toml` wins. Use `--config` to make the source explicit, and use
`config show` to inspect the selected file and effective defaults:

```bash
yaga config show --repo .
yaga config show --repo . --format json
yaga commit check --repo . --config .yaga.toml --message "feat: example"
```

Standalone provider policies (`.yaga/*-policy.toml`) are never discovered by a provider. Pass
their path explicitly or reference it from a repository plan.

## Quality checks fail before a model result

`commit quality` is an optional advisory and has its own dependency and provider boundary. Install
the local Hugging Face extra before using the default or `seq2seq` mode:

```bash
uv sync --extra quality
yaga commit quality --message "fix(parser): reject malformed trailers"
```

On Linux, the quality extra resolves the CPU-only PyTorch wheel; GPU drivers and CUDA libraries are
not required. The first online run can still download model files, while later runs can be made
network-free with the same model revision and cache directory:

```bash
HF_HOME="$PWD/.yaga-huggingface" yaga commit quality \
  --message "fix(parser): reject malformed trailers" \
  --revision <40-character-model-sha>
HF_HOME="$PWD/.yaga-huggingface" yaga commit quality \
  --message "fix(parser): reject malformed trailers" \
  --revision <40-character-model-sha> --offline
```

If the offline command reports a missing file, repeat the online command with the same model,
revision, task, Python environment, and `HF_HOME`. Do not interpret a cache miss as a policy
finding: missing dependencies, unavailable model files, malformed provider output, and invalid
configuration return exit `2`. A model finding returns exit `1` and is still advisory; use
`commit check` for exact body, footer, paragraph, and scope rules.

For Bedrock, install the separate provider extra and keep AWS credentials outside policy files:

```bash
uv sync --extra quality-bedrock
yaga commit quality --provider bedrock --region us-east-1 \
  --message "fix(parser): reject malformed trailers"
```

Bedrock currently supports classification only and does not use a model revision. Check the AWS
SDK credential chain, region, model availability, and permissions when this command returns exit
`2`; YAGA intentionally emits bounded provider details rather than credentials or raw responses.

Quality output states the selected message line count and body-line count. It also reports the
Hugging Face token window and whether the input was truncated when the tokenizer exposes that
information. These fields describe coverage, not correctness: a title-only report means the
selected commit actually had no body, while a truncated report means the model did not see the
complete bounded prompt.

## “Exactly one source” errors

`commit check` accepts one of `--message`, `--file`, `--stdin`, `--commit`, or `--range`, and
defaults to `HEAD` only when none is supplied. Do not combine them. A range requires complete
history and is evaluated oldest first; YAGA will not fetch missing commits.

For a pull request, pass the exact base and head SHAs supplied by the event, usually as one quoted
environment variable. Do not interpolate untrusted values into shell source.

## Committed-tree checks pass locally but fail in CI

`tree`, `path`, `mode`, and `size` inspect one exact committed tree. They do not inspect the index,
working tree, or untracked files. Confirm that the revision exists in the CI checkout and that the
policy path is available in the trusted checkout used to run the command:

```bash
git cat-file -e "$YAGA_REVISION^{commit}"
yaga tree check --policy .yaga/tree-policy.toml --revision "$YAGA_REVISION"
```

For `size check`, the selected blob objects must also be present. A shallow checkout is supported
only when all required objects exist; use a complete checkout when the revision or objects may be
absent.

## Repository plans reject a path or option

Plans are opt-in and versioned. `--plan` cannot be combined with provider-selection flags such as
`--check`, `--workflow-path`, or `--mode-policy`. Runtime state remains outside the plan:
`--repo`, `--config`, `--commit`, `--range`, `--revision`, and `--format` belong on the command
line.

Plan policy paths are resolved under `--repo`, not beside the plan. Unknown keys, duplicate
providers, absolute or parent-traversing paths, unused provider keys, and a missing required
policy are intentional errors. Start with the smallest explicit plan and add providers one at a
time:

```toml
plan-version = 2
checks = ["tree"]
tree-policy = ".yaga/tree-policy.toml"
```

```bash
yaga repo check --repo . --plan .yaga/checks/ci.toml --revision HEAD
```

## Workflow checks disagree

The workflow providers are independent: `workflow check` validates immutable references,
`workflow security` validates the selected trust profile, and `workflow lint` runs pinned
actionlint in a hardened container. One passing provider does not replace another.

The security profiles are cumulative: `recommended-v1` is the frozen default, v2 additionally
requires `persist-credentials: false` on direct runner-resolved checkout steps, and v3 additionally
rejects exact `pull-requests: write` and `statuses: write` under `pull_request` triggers. Use an
exact custom rule selection only when the repository intentionally owns that policy.

## Preview or publish the docs

Run a clean strict build before committing documentation changes:

```bash
make docs
make docs-serve
```

The preview server serves `http://localhost:9000` by default. Override it with
`make docs-serve DOCS_ADDR=127.0.0.1:8000` if that port is occupied. The Pages workflow publishes
only from `main`; if the build passes locally but deployment fails, inspect the latest `docs.yml`
run and confirm that the repository has GitHub Pages configured for workflow deployment.
