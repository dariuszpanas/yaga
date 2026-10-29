# Pull-request descriptions

Check a description before opening a pull request, fetch its current body locally, or run the same
policy when its body changes in CI. YAGA checks required content, word and character limits,
heading structure, text hygiene, unfinished placeholders, optional task completion, spelling, and
an explicitly selected Markdown template.

These commands are available in YAGA 0.4.0 and later. Install or upgrade the `yaga-cli`
distribution before using them.

```bash
yaga pull-request description check \
  --policy examples/description-policy.toml \
  --template examples/pull-request-template.md \
  --file pr-description.md
```

Use `--stdin` instead of `--file` to read standard input. Exactly one of `--file`, `--stdin`, or
`--pr` is required. File and stdin checks make no network request; `--pr` explicitly fetches the
current GitHub description. The command reads no Git history and discovers no policy or template.
Policy and template paths are explicit filesystem paths, resolved from the current directory.
Omit `--template` when the repository needs content checks without a fixed section layout.

## Check an existing pull request

Use `--pr` with a canonical HTTPS pull-request URL to validate the description currently stored
on GitHub:

```bash
yaga pull-request description check \
  --policy examples/description-policy.toml \
  --template examples/pull-request-template.md \
  --pr https://github.com/OWNER/REPO/pull/123
```

For a positive pull-request number, supply its repository explicitly:

```bash
yaga pull-request description check \
  --policy examples/description-policy.toml \
  --pr 123 --repo OWNER/REPO
```

`--repo OWNER/REPO` selects `github.com`; `--repo HOST/OWNER/REPO` selects a GitHub Enterprise
host. A URL identifies the host and repository itself, so combining a URL with `--repo` is an
error. Repository selection is never inferred from a checkout, current branch, or `GH_REPO`.
`--repo` is only valid with a numeric `--pr`; remote sources cannot be combined with `--file`
or `--stdin`. Use a URL without query parameters, fragments, credentials, or a custom port.

