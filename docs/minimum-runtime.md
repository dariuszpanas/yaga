# Minimum runtime qualification

The frozen CI matrix and release build gate test the locked dependency versions. A separate
required CI matrix checks and audits the lower versions allowed by the published **base** wheel
on Linux and Windows with Python 3.12. Run the same check from a development checkout with
locked development dependencies, uv, Git, and Python 3.12 available:

```bash
uv run --locked --python 3.12 python scripts/check_minimum_runtime.py --report minimum-runtime.json
```

Choose a new report path for each run; the helper refuses to overwrite existing evidence.
It builds the current working-tree sources using the installed locked backend, creates a
temporary environment outside the checkout, and installs the fresh wheel with
`uv pip install --resolution lowest --no-build` without a shared cache. That resolution includes transitive
dependencies, unlike `lowest-direct`. It uses published wheel metadata, omits every extra,
and ignores uv/pip configuration and environment overrides. Installation and the mandatory
advisory audit access public services; the recipe is not an offline dependency test.

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
checks. It also retains the exact audited pins, audit time, auditor version, complete audit
response, and result. Failed checks return nonzero and retain failure evidence when available.
Temporary wheel/environment files are removed afterward; their paths in the receipt describe
the completed run. This is qualification evidence, not a retained release artifact.

To qualify an already-built wheel without rebuilding it, pass its exact path:

```bash
uv run --locked --python 3.12 python scripts/check_minimum_runtime.py \
  --wheel dist/yaga_cli-0.4.1-py3-none-any.whl --report minimum-runtime.json
```

Use the filename of the version being qualified. The receipt binds the selected wheel's SHA-256
and version to the installed inventory and audit.

Use the Python 3.12 recipe for the supported lowest-runtime baseline. `--no-build` deliberately
requires available dependency wheels; another interpreter/platform may need a separate source-build
qualification. A successful run does not establish every Python/platform combination, optional
dependency minimum, model inference, or development-tool compatibility. Lowest resolution depends
on currently available distributions, so retain the receipt instead of assuming a historical list
of versions. Do not raise dependency floors just to match the development lock.

This helper does not replace `uv run make ci`, the frozen platform matrix, or the strict build
gate's hash-locked installation. `CI Prerequisites` requires both minimum-runtime jobs to succeed;
failure, cancellation, or a skipped job cannot pass the aggregate. Windows is included because
its dependency closure includes packages such as Colorama that Linux does not install.

The release workflow builds once, then both platform jobs download and qualify the retained
wheel using `--wheel`. The PyPI publisher requires the build and the complete minimum-runtime
matrix to succeed. Manual release rehearsals run the same audit without publishing. Audit
receipts are separate, platform-specific Actions artifacts retained for seven days; they are
stored outside the source checkout and never added to the distributions.

## Security audit of the resolved dependencies

Functional smoke success does not mean a dependency set is free of known advisories. Every helper
run therefore audits the exact installed versions, excluding only the verified YAGA distribution,
using the development-locked pip-audit tool in a separate process. It uses exact requirements with
`--no-deps --disable-pip --strict`, queries the PyPI advisory service with a private cache, and
validates the returned inventory against every expected pin.

Known advisories, skipped distributions, missing or unexpected pins, duplicate entries, malformed
responses, unavailable advisory services, and process errors all fail the gate. There are no
advisory ignores or audit opt-outs. Audit-tool dependencies are separate from the target runtime.
The audit does not execute the recorded dependency versions or replace installed smoke checks.
Audit the release lock separately, and repeat the audit against actual pins from fresh public
package installations, including lowest resolution, after publication. Retain the audit time
because advisory data changes. A passing audit means no known advisories were reported at that time.

YAGA 0.4.1 adds a base `Pygments>=2.20.0,<3` constraint to exclude the versions affected by
[CVE-2022-40896](https://github.com/advisories/GHSA-mrwq-x4v8-fh7p) and
[CVE-2026-4539](https://github.com/advisories/GHSA-5239-wwwm-4pmq). This is a security-driven floor
for the existing Typer/Rich dependency, not an attempt to match every development-lock version.
YAGA does not call the affected Smithy or ADL lexers; that reachability assessment does not waive
the dependency audit.
