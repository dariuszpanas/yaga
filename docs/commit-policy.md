# Commit policy

`commit check` is YAGA's first general-purpose provider. It validates Conventional Commit
structure while keeping parser behavior separate from the configured policy.

## Configuration

Configuration is schema version 1 in either `[tool.yaga]` and `[tool.yaga.commit]` in
`pyproject.toml`, or `[commit]` in a standalone `.yaga.toml`:

```toml
[tool.yaga]
config-version = 1

[tool.yaga.commit]
allowed-types = ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"]
type-case = "lower"
scope-policy = "optional"
scope-policy-by-type = { feat = "required", fix = "required" }
allowed-scopes = ["api", "cli", "config"]
scope-case = "lower"
header-max-length = 100
description-min-length = 3
description-max-length = 72
description-ending = "forbid"
breaking-markers = "paired"
required-footer-tokens = ["Refs"]
forbidden-footer-tokens = ["WIP"]
body-policy = "optional"
body-min-words = 8
# Omit body-max-line-length unless this repository wants a wrapping limit.
# Omit body-max-consecutive-single-line-paragraphs unless this repository wants
# to catch sentence-like prose split by blank lines.
dependabot-pull-requests = "skip"
typos = "skip"
merge-commits = "reject"
max-commits = 64
```

YAGA loads exactly one nearest `.yaga.toml` or `pyproject.toml`; files are never merged. An
explicit `--config` selects one source. Unknown keys, invalid types, duplicate normalized tokens,
and unsupported schema versions fail closed.

`scope-policy-by-type` overrides the global scope policy for complete commits and PR titles.
The override replaces only scope presence; `scope-case` and `allowed-scopes` still validate every
scope that is present. Matching is case-insensitive for the type key, while configured type and
scope case policies remain independent. An empty `allowed-scopes` list permits only unscoped
messages, and `allowed-types` must not be empty when supplied.

`description-min-length` and `description-max-length` apply to the description after the type and
optional scope. `description-ending` controls only `.`, `!`, and `?`; it does not rewrite or strip
the message. `breaking-markers` accepts `either` or `paired`; pairing requires both a header `!`
and a recognized final breaking footer, or neither.

## Bodies and footers

`body-min-words` counts Unicode-whitespace-delimited prose tokens that contain a Unicode
alphanumeric character. Recognized final footers are excluded. A nonzero minimum does not make an
optional absent body required, and body minima must be zero when the body is forbidden.

Required and forbidden footer tokens apply to complete commit messages, never pull-request titles.
Matching is exact and case-insensitive and recognizes both `Token: value` and `Token #value`;
repeated tokens are allowed. These are presence checks, not signer, DCO, or signature validation.
Tokens use one to 128 ASCII letters, digits, or hyphens; the two lists share a 128-entry bound,
must not overlap, and cannot use the reserved breaking-marker spellings.

Footer parsing starts only when a valid token begins the first content paragraph or a later
paragraph after a blank line. Once the boundary is found, blank and non-token lines remain part of
the current multiline footer value until a later valid token begins another footer. The stable
`footer.required` and `footer.forbidden` diagnostics retain source-line attribution while bounding
displayed details.

`body-min-length` and `body-min-words` are independent lower bounds. Word counting splits on
Unicode whitespace and counts only tokens containing a Unicode alphanumeric character; punctuation
and emoji-only tokens do not count, and recognized footer content is excluded. A nonzero minimum
does not make an optional body required. Use `body-policy = "required"` when presence matters, and
keep both minima at zero when `body-policy = "forbidden"`.

`body-max-line-length` is an optional upper bound, not a default formatting rule. Omit it to allow
longer prose lines, or set a repository-specific positive limit when commit wrapping is part of the
project’s style. YAGA does not require 72- or 100-column body wrapping.

`body-max-consecutive-single-line-paragraphs` is an optional upper bound for consecutive prose
paragraphs containing one non-empty line. It catches bodies that put a blank line between sentences
without rejecting an intentional one-line paragraph. Wrapped paragraphs and list items break the
run. Omitted or `0` means unlimited; `1` permits standalone short paragraphs while flagging
paragraph-splitting. A one-line validation note or justification is valid.

For example, with `body-max-consecutive-single-line-paragraphs = 1`, this passes because the two
short paragraphs are separate notes and the first ends a sentence:

```text
fix: clarify parser behavior

The parser accepts the legacy form.

Validation: the compatibility test still passes.
```

This fails because the blank lines split one sentence into three one-line prose paragraphs:

```text
fix: clarify parser behavior

The parser accepts the legacy form,

including input received from older clients

when compatibility mode is enabled.
```

The heuristic looks for a continuation: the previous paragraph does not end in `.`, `!`, or `?`,
or the next paragraph begins with a lowercase letter. It is deliberately not a general prose
formatter. Use `0` (or omit the key) when the repository does not want this layout check at all;
use `body-max-line-length` separately if it also wants to constrain physical line width.

YAGA’s own repository policy sets `body-policy = "required"` with a minimum prose length, so normal
human commits include a durable explanation. The event-aware commit Action still skips policy
evaluation for the exact Dependabot bot identity when `dependabot-pull-requests = "skip"`, including
multiline Dependabot commit messages.

## Optional Typos integration