Install the [GitHub CLI](https://cli.github.com/manual/) and authenticate it for the selected
host before running the check. `gh` must be on `PATH` outside the current directory; YAGA rejects
a current-directory executable to avoid implicit Windows executable lookup. YAGA invokes
`gh pr view` without a shell, reuses the caller's
existing authentication, and disables interactive prompts. No checkout or GitHub Actions workflow
is required. Open, closed, and merged pull requests use the same policy, template, and spelling
checks. The report identifies the returned canonical URL and PR number after verifying that they
match the requested pull request.

This reads the current body once. It does not recover the description at merge time, prevent later
edits, or modify the pull request. For a local publication workflow, validate a draft with `--file`,
publish that same file using the GitHub CLI's `--body-file`, then run `--pr` to check what GitHub
stored. The [source-text guidance](#preserve-the-source-text) explains how to preserve Markdown
while writing the draft.

The fetch has a 30-second process timeout, a 512 KiB JSON response limit, and a 64 KiB stderr
limit. The decoded description still has the normal 64 KiB input limit. Missing `gh`, failed
authentication or network access, invalid responses, and exceeded limits return exit `2`.
Raw process output and credentials are not included in error reports. File, stdin, and event
checks continue to work without `gh` or network access.

## Policy

Use a standalone TOML file; this policy is independent of `.yaga.toml` and `[tool.yaga.commit]`.
The smallest policy is `description-policy-version = 1`. This example adds useful review limits:

```toml
description-policy-version = 1
min-words = 20
max-length = 10000
heading-min-level = 2
heading-max-length = 120
placeholders = ["TODO", "TBD", "Describe the change", "Describe the validation"]
require-checked-tasks = false
typos = "skip"
```

| Key | Default | Meaning |
| --- | --- | --- |
| `description-policy-version` | Required | Integer `1` |
| `min-words` | `1` | Minimum prose word count, from `0` through `65536` |
| `max-length` | `65536` | Maximum character count for the complete description, from `1` through `65536` |
| `heading-min-level` | `2` | Lowest permitted Markdown heading level, from `1` through `6`; the default rejects H1 |
| `heading-max-length` | `120` | Maximum heading-text character count, from `1` through `256` |
| `placeholders` | `["TODO", "TBD"]` | Whole prose or list lines that must be replaced; matched without case sensitivity |
| `require-checked-tasks` | `false` | Report unchecked Markdown task items when `true` |
| `typos` | `"skip"` | Set to `"check"` to run the installed Typos CLI |

Unknown keys, unsupported versions, wrong types, and duplicate normalized placeholders are errors.
An empty `placeholders` array disables placeholder matching. A zero word minimum disables that
minimum; the description must still contain meaningful content.
There may be at most 128 placeholders, each from 1 through 128 characters. A policy whose minimum
word count cannot fit within its character maximum is rejected. LF and CRLF line endings are
supported; CRLF becomes LF before counting characters. A bare CR is an input error. The input
byte limit applies before normalization.

Words are whitespace-delimited prose tokens containing at least one Unicode alphanumeric
character. Headings, HTML comments, fenced or indented code, punctuation-only lines, and unchecked task labels
do not satisfy the content requirement. Checked task labels can count. These deterministic checks
cannot establish whether an explanation is accurate or sufficient for a reviewer.

Placeholder matching applies to an entire normalized line, so a sentence discussing a TODO does
not fail just because it contains that word. Prefer complete prompt lines in `placeholders` if
contributors sometimes leave instructions in their descriptions. `N/A` is allowed by default;
repositories can add it to their own placeholder list if an explanation is required instead.

Spelling uses the installed `typos` executable with isolated configuration, bounded input/output,
and a timeout. It does not discover repository spelling configuration. Missing Typos, malformed
output, or a failed process is an operational error rather than a passing spelling result. The
spell check is deterministic and does not use a model. Comments and code blocks are excluded.

## Markdown structure

Heading checks apply even when no template is selected. By default, use H2 through H6 for
sections: the pull request already has a title. Set `heading-min-level = 1` if a repository
deliberately uses H1. Every recognized heading must have text and fit `heading-max-length`.
The limit counts heading text, including inline Markdown syntax, rather than rendered glyphs.

YAGA recognizes top-level [ATX headings](https://spec.commonmark.org/0.31.2/#atx-headings), such as
`## Summary`, and [Setext headings](https://spec.commonmark.org/0.31.2/#setext-headings): a paragraph
followed immediately by an `=` underline for H1 or a `-` underline for H2. A Setext heading can
span multiple paragraph lines. Its whole text is heading content and cannot satisfy the prose
minimum. A blank line separates a paragraph from a following thematic break.

These rules catch a description written as one giant `#` heading even if a short sentence such
as `Done.` follows it. They also catch an oversized H2 and an entire description accidentally
turned into an underlined heading. A description containing only headings fails the independent
meaningful-content check as well.

Raw HTML heading tags, from `<h1>` through `<h6>`, are rejected with
`description.html-heading`; use Markdown headings so the same rules and template checks apply.
Each opening or closing heading tag produces a finding, including tags with attributes or mixed
case. Other HTML such as `<details>` and `<summary>` is permitted by this rule. Tags in comments,
code, or escaped literal markup are excluded. See the [GFM raw HTML grammar](https://github.github.com/gfm/#raw-html)
for the underlying syntax.

Code blocks and HTML comments cannot supply prose or template sections. Copying an entire
description inside a Markdown code fence therefore fails the content requirement. Unclosed
comments and fences also produce `description.markdown` findings.

Text hygiene checks run outside code, comments, and escaped literal markup. They report
suspicious invisible characters, including zero-width spaces, byte-order marks, bidirectional
format controls, Unicode line/paragraph separators, and U+034F. Diagnostics identify code points
and names instead of displaying the invisible character. Ordinary Unicode text, joining
characters, combining marks, and emoji sequences remain supported.

Use spaces in prose: a prose tab produces `description.tab`, while tab-indented code remains
allowed. Literal `\n\n` and `\r\n` sequences produce `description.escaped-newline` because they
often indicate escaped text was copied instead of actual newlines. A plain Windows path such as
`C:\new\notes` does not match that rule. Put literal escape sequences in code notation when
discussing them. Unmatched, unescaped backtick runs produce `description.backtick`; balanced
inline code and fenced blocks remain supported. The checker does not guess whether arbitrary
asterisks or underscores were intended as emphasis.

Unsupported control characters such as NUL, bell, backspace, and form feed are input errors,
including when they occur inside code. A bare CR is also rejected; use LF or CRLF line endings.
These are operational failures with exit `2`, distinct from text hygiene findings with exit `1`.

This is a bounded set of structural quality rules. YAGA does not render GitHub Markdown, validate
every nested block or inline formatting construct, sanitize HTML, or establish that links are
safe. Ordinary emphasis, lists, links, and collapsible details do not fail merely for using markup.

## Preserve the source text

Save the description as literal UTF-8, validate that file, and pass the same file to a publishing
tool. For the GitHub CLI, use `--body-file` instead of interpolating the description into a
command string. In PowerShell, a single-quoted here-string preserves Markdown backticks and
dollar signs:

```powershell
$description = @'
## Summary

Handle `None` request inputs explicitly and preserve existing behavior for valid data.

## Validation

Ran the request parser tests, including missing fields and valid inputs. All checks passed.
'@
$descriptionPath = Join-Path (Get-Location) 'pr-description.md'
[System.IO.File]::WriteAllText(
    $descriptionPath, $description, [System.Text.UTF8Encoding]::new($false)
)
yaga pull-request description check --policy examples/description-policy.toml `
  --template examples/pull-request-template.md --file $descriptionPath
```

A double-quoted PowerShell here-string expands escape sequences, which can turn Markdown
backticks into control characters or remove part of the intended text. The checker reads the
text it receives; it cannot reconstruct already truncated content or infer letters lost before
validation. An explicit template can reveal missing sections. YAGA reports findings and never
rewrites or publishes the description.

## Template matching

Pass the expected Markdown file with `--template`. For example:

```markdown
## Summary

<!-- Explain the problem and resulting behavior. -->

## Validation

<!-- Record checks performed, results, and outstanding checks. -->
```

A completed description for this template could be:

```markdown
## Summary

Reject empty pull-request descriptions so reviewers can understand the change before inspecting
the diff. Require a validation section to make test coverage and remaining checks visible.

## Validation

Ran the policy parser and CLI tests, including empty descriptions and descriptions with missing
template sections. All checks passed.
```

Templates may use the supported ATX or Setext heading forms. Every template heading must occur
exactly once in the description, at the same level, with the same case-sensitive text, and in
template order. The heading form itself may differ: `Summary` followed by a `-` underline matches
`## Summary`. Additional headings are allowed and must also pass the heading rules. Comments and
fenced code do not create headings. YAGA does not compare complete documents byte for byte.

Each required section needs meaningful content before the next heading at the same or a higher
level; prose in a child section contributes to its parent. A section whose complete normalized
prose matches the template's visible prompt content fails. Adding prose makes that section
different; use `placeholders` to reject specific prompt lines even when other text was added.
Put instructional text in HTML comments when it should remain in the authored description.
A template with no supported headings, duplicate headings, raw HTML heading tags, or headings
that violate the selected level/length policy is an operational error. YAGA cannot require a
description to reproduce a forbidden heading. Optional closing `#` markers and outer heading
whitespace are ignored. Unclosed comments or fences produce a finding in a description and an
operational error in a template. Visible template text must also pass the text hygiene rules;
comments and code retain the same exemptions as they do in descriptions.

## GitHub Actions

Use the event adapter to check the body directly from `GITHUB_EVENT_PATH`:

```bash
yaga github pull-request description check \
  --policy examples/description-policy.toml \
  --template examples/pull-request-template.md \
  --event-file "$GITHUB_EVENT_PATH" \
  --format github
```

In an existing unprivileged workflow with YAGA installed and the selected policy and template
available, include body-edit events:

```yaml
on:
  pull_request:
    types: [opened, reopened, synchronize, edited, ready_for_review, converted_to_draft]

permissions:
  contents: read
```

Add the check to the job's steps:

```yaml
- name: Check pull-request description
  run: >-
    yaga github pull-request description check
    --policy .yaga/description-policy.toml
    --template .github/pull_request_template.md
    --event-file "$GITHUB_EVENT_PATH"
    --format github
```

The event parser requires a supported action, matching positive PR numbers, an open state, and a
consistent draft flag. Null or missing bodies become empty descriptions. Duplicate JSON keys,
invalid types, and nonfinite JSON numbers are rejected. The body-only adapter does not require
title, author, repository, or Git ref metadata, and the event file does not authenticate identity.
It does not fetch Git objects, verify the checkout, query live GitHub state, or write comments or
statuses. Pass the event file path; never interpolate the body into shell source.

This is a quality check over the supplied event and files. Its policy is as trusted as the workflow
and checkout supplying it. Commit-policy author exemptions, including the Dependabot option, do
not exempt descriptions. The existing `github pull-request check` still checks titles and commits;
its `title-and-body` option checks a proposed commit message, not this description policy. The
description checker is installed-only and is not part of either composite Action or `repo check`.

## Reports and limits

Both commands support `--format text`, `--format json`, `--format github`, and `--quiet`. Findings
return exit `1`; a passing description returns `0`; invalid input, policy/template errors, and
fetch or spelling process failures return `2`. Quiet mode suppresses result reports but retains
operational errors on standard error.

JSON reports use schema version `1` and kind `pull_request_description`. Diagnostics identify the
failing rule and source line. For `--pr`, `source` is the canonical returned URL and
`pull_request_number` is the verified PR number. Reports retain exact finding counts while storing
at most 256 diagnostics; GitHub output escapes untrusted text and emits at most 50 annotations.
Description, template, and policy files each have a 64 KiB input limit;
GitHub events have a 1 MiB limit. Templates support at most 64 headings. The hard UTF-8 byte limit
applies independently of the configurable character limit.

| Diagnostic | Meaning |
| --- | --- |
| `description.required` | No meaningful prose remains |
| `description.word-count` / `description.length` | Content is below the word minimum or above the character maximum |
| `description.placeholder` | An unfinished placeholder line remains |
| `description.unchecked-task` | A task remains unchecked when completion is required |
| `description.markdown` | A comment or fence is unclosed |
| `description.heading-level` | A Markdown heading is above the permitted minimum level |
| `description.heading-length` | A Markdown heading exceeds the heading-text character limit |
| `description.heading-empty` | A recognized Markdown heading has no text |
| `description.html-heading` | A raw HTML heading tag occurs outside excluded literal content |
| `description.invisible-character` | A suspicious invisible character occurs in prose |
| `description.tab` | A tab occurs outside excluded literal content |
| `description.escaped-newline` | Literal escaped line breaks occur instead of actual newlines |
| `description.backtick` | An unmatched backtick delimiter remains |
| `description.section-required` / `description.section-duplicate` | A required heading is missing or repeated |
| `description.section-order` | Required headings appear out of order |
| `description.section-empty` | A required section has no meaningful content |
| `description.template-unchanged` | A section's complete normalized prose matches the template |
| `description.typo` | Typos reports a possible misspelling |

See the [command index](commands.md), [usage modes](usage.md), and
[commit policy](commit-policy.md) for the other validation surfaces.
