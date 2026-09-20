# Commit policy direction for 0.2.0

This is a development plan, not a list of features already released. The first implementation
adds per-type body requirements and an optional total prose limit. Later items need their own
design and validation before becoming public configuration.

## The problem to solve

A useful commit records what changed and enough context to understand why. Teams also use its
structure for release classification, issue references, and review policy. These are different
needs: passing a punctuation rule does not prove a message is useful, and a long body can be
less helpful than a short explanation.

YAGA should make those choices explicit and apply them consistently to the inputs a project
actually uses: local messages, editor files, historical commits, and pull-request titles.
Configuration should describe policy without requiring a program to execute it.

## What the surrounding tools teach us

- [commitlint](https://commitlint.js.org/reference/rules.html) offers detailed component rules;
  its [rule configuration](https://commitlint.js.org/reference/rules-configuration.html) also
  separates severity from conditions. The useful lesson is control and gradual adoption,
  not copying every casing convention or inversion into YAGA.
- [Gitlint](https://jorisroovers.com/gitlint/latest/rules/builtin_rules/) emphasizes readability,
  configurable limits, and tailored rules, including checks beyond conventional structure.
  Its [configuration precedence](https://jorisroovers.com/gitlint/latest/configuration/) permits
  message-level overrides. YAGA should keep policy outside the message being evaluated.
- [Commitizen](https://commitizen-tools.github.io/commitizen/commands/check/) puts validation
  alongside authoring and release commands. Helping users prepare a valid message is valuable;
  quietly exempting broad text prefixes is not the same as verifying a Git merge identity.
- [Cocogitto](https://docs.cocogitto.io/reference/config.html) connects commit conventions with
  versioning, changelogs, and monorepos. YAGA should provide dependable checks and reports
  without requiring users to replace their release tooling.
- [git-cliff](https://git-cliff.org/docs/configuration/git/) demonstrates the downstream need
  for classification and reference extraction. Checking stored input and transforming release
  notes should remain separate operations.
- [semantic-pull-request](https://github.com/amannn/action-semantic-pull-request) focuses on the
  title that often becomes a squash commit. The meaningful input depends on merge strategy;
  validating every intermediate commit is not universally the best default workflow.

These are observations from the tools' primary documentation, reviewed on 2026-09-20. They are
not a popularity ranking or a promise of compatibility with their configurations.

## YAGA's place

YAGA combines message checks with workflow and committed-tree policy, explicit Git selection,
bounded processing, and shared local/CI reporting. Its trusted GitHub adapter can verify the
event and checked-out objects before applying account exemptions. Those properties are worth
preserving as customization grows.

The main gaps are flexibility and usability: content requirements currently apply mostly to
all commit types alike; staged adoption cannot distinguish warnings from errors; reference and
footer-value policies are limited; and authors receive less help preparing messages than
checking them. Conventional structure is also mandatory today, excluding projects that want
readability checks on ordinary titles.

## Defaults and customization

Keep structural validity separate from style. Do not impose universal capitalization, punctuation,
mandatory prose, dictionaries, or artificial minimum lengths. A project may enable these choices,
but the documentation must say what each rule measures and what it cannot prove.

Keep existing schema-v1 defaults stable. A future recommended setup should generate explicit,
editable choices or select a frozen versioned profile. It must not silently change existing
repositories when YAGA is upgraded. Unknown keys and contradictory requirements should fail
before checking messages. Project configuration remains independent of global fallback settings.

Add customization in bounded, named forms. Do not put commands, imports, message-controlled
waivers, or unrestricted regular expressions into policy. Optional authoring assistance should
preview changes; checking must never rewrite history or alter its input.

## Work sequence

1. **Content requirements:** require bodies for selected types without skipping other checks;
   bound total prose independently of line wrapping. Preserve optional-body and title-only behavior.
2. **Understandable configuration:** explain effective settings and overrides; offer editable
   starter policies for squash-title and full-message workflows.
3. **References and trailers:** design bounded project-specific reference and footer-value
   constraints. Token presence must never be presented as verified identity or completed testing.
4. **Gradual enforcement:** design warning/error policy with an explicit report-version decision
   and unchanged input-error precedence. Do not reinterpret existing failed reports as warnings.
5. **Alternative message formats and authoring:** investigate an explicit ordinary-title mode and
   configuration-derived message templates. Retain a clear conventional parser boundary.

Full release/changelog automation, arbitrary plugin execution, and automatic commit rewriting are
not necessary to make commit checking useful. Keep them outside this first 0.2.0 scope.
