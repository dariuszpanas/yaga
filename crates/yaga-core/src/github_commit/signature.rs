//! Offline verification of exact Git objects with the package's pinned public key.
//! Authentication of signed bytes never authorizes an author by itself.
use super::event::valid_oid;
use crate::git::{Repository, process::run_bounded};
use sha1::Sha1;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};

pub const FINGERPRINT: &str = "968479A1AFF927E37D1A566BB5690EEEBB952194";
const MAX_OBJECT: usize = 256 * 1024;
const MAX_SIGNATURE: usize = 16 * 1024;
const MAX_STATUS: usize = 16 * 1024;
// Audited GitHub web-flow public keys retrieved on 2026-10-17. A future key
// rotation requires a package update. No key is imported or fetched at runtime.
const KEYRING: &[u8] = include_bytes!("github-keyring.gpg");

pub fn matches_object(raw: &[u8], sha: &str) -> bool {
    if raw.is_empty() || raw.len() > MAX_OBJECT {
        return false;
    }
    let prefix = format!("commit {}\0", raw.len());
    let actual = if sha.len() == 40 {
        format!(
            "{:x}",
            Sha1::new()
                .chain_update(prefix)
                .chain_update(raw)
                .finalize()
        )
    } else {
        format!(
            "{:x}",
            Sha256::new()
                .chain_update(prefix)
                .chain_update(raw)
                .finalize()
        )
    };
    actual == sha
}

pub fn split_signature(raw: &[u8]) -> Option<(Vec<u8>, Vec<u8>)> {
    if raw.is_empty() || raw.len() > MAX_OBJECT || raw.contains(&0) {
        return None;
    }
    let boundary = raw.windows(2).position(|s| s == b"\n\n")?;
    let headers = &raw[..boundary];
    if headers.contains(&b'\r') {
        return None;
    }
    let mut retained = Vec::new();
    let mut signature: Option<Vec<&[u8]>> = None;
    let mut inside = false;
    for line in headers.split(|&b| b == b'\n') {
        if let Some(continued) = line.strip_prefix(b" ") {
            if inside {
                signature.as_mut()?.push(continued)
            } else if retained.is_empty() {
                return None;
            } else {
                retained.push(line)
            }
        } else {
            let space = line.iter().position(|&b| b == b' ')?;
            let key = &line[..space];
            if key.is_empty() {
                return None;
            }
            inside = key.starts_with(b"gpgsig");
            if inside {
                if key != b"gpgsig" || signature.is_some() {
                    return None;
                }
                signature = Some(vec![&line[space + 1..]]);
            } else {
                retained.push(line)
            }
        }
    }
    let mut detached = signature?.join(&b'\n');
    detached.push(b'\n');
    let end = detached.iter().rposition(|&c| c != b'\n')? + 1;
    if detached.len() > MAX_SIGNATURE
        || !detached.starts_with(b"-----BEGIN PGP SIGNATURE-----\n")
        || !detached[..end].ends_with(b"-----END PGP SIGNATURE-----")
        || [b"-----BEGIN ".as_slice(), b"-----END "]
            .iter()
            .any(|marker| {
                detached
                    .windows(marker.len())
                    .filter(|w| *w == *marker)
                    .count()
                    != 1
            })
    {
        return None;
    }
    let mut payload = retained.join(&b'\n');
    payload.extend_from_slice(&raw[boundary..]);
    Some((payload, detached))
}

pub fn valid_status(raw: &[u8]) -> bool {
    if raw.len() > MAX_STATUS || !raw.ends_with(b"\n") {
        return false;
    }
    // Fingerprints and record names are ASCII. Human signer names can contain arbitrary bytes.
    let decoded = String::from_utf8_lossy(raw);
    let data = decoded.replace("\r\n", "\n").replace('\r', "\n");
    let (mut newsig, mut good, mut valid) = (0, 0, 0);
    for line in data.lines() {
        let Some(status) = line.strip_prefix("[GNUPG:] ") else {
            return false;
        };
        let (kind, args) = status.split_once(' ').unwrap_or((status, ""));
        match kind {
            "NEWSIG" => newsig += 1,
            "GOODSIG" => {
                good += 1;
                let Some((signer, _)) = args.split_once(' ') else {
                    return false;
                };
                if signer != FINGERPRINT && signer != &FINGERPRINT[24..] {
                    return false;
                }
            }
            "VALIDSIG" => {
                valid += 1;
                let fields: Vec<_> = args.split(' ').collect();
                if fields.len() != 10 {
                    return false;
                }
                let d = fields[1].as_bytes();
                if fields[0] != FINGERPRINT
                    || fields[9] != FINGERPRINT
                    || d.len() != 10
                    || !d.iter().enumerate().all(|(i, c)| {
                        if i == 4 || i == 7 {
                            *c == b'-'
                        } else {
                            c.is_ascii_digit()
                        }
                    })
                    || !(1..=12).contains(&fields[2].len())
                    || !fields[2].bytes().all(|c| c.is_ascii_digit())
                    || fields[3..7] != ["0", "4", "0", "1"]
                    || !matches!(fields[7], "8" | "9" | "10" | "11")
                    || fields[8] != "00"
                {
                    return false;
                }
            }
            "KEY_CONSIDERED" | "SIG_ID" => (),
            _ => return false,
        }
    }
    (newsig, good, valid) == (1, 1, 1)
}

