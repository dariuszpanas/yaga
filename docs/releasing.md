# Releasing YAGA

The release workflow is `.github/workflows/release.yml`. It validates the selected source,
runs the full local gate on GitHub's Linux runner, packages and verifies the Rust workspace
crates for crates.io (`cargo package --workspace --locked`), builds and tests all six native
executables and zero-dependency platform Python wheels, and retains the verified distributions
for tag-triggered publishing.

## Standalone release assets

The release workflow builds one executable per architecture (x86-64 and ARM64) for Linux musl,
macOS and Windows MSVC. Each build runs the actual executable on a matching runner, checks the
architecture and version, and retains its original bytes. Linux images must have no ELF interpreter
or dynamic library dependency. Windows releases use Rust 1.100 to resume a suspended child
through its primary thread handle after assigning the child to its job object. Other targets
use Rust 1.99. The assembler checks all six files and writes
`yaga-native-sha256.txt`. The `native-release` rehearsal artifact is also the exact bundle attached
as individual GitHub Release assets after the tag is published.

The manifest is ASCII with LF endings. Its header is `yaga-native-v1 X.Y.Z`. Each following
line contains a lowercase SHA-256 digest, two spaces, and the exact filename
`yaga-<Rust target>` (`.exe` on Windows), sorted by filename. A mirror must preserve filenames
and bytes. To verify an unpacked bundle independently, retain the manifest outside the bundle
directory, run `python scripts/native_release.py manifest DIRECTORY` and compare both manifests.
Tools that need stronger provenance can pin the verified manifest digest in their own trusted
source. Release-host checksums alone trust the release publisher.

## Python wheel release gate

The same six runner architectures package each verified standalone `yaga` executable into a
zero-dependency `py3-none-<platform>` wheel (just like `uv`):

- Linux (`x86_64` and `aarch64`): wraps the verified static musl binary under `manylinux_2_28_*`.
- macOS (`x86_64` and `arm64`): wraps the thin Mach-O binary targeting macOS 11.0+.
- Windows (`amd64` and `arm64`): wraps the native MSVC PE executable.

Each platform installs its finished wheel with `--no-deps` into an isolated environment, checks
that `yaga.find_yaga_bin()` resolves inside the environment, and runs the console script and
`python -m yaga` from outside the checkout. The assembler checks all six platform wheels and the
source archive (verifying executable architecture, binary role, and macOS deployment target) before
handing them to the tag-only publishing jobs. See [minimum runtime qualification](minimum-runtime.md)
for the zero-dependency wheel verification contract.

## Crates.io release gate

The `build` job runs `cargo package --workspace --locked` to assemble and compile both
`yaga-core` and `yaga-cli` from their unpacked `.crate` archives (including each crate's README,
`LICENSE`, and bundled installer assets). On tag pushes, the isolated `publish-crates` job runs
`cargo publish --workspace --locked`, publishing `yaga-core` first, waiting for the crates.io
index, and then publishing `yaga-cli`.

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

## One-time crates.io setup

Create a GitHub `crates-io` environment with required maintainer approval and deployment rules
allowing `v*` release tags.

- **First-ever publish of `yaga-core` and `yaga-cli`**: crates.io requires crates to exist before
  OIDC Trusted Publishing can be attached to them. Add a short-lived `CARGO_REGISTRY_TOKEN`
  secret (scoped to `publish-new` / `publish-update`) to the `crates-io` GitHub environment for
  the initial release tag. When `secrets.CARGO_REGISTRY_TOKEN` is set, `publish-crates` skips
  OIDC token exchange and publishes directly with that token.
