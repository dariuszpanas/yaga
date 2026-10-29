# Minimum runtime qualification

The frozen CI matrix and release build gate test the locked dependency versions. To also
check the lower versions allowed by the published **base** wheel, run this separate recipe
from a development checkout with uv, Git, and Python 3.12 available:

```bash
uv run --locked --python 3.12 python scripts/check_minimum_runtime.py --report minimum-runtime.json
```

Choose a new report path for each run; the helper refuses to overwrite existing evidence.
It builds the current working-tree sources using the installed locked backend, creates a
temporary environment outside the checkout, and installs the fresh wheel with
`uv pip install --resolution lowest --no-build`. That resolution includes transitive
dependencies, unlike `lowest-direct`. It uses published wheel metadata, omits every extra,
and ignores uv/pip configuration and environment overrides. Installation may access PyPI
or the local uv cache; the recipe is not an offline dependency test.

The helper runs `uv pip check`, verifies that YAGA, Typer, Rich, and PyYAML import from
the new environment, and executes the same installed smoke cases as the strict build gate:

- Commit/config and branch checks, current-file workflow policies, and repository reports.
- Git-bound change, tree, path, mode, and size checks in a disposable repository.
- Local/event descriptions, mocked current-PR retrieval, and description findings/errors.
- Test policy, planning, handoff, diagnostics, and result evaluation.
- Installed command help, including the Docker-backed lint entrypoint.

The smoke exercises reporting and exit contracts; it does not invoke Docker, authenticated
GitHub requests, optional quality models, or Bedrock. Python runs with checkout/user-site
leakage disabled. The JSON receipt records the fresh wheel's SHA-256, uv and Python versions,
platform, actual installed distribution versions, inspected import locations, and passing
checks. Temporary wheel/environment files are removed afterward; their paths in the receipt
describe the completed run. This is qualification evidence, not a retained release artifact.

Use the Python 3.12 recipe for the supported lowest-runtime baseline. `--no-build` deliberately
requires available dependency wheels; another interpreter/platform may need a separate source-build
qualification. A successful run does not establish every Python/platform combination, optional
dependency minimum, model inference, or development-tool compatibility. Lowest resolution depends
on currently available distributions, so retain the receipt instead of assuming a historical list
of versions. Do not raise dependency floors just to match the development lock.

This helper does not replace `uv run make ci`, the frozen platform matrix, or the strict build
gate's hash-locked installation. It adds no hosted workload; a small hosted smoke can be considered
when the Actions budget permits.

## Security audit of the resolved dependencies

Functional smoke success does not mean a dependency set is free of known advisories. Before
publication, audit the exact installed versions recorded in the minimum-runtime receipt, in
addition to auditing the release lock. Keep the audit output and the receipt together.

Generate a requirements file from the receipt's `runtime.distributions` mapping, excluding
`yaga-cli` itself, with one `name==version` per line. Audit those pins without installing them:

```bash
uv tool run --from pip-audit==2.10.1 pip-audit --no-deps --disable-pip --strict \
  --requirement minimum-runtime-requirements.txt --format json --output minimum-runtime-audit.json
```

Require a successful audit with no skipped distributions or known advisories; inspect the JSON
rather than treating an unavailable advisory service as a clean result. This command contacts the
advisory service. It does not execute the recorded dependency versions or replace the installed
smoke checks. Repeat the audit against the actual pins from fresh public-package installations,
including lowest resolution, after publication. Retain the audit time because advisory data changes.

YAGA 0.4.1 adds a base `Pygments>=2.20.0,<3` constraint to exclude the versions affected by
[CVE-2022-40896](https://github.com/advisories/GHSA-mrwq-x4v8-fh7p) and
[CVE-2026-4539](https://github.com/advisories/GHSA-5239-wwwm-4pmq). This is a security-driven floor
for the existing Typer/Rich dependency, not an attempt to match every development-lock version.
YAGA does not call the affected Smithy or ADL lexers; that reachability assessment does not waive
the dependency audit.
