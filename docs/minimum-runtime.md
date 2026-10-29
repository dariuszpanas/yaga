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
