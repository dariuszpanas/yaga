# Releasing YAGA

The release workflow is `.github/workflows/release.yml`. It validates the selected source,
runs the full local gate on GitHub's Linux runner, and retains the exact wheel and source
distribution inspected by the package build gate. Publishing uses those artifacts without rebuilding.

## One-time PyPI setup

Configure a [PyPI trusted publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
with these values (use a pending publisher when creating the project):

| Field | Value |
| --- | --- |
| PyPI project | `yaga-cli` |
| GitHub owner | `dariuszpanas` |
| Repository | `yaga` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

Create the GitHub `pypi` environment with required maintainer approval and deployment rules
allowing release tags. Protect `v*` tags against unauthorized creation, updates, and deletion.
No PyPI API token or repository secret is needed. Only the upload job receives `id-token: write`;
it checks out no source and runs no build scripts. Keep that separation when editing the workflow.

## Rehearse without publishing

Once the workflow is merged, select **Actions → Release → Run workflow** on the default branch.
The manual route runs validation and uploads the `distributions` artifact to the workflow run.
It never runs the PyPI upload or GitHub Release jobs. It also retains the selected changelog
section as a separate `release-notes` artifact. Manual runs on other branches are skipped.

Download the artifact to inspect the wheel and source distribution. A rehearsal is build evidence,
not proof that PyPI's publisher/environment setup works: OIDC authorization happens on publication.

Inspect the description embedded in the wheel, rendered with `readme_renderer.markdown`, in a
browser. The build gate checks the Markdown content type, absolute HTTPS links, and preserved
public logo markup; it deliberately does not make network requests. Before publication, verify
image and link URLs anonymously, including documentation fragments and the package's project
URLs. The logo must load from the public documentation site. Repository and example links need
the source repository to be public; authenticated maintainer access does not prove they work for
package users. Twine's metadata check does not prove remote images or links are reachable.

## Publish a version

1. Update `pyproject.toml`, `VERSION` in `src/yaga/version_requirement.py`, and user-facing version
   references. Tests require the Action runtime version to match package metadata. Convert the candidate changelog
   heading into `## [0.1.0] - YYYY-MM-DD`, using the actual release date, and retain `Unreleased`
   above it. The Beta classifier is a maturity label; version `0.1.0` is a normal PyPI version,
   not the PEP 440 prerelease `0.1.0b1`.
2. Merge the release changes and require all hosted CI lanes to pass for the exact commit,
   including Windows. Resolve dependency PR failures before choosing the candidate.
3. After publication is explicitly authorized, create and push the matching `v0.1.0` tag at that
   verified commit. Use the corresponding version for later releases.
4. The workflow requires the tagged commit to be on the default branch, checks that the tag matches
   package metadata and a dated changelog entry, and reruns `uv run make ci`. Only then does it
   offer the `pypi` environment approval and upload the validated distributions with attestations.
5. Verify the PyPI project files and install `yaga-cli==0.1.0` from PyPI in a clean tool environment.
   Confirm `yaga --help` and the automatically created GitHub Release. Its wheel and source
   archive must be the same artifacts published to PyPI.

This workflow accepts final `vMAJOR.MINOR.PATCH` tags and does not publish TestPyPI packages.
After PyPI publication succeeds, a separate job creates the GitHub Release from the validated
changelog section and original distributions. Only that job has `contents: write`; it checks out
no source and has no OIDC permission. The PyPI job retains its isolated `id-token: write` permission.
Read the Docs configuration is independent of package upload.

If only the GitHub Release job fails, repair or rerun that job without republishing to PyPI.
If a release entry already exists after a partial failure, inspect its draft state and assets and
complete it using the retained artifacts; the workflow deliberately refuses to overwrite an entry.

If validation fails, fix the source and choose a new verified candidate before publishing. If an
upload is interrupted, inspect PyPI before rerunning: package versions/files cannot be overwritten,
and the workflow deliberately does not suppress existing-file errors.
