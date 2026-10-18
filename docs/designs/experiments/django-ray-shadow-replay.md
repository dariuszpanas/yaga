# django-ray historical selection replay

The first replay compared 31 merged revisions with the frozen current qualification selector.
It found **zero omitted existing obligations**, but selected the full five-suite catalog for
28 revisions. This is selection evidence only. No tests were executed by the replay, no missed
failure rate was established, and no feedback-time improvement has been measured.

Required CI remains authoritative. Selective execution remains disabled. The adoption threshold
will be chosen after measurement and before activation.

## Recorded result

| Observation | Result |
| --- | --- |
| Merged revisions compared | 31 |
| Revisions omitting an existing selector obligation | 0 |
| Revisions adding obligations | 24 |
| Full five-suite selections | 28 |
| Empty qualification selections | 3 |
| Explicit full-trigger selections | 17 |
| Unknown-path full fallbacks | 11 |
| Median local planning time | 0.825046 seconds |
| Maximum local planning time | 1.028669 seconds |
| Actual qualification runtime observations | 0 |
| Measured feedback-time reduction | Unavailable |

Timing covers Git collection, plan creation, handoff validation, and comparison. It excludes
registry creation, Python/dependency setup, queueing, artifact upload, and qualification execution.
The measurement ran on Windows with Python 3.15 with no local qualifications running. It is
not a CI benchmark or a speedup estimate.

The policy is deliberately conservative: all 17 explicit full triggers and all 11 unknown-path
fallbacks select every suite. For example, dependency PR #638 adds latency qualification to the
baseline's other four suites. Test-only PR #630 selects all suites because its path is unclassified.
Frontend asset PR #356 retains the baseline application qualification but its accompanying
unclassified tests also force the full catalog. Only documentation-only PRs #633, #615, and #597
produce empty qualification selections. Ordinary CI is outside this catalog and still runs.

These results do not justify activation or a savings claim. The next observation should measure
real full same-source runs and distinguish intended full triggers from unclassified paths worth
reviewing. Rules should change only with coverage evidence and regression cases; this replay
does not weaken any fallback.

## Sources and sampling

YAGA implementation: `6cab22564af22773155a081e13fe161c98b29388`.
django-ray adapter snapshot: `a5a17c9e3687687db1362c61ff0567857f12d159`, tree
`3af35b26239c10fa63f83d42845b97b1d3e8d63f`, based on upstream
`34ef6d2f400fbc7aa59994b7cdfc15b31fd4479e`. The consumer snapshot is published in
django-ray PR #640; the YAGA implementation and this evidence accompany YAGA PR #223.

The cohort contains the 30 newest merge timestamps among 60 recently updated closed PRs returned
by GitHub on 2026-10-17. It also includes #356, the latest merged change touching frontend static
assets. Backend, assets, infrastructure, dependencies, tests/configuration, documentation, and
mixed changes are represented; this is a convenience sample, not a randomized performance study.

For each PR, its recorded API base and merged commit were checked locally: the base must be an
ancestor, and the number of commits in `base..merged` must equal the API PR commit count. The
execution tree comes from the exact merged commit. The replay uses these explicit two-dot ranges,
not current branch names, original PR heads, GitHub's current PR-files API, or inferred worktrees.
It measures merged revisions; the live PR adapter uses an explicit three-dot base-to-head range.

Both selectors use the same complete YAGA-collected paths. The baseline selector, policy, and fixed
executor definitions come from the current adapter snapshot for every revision. Historical code
is never imported or executed. This comparison does not reconstruct the gate decisions or outcomes
that actually occurred when those PRs were open.

The [report](django-ray-shadow-replay/report.json) records source identities, policy/adapter/replay
file hashes, timing context, cohort, and canonical SHA-256 identities for these complete observation
files. Each observation retains the versioned plan, its digest, paths, reasons, and comparison:

- [Observations 1–6](django-ray-shadow-replay/observations-01.json)
- [Observations 7–12](django-ray-shadow-replay/observations-02.json)
- [Observations 13–18](django-ray-shadow-replay/observations-03.json)
- [Observations 19–24](django-ray-shadow-replay/observations-04.json)
- [Observations 25–30](django-ray-shadow-replay/observations-05.json)
- [Observation 31](django-ray-shadow-replay/observations-06.json)

## Reproduce the comparison

Use the exact reviewed consumer checkout above, with its complete Git history already present,
and the YAGA implementation installed. The runner does not fetch. The consumer path is trusted
executable input because the runner imports that checkout's adapter and existing selector.
The runner verifies the checkout root, HEAD, tree, and clean tracked/untracked state before import,
after import, and after replay. Ignored local environments and bytecode are allowed. This correlates
the recorded source in a trusted checkout; it does not isolate execution from host modifications.

```bash
uv run python examples/test-planning/replay_django_ray.py \
  --consumer /path/to/django-ray-shadow \
  --cohort docs/designs/experiments/django-ray-shadow-replay/cohort.json \
  --adapter-commit a5a17c9e3687687db1362c61ff0567857f12d159 \
  --adapter-tree 3af35b26239c10fa63f83d42845b97b1d3e8d63f \
  --output /path/to/new-replay.json
```

The command writes a complete report, exits nonzero on an omission, and fails instead of replacing
an unavailable plan with an empty selection. Selection and source identities are reproducible;
timings vary. The retained report partitions observations into six files to remain below the
repository's per-file limit; canonical observation hashes use sorted keys, compact separators,
UTF-8 without ASCII escaping, and no trailing newline. The separate
[cohort](django-ray-shadow-replay/cohort.json) is the exact explicit input list.

Live full-shadow observation still requires publication of the pinned YAGA implementation and
registration of the consumer workflow on its default branch, as required for
[GitHub manual dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
Then manually dispatch the reviewed
source with an explicit base. Keep required checks enabled, retain all five original workflow
artifacts and cleanup evidence, and assess real runtime and omitted failures before activation.