- **Subsequent releases (OIDC Trusted Publishing)**: once `yaga-core` and `yaga-cli` exist on
  crates.io, delete the `CARGO_REGISTRY_TOKEN` secret from the `crates-io` environment and
  configure [crates.io Trusted Publishing](https://crates.io/docs/trusted-publishing) on **both**
  `yaga-core` and `yaga-cli` with these values:

| Field | Value |
| --- | --- |
| Repository owner | `dariuszpanas` |
| Repository name | `yaga` |
| Workflow filename | `release.yml` |
| Environment | `crates-io` |

With no `CARGO_REGISTRY_TOKEN` secret present, `publish-crates` authenticates automatically using
the pinned `rust-lang/crates-io-auth-action` OIDC exchange (which revokes its temporary token
when the job exits).

## Rehearse without publishing

Once the workflow is merged, select **Actions → Release → Run workflow** on the default branch.
The manual route runs validation, `cargo package --workspace --locked`, and all six native target
builds, uploading the assembled `python-release`, `native-release`, and `release-notes` artifacts
to the workflow run. It never runs PyPI, crates.io, or GitHub Release publishing jobs. Manual runs
on other branches are skipped.

Download the artifacts to inspect the wheels, source distribution, standalone binaries, and
`yaga-native-sha256.txt` manifest. A rehearsal is build evidence, not proof that PyPI or
crates.io OIDC authorization works: trusted-publisher exchange happens on tag publication.

Inspect the description embedded in the wheel, rendered with `readme_renderer.markdown`, in a
browser. The build gate checks the Markdown content type, absolute HTTPS links, and preserved
public logo markup; it deliberately does not make network requests. Before publication, verify
image and link URLs anonymously, including documentation fragments and the package's project
URLs. The logo must load from the public documentation site. Repository and example links need
the source repository to be public; authenticated maintainer access does not prove they work for
package users.

## Publish a version

1. Update the version across `Cargo.toml` (`[workspace.package].version` and
   `[workspace.dependencies].yaga-core`), `Cargo.lock`, `pyproject.toml`, `uv.lock`, and the
   version literals in `action.yml`. `scripts/check_release.py` rejects mismatches across package
   metadata, Cargo workspace/dependency versions, `action.yml`, and the dated changelog heading.
   Convert the candidate changelog heading into `## [X.Y.Z] - YYYY-MM-DD`, using the actual
   release date, and retain `## [Unreleased]` above it. The Beta classifier is a maturity label;
   use a normal semantic version such as `0.6.0` (not `0.6.0b1`) and `YAGA 0.6.0` as the GitHub
   Release title.
2. Merge the release changes to `main` and require all hosted CI lanes to pass for the exact
   commit (including `YAGA Checks`, `Lint, Package Build & Docs`, Ubuntu/Windows native Rust
   qualification with `cargo package --workspace --locked`, macOS ARM64 wheel verification, and
   the Python 3.12–3.15 wheel matrix).
3. Optionally run a manual **Release** rehearsal on `main` and inspect `python-release`,
   `native-release`, and `release-notes`.
4. After publication is explicitly authorized, create and push the matching `vX.Y.Z` tag at that
   verified default-branch commit.
5. The workflow verifies the tagged commit is an ancestor of `main`, checks tag/version/changelog
   alignment, runs `uv run --frozen make ci BUILD_ARGS="--output-dir dist"` and
   `cargo package --workspace --locked`, builds and verifies all six native binaries and wheels,
   and then pauses at the `pypi` and `crates-io` environment approvals before publishing to PyPI
   and crates.io. Only after both package registries succeed does `github-release` create the
   GitHub Release with the retained wheels, source archive, standalone executables, and
   `yaga-native-sha256.txt` manifest.
6. Verify the published artifacts across all channels:
   - PyPI: install `yaga-cli==X.Y.Z` in a clean `uv tool` environment and run `yaga --version`.
   - crates.io: verify `yaga-core` and `yaga-cli` pages and `cargo install --locked yaga-cli --version X.Y.Z`.
   - GitHub Release: confirm all six standalone executables, `yaga-native-sha256.txt`, wheels,
     and source archive are attached. To publish or update the **YAGA Checks** GitHub Marketplace
     Action listing, edit that GitHub Release in the web UI and confirm **Publish this Action to
     the GitHub Marketplace**.
   - Pinned native Action (optional): run **Stage pinned native Action** with the successful
     Release run ID, reviewed source SHA, and `yaga-native-sha256.txt` digest as documented in
     [GitHub Actions native distribution](github-actions.md#native-distribution).

This workflow accepts final `vMAJOR.MINOR.PATCH` tags and does not publish TestPyPI packages.
After PyPI and crates.io publication succeed, a separate job creates the GitHub Release from the
validated changelog section and original distributions. Only `github-release` has
`contents: write`; it checks out no source and has no OIDC permission. `publish` and
`publish-crates` retain their isolated `id-token: write` permissions in their separate GitHub
environments.

If only `github-release` fails, repair or rerun that job without republishing to PyPI or
crates.io. If a release entry already exists after a partial failure, inspect its draft state and
assets and complete it using the retained artifacts; the workflow deliberately refuses to
overwrite an entry.

If validation fails, fix the source and choose a new verified candidate before publishing. If an
upload is interrupted, inspect PyPI and crates.io before rerunning: published versions cannot be
overwritten, and the workflow deliberately does not suppress existing-version errors.
