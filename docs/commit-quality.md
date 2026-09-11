# Commit quality advisory

`commit quality` is an optional provider-backed signal for messages that are structurally valid
but may not explain a change clearly in a Git log. It is separate from `commit check`: deterministic
policy remains the merge gate, while the model provides a bounded advisory signal.

## When to use it

Run both checks when exact policy enforcement and broader writing feedback are useful:

```bash
yaga commit check --range origin/main..HEAD
yaga commit quality --range origin/main..HEAD --offline
```

`commit check` owns Conventional Commit syntax, types, scopes, body and footer rules, paragraph
layout, merge-parent behavior, and optional Typos integration. A model result does not prove any
of those rules. Quality findings use exit `1`; missing dependencies, cache failures, malformed
provider output, and other operational failures use exit `2`.

## Input sources and modes

Exactly one source may be supplied; without one, YAGA checks `HEAD`:

| Source | Example | Use |
| --- | --- | --- |
| Message | `--message "fix: reject invalid values"` | Check text before creating a commit. |
| File | `--file .git/COMMIT_EDITMSG` | Integrate with an editor or hook. |
| Standard input | `--stdin` | Feed generated text. |
| Commit | `--commit HEAD~1` | Recheck one existing commit. |
| Range | `--range origin/main..HEAD` | Check commits oldest first. |

The source determines the complete message selected; `--input-mode` determines what reaches the
provider:

```bash
# Default: subject, body, and final footer block.
yaga commit quality --commit HEAD~1 --input-mode message

# Deliberately score only the first line.
yaga commit quality --commit HEAD~1 --input-mode title
```

`message` is the default and includes the body. `title` sends only the first line. Reports include
the selected mode and retain message/body line counts. Both modes are bounded to 12,000 characters
before inference; reports expose the exact character count and character truncation state.
Git-backed selection never fetches, and range results preserve oldest-first order.

## Provider and task matrix

| Provider | Task | Execution | Result |
| --- | --- | --- | --- |
| `huggingface` | `classification` | Local model | Numeric low-quality score and threshold decision. |
| `huggingface` | `seq2seq` | Local instruction model | Strict `PASS` or `FLAG`, optionally with a reason. |
| `bedrock` | `classification` | AWS Converse API | Classification decision outside the runner. |

The default is the pinned Hugging Face classifier. It treats `LABEL_0` as the low-quality
probability and flags scores at or above `0.70`; classification does not explain its score.
Classifier output is bounded and duplicate labels are rejected, so malformed or ambiguous model
responses fail as operational errors rather than silently changing the decision.
Seq2seq responses must begin with `PASS` or `FLAG` and are still advisory. Bedrock supports
classification only, defaults to `amazon.nova-micro-v1:0`, and reads credentials from the normal
AWS SDK chain. Credentials never belong in TOML or reports.

## Configuration

Use `[tool.yaga.commit.quality]` in `pyproject.toml` or `[commit.quality]` in `.yaga.toml`.
Use `--config` for one explicit file; CLI options override configured values:

```toml
[tool.yaga.commit.quality]
provider = "huggingface"
task = "classification"
model = "saridormi/commit-message-quality-codebert"
revision = "30c7895b3eb0270a3246ef3db7b43c837d8e553d"
threshold = 0.70
max-tokens = 32
max-input-tokens = 512
input-mode = "message"
```

Hugging Face revisions must be lowercase hexadecimal commit identifiers. The tokenizer window
defaults to 512 tokens and can be set from 1 through 4096. Character and token bounds are
independent, so a message can fit YAGA's character limit but still be tokenizer-truncated.

## Cache and offline replay

An online Hugging Face run downloads files for the exact revision. Set `HF_HOME` and repeat the
same model, revision, task, and Python environment for an offline proof:

```bash
export HF_HOME="$PWD/.yaga-huggingface"
yaga commit quality --range origin/main..HEAD --revision <model-sha>
yaga commit quality --range origin/main..HEAD --revision <model-sha> --offline
```

`--offline` is Hugging Face-only and must fail on a cache miss rather than using the network. In
CI, cache only the model directory with a key containing OS, Python family, task, model ID, and
revision. Never cache credentials or files produced by untrusted pull-request code.

## Reports and limits

Use text for people, JSON for automation, and GitHub output for an unprivileged advisory job:

```bash
yaga commit quality --commit HEAD --format text
yaga commit quality --commit HEAD --format json > quality.json
yaga commit quality --range origin/main..HEAD --format github
```

Text and JSON identify provider, task, model, revision, input mode, character limit, and per-message
coverage. Hugging Face results also expose measured token count and token truncation; unsupported
measurements are `null`. GitHub output emits warning annotations for flagged messages and one
bounded notice with counts and metadata; provider failures emit an error annotation and exit `2`.

Use `commit check` for exact body, footer, paragraph, scope, and Typos enforcement. Keep quality
advisory checks separate from the dependency-free Agent review runtime and required security gates.
