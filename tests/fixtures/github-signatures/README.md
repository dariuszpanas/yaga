# GitHub signature fixtures

These public fixtures exercise verification against real GitHub signatures. They contain no
private keys. The `.commit` files contain exact raw Git commit object payloads, without the Git
object type/length prefix. Preserve their bytes: changing line endings invalidates both their
object IDs and their signatures.

Collected on 2026-10-17 from the local Git object database for
[`dariuszpanas/yaga`](https://github.com/dariuszpanas/yaga), using binary subprocess output from
`git --no-pager --no-lazy-fetch cat-file commit <full-SHA>`. Each object ID was independently
recomputed as SHA-1 of `b"commit " + decimal_payload_length + b"\0" + payload`.

| Fixture | Original Git commit | SHA-256 of fixture bytes |
| --- | --- | --- |
| `dependabot-urllib3.commit` | [`2dea34660d96cdc91ae89eb11b445f816fbf3cea`](https://github.com/dariuszpanas/yaga/commit/2dea34660d96cdc91ae89eb11b445f816fbf3cea) | `7a77747a7ab97eb086c03645a59bc0b1857f69bb63998820d2b27db89126a0bc` |
| `dependabot-setup-uv.commit` | [`6680afa1ca995c956871005c280d24e06007dd44`](https://github.com/dariuszpanas/yaga/commit/6680afa1ca995c956871005c280d24e06007dd44) | `dccd78d9b194d39334e1170d835cc61d66f8acf52e487a89f62e881ba357136a` |
| `dependabot-urllib3-parent.commit` | [`646a0a1d28b404fabb79776612be4721c11052ff`](https://github.com/dariuszpanas/yaga/commit/646a0a1d28b404fabb79776612be4721c11052ff) | `fb235975aaefb46fcd432e1720eb5a4a9dcd48306866cff5f951903656a3421d` |
| `web-flow.asc` | [GitHub's public signing keys](https://github.com/web-flow.gpg), downloaded over HTTPS | `6e8af687f60cf3f403151c8fb1b26e95e6f9e424ca60cc8f3787bd4466a3ef84` |

The original commits have author `dependabot[bot]
<49699333+dependabot[bot]@users.noreply.github.com>` and committer `GitHub
<noreply@github.com>`. Their messages also contain a Signed-off-by footer; that footer is not
authentication evidence. GitHub's signing key is shared across GitHub-created commits and does
not, by itself, prove Dependabot authorship.

Both signatures were independently verified with Git for Windows' `gpgv.exe` against a temporary
keyring dearmored from `web-flow.asc`. Verification reported `GOODSIG B5690EEEBB952194` and
`VALIDSIG 968479A1AFF927E37D1A566BB5690EEEBB952194` for each original payload. The complete
fingerprint, rather than only the short key ID or displayed user ID, identifies the intended key.

To reproduce the independent verification, extract the `gpgsig` header and its continuation lines
from a raw commit, remove one continuation-space prefix from each signature line, and write that
ASCII-armored signature separately. The signed payload is the exact original commit bytes with
only that complete header removed. In an isolated temporary directory, run:

```text
gpg --batch --no-options --homedir <temporary-directory> --dearmor --output <temporary-directory>/keys.gpg web-flow.asc
gpgv --homedir <temporary-directory> --keyring keys.gpg --status-fd 1 signature.asc payload
```

The fixtures retain the original tree and parent IDs. Raw signature verification does not need
their referenced trees or parents. The parent fixture supports an exact parent-to-head range in
end-to-end commit-message tests; its excluded ancestors and tree contents are not inspected.
Tests must not fetch objects. Any mutation used for rejection tests should be derived in memory;
do not overwrite these originals.