Set `typos = "check"` to run the installed [Typos CLI](https://github.com/crate-ci/typos) against
each selected commit message. YAGA sends the message through standard input, requests Typos JSON
Lines output, and converts each finding into a stable `typos.word` diagnostic. This works with
`--message`, hooks, individual Git commits, and ranges; the default `typos = "skip"` keeps policy
results independent of the tools installed on a developer machine.

```toml
[commit]
typos = "check"
```

The check is intentionally explicit: when enabled, a missing `typos` executable, malformed output,
unexpected exit status, timeout, or excessive output is an operational error (exit `2`) rather than
an ignored warning. Install Typos separately, for example with `cargo install typos-cli --locked`,
and keep project-specific words in Typos' `_typos.toml`. Use Typos' repository-wide action or
pre-commit integration for source files; YAGA's adapter is narrowly scoped to commit messages.

## Optional model quality advisory

Typos catches spelling mistakes, but it cannot tell whether a message explains a meaningful change.
For a broader, opt-in signal, choose a provider and run:

```bash
uv sync --extra quality
yaga commit quality --message "fix: update parser behavior"
yaga commit quality --range origin/main..HEAD --offline

# Local instruction model
yaga commit quality --task seq2seq --model google/flan-t5-small --revision <sha>

# Amazon Bedrock
uv sync --extra quality-bedrock
yaga commit quality --provider bedrock --region us-east-1
```

The command uses `saridormi/commit-message-quality-codebert` at a pinned revision by default. The
model was trained as a binary high/low commit-message quality classifier; YAGA treats its `LABEL_0`
score as a low-quality probability and flags messages at `0.70` or higher. Override the model,
revision, and threshold with `--model`, `--revision`, and `--threshold` when testing a compatible
classifier. The `seq2seq` task uses `AutoTokenizer` and `AutoModelForSeq2SeqLM`, bounds generation
with `--max-tokens`, and accepts only a `PASS`/`FLAG` response. Pin every Hugging Face revision
with a lowercase hexadecimal SHA, so an accidental moving tag cannot silently change the result.
Use `--max-input-tokens` or `quality.max-input-tokens` to tune the tokenizer window for a compatible
model; the default is 512 and the allowed range is 1 through 4096. The effective bound is printed
in text and JSON reports. Hugging Face results additionally state whether each selected message
was truncated at that bound and include the measured input-token count; other providers may leave
those per-message fields unknown.

The Bedrock provider uses the AWS SDK default credential chain and the Converse API. Its default
model is `amazon.nova-micro-v1:0`; use `--model` for a different compatible model and `--region`
to select the runtime region. The adapter sends only the bounded commit message and a fixed
classification prompt. It does not expose AWS credentials, response headers, or raw provider
errors in reports.

Input coverage is deliberately visible but bounded. The selected message is passed to the quality
adapter up to YAGA's 12,000-character provider-input limit. The Hugging Face classification and
seq2seq adapters then tokenize with a 512-token limit, so a very long message can have its tail
truncated by the model even though the report still counts every selected message and body line.
Use `commit check` for exact full-message rules such as body structure, paragraph layout, footer
presence, and Typos findings; treat model output as an advisory signal about the bounded model
input, not proof that every token was semantically reviewed.

All providers are advisory, not a parser, formatter, security boundary, or replacement for the
configured commit policy. It may misunderstand project-specific context and should not be used to
reject automated commits without review. The model and its Python runtime are not imported by
either composite Action runtime, and the default package installation remains dependency-light.
The first online run may download roughly 500 MB of model weights; subsequent `--offline` runs use
the local Hugging Face cache. Use `--format json` for automation and retain the model revision in
the report for reproducibility.

## Git selection and hooks

```bash
yaga commit check --message "feat(cli): add a check"
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
```

Git selection is shell-free, disables replacement refs and lazy fetching, rejects unsafe revisions,
and bounds messages, ranges, output, and diagnostics. Merge-commit behavior is controlled by
`merge-commits`; it is not inferred from the commit subject.

The source modes are deliberately exclusive:

| Source | Checks | Typical use |
| --- | --- | --- |
| `--message` | One supplied message | Editor or test feedback. |
| `--file` | One UTF-8 file | The `commit-msg` hook. |
| `--stdin` | One UTF-8 stream | Shell or editor integration. |
| `--commit` | One Git commit | Inspecting an existing commit. |
| `--range` | Every commit, oldest first | Pull-request or release validation. |

With no source, YAGA checks `HEAD`. It never combines sources or silently changes a missing base.
Git-backed checks require the relevant history and do not fetch it.

YAGA also exposes one direct `commit-msg` pre-commit adapter. Install it explicitly with
`pre-commit install --hook-type commit-msg --install-hooks`. The hook cannot inspect parent
metadata, so the repository-side range check remains authoritative.

## Dependabot and reports

`dependabot-pull-requests = "skip"` applies only to the event-aware GitHub PR command and the
read-only commit Action. It requires the exact `dependabot[bot]` login, `Bot` type, and bounded
positive ID from the parsed PR author object. Ordinary commit and repository checks continue to
run.

Stable diagnostics include `syntax.header`, `type.allowed`, `scope.required`, `header.length`,
`breaking.marker-pair`, `body.word-count`, `footer.required`, and `footer.forbidden`. Use
`--format json` for the versioned machine contract or `--format github` for escaped annotations.

Complete commit messages receive body, footer, and merge-parent rules. Pull-request titles are
checked only as a Conventional Commit header: body minima, footer tokens, breaking-footer pairing,
and merge-parent policy do not apply to the title. The event-aware adapter then checks the exact
event `base.sha..head.sha` commit range separately.