fn verify(exe: &Path, dir: &Path, payload: Vec<u8>, signature: &[u8], timeout: Duration) -> bool {
    if std::fs::write(dir.join("signature.asc"), signature).is_err() {
        return false;
    }
    let mut command = Command::new(exe);
    command.current_dir(dir).env_clear().env("LC_ALL", "C");
    for name in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command.args([
        "--homedir",
        ".",
        "--keyring",
        "github-keyring.gpg",
        "--status-fd",
        "1",
        "--weak-digest",
        "SHA1",
        "--weak-digest",
        "RIPEMD160",
        "--",
        "signature.asc",
        "-",
    ]);
    run_bounded(command, Some(payload), MAX_STATUS, MAX_STATUS, timeout)
        .is_ok_and(|o| o.status.success() && valid_status(&o.stdout))
}

pub(super) fn verified(repo: &Repository, candidates: Vec<String>) -> HashSet<String> {
    let mut accepted = HashSet::new();
    let deadline = Instant::now() + Duration::from_secs(30);
    let remaining = || {
        deadline
            .saturating_duration_since(Instant::now())
            .min(Duration::from_secs(5))
    };
    let Ok(exe) = repo.find_external_executable("gpgv") else {
        return accepted;
    };
    let Ok(dir) = tempfile::Builder::new()
        .prefix("yaga-github-signature-")
        .tempdir()
    else {
        return accepted;
    };
    if std::fs::write(dir.path().join("github-keyring.gpg"), KEYRING).is_err() {
        return accepted;
    }
    let mut budget = 16 * 1024 * 1024;
    for sha in candidates.into_iter().take(256) {
        let timeout = remaining();
        if timeout.is_zero() || budget == 0 {
            break;
        }
        if !valid_oid(&sha) {
            continue;
        }
        let bound = budget.min(MAX_OBJECT);
        let Ok(output) = repo.run_with_timeout(&["cat-file", "commit", &sha], bound, timeout)
        else {
            budget -= bound;
            continue;
        };
        budget -= output.stdout.len();
        if !output.status.success() || !matches_object(&output.stdout, &sha) {
            continue;
        }
        if let Some((payload, sig)) = split_signature(&output.stdout) {
            let timeout = remaining();
            if timeout.is_zero() {
                break;
            }
            if verify(&exe, dir.path(), payload, &sig, timeout) {
                accepted.insert(sha);
            }
        }
    }
    accepted
}

#[cfg(test)]
mod tests {
    use super::*;
    const RAW: &[u8] =
        include_bytes!("../../tests/fixtures/github-signatures/dependabot-urllib3.commit");
    const ID: &str = "2dea34660d96cdc91ae89eb11b445f816fbf3cea";
    #[test]
    fn exact_commit_object_and_detached_bytes() {
        assert!(matches_object(RAW, ID));
        let (payload, signature) = split_signature(RAW).unwrap();
        assert!(!payload.windows(7).any(|w| w == b"gpgsig "));
        assert!(signature.starts_with(b"-----BEGIN PGP SIGNATURE-----\n"));
        assert!(!matches_object(&payload, ID));
        assert!(split_signature(&payload).is_none());
        let mut duplicate = RAW.to_vec();
        duplicate.splice(0..0, b"gpgsig invalid\n".iter().copied());
        assert!(split_signature(&duplicate).is_none());
    }
    #[test]
    fn status_requires_one_pinned_strong_signature() {
        let data = format!(
            "[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG B5690EEEBB952194 signer\n[GNUPG:] VALIDSIG {FINGERPRINT} 2026-10-11 1791682550 0 4 0 1 8 00 {FINGERPRINT}\n"
        );
        assert!(valid_status(data.as_bytes()));
        for invalid in [
            data.replace(" 8 00", " 2 00"),
            data.replace("NEWSIG", "ERRSIG"),
            data.replace(" signer", " signer\r[GNUPG:] BADSIG"),
            data.repeat(2),
            data.replace(FINGERPRINT, "1234"),
        ] {
            assert!(!valid_status(invalid.as_bytes()))
        }
    }
    #[test]
    #[ignore = "requires the real gpgv on PATH"]
    fn real_pinned_key_verifies_exact_payload_only() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::open(dir.path()).unwrap();
        let exe = repo.find_external_executable("gpgv").unwrap();
        std::fs::write(dir.path().join("github-keyring.gpg"), KEYRING).unwrap();
        let (mut payload, signature) = split_signature(RAW).unwrap();
        assert!(verify(
            &exe,
            dir.path(),
            payload.clone(),
            &signature,
            Duration::from_secs(5)
        ));
        payload.push(b'\n');
        assert!(!verify(
            &exe,
            dir.path(),
            payload,
            &signature,
            Duration::from_secs(5)
        ));
    }
}
