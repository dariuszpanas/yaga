use serde_json::Value;
use std::{fs, path::Path, process::Command};

fn error(cwd: &Path, args: &[&str]) -> Value {
    let result = Command::new(env!("CARGO_BIN_EXE_yaga"))
        .current_dir(cwd)
        .args(args)
        .args(["--format", "json"])
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(2));
    assert!(result.stdout.is_empty());
    serde_json::from_slice(&result.stderr).unwrap()
}

#[test]
fn commit_policy_check_and_config_show_report_the_input_key_and_version() {
    let dir = tempfile::tempdir().unwrap();
    for (source, template) in [
        (
            "config-version=2\n[commit]",
            "config-version must be an integer from 1 through 1 in {path} (found newer schema version 2; update YAGA using its installation manager)",
        ),
        (
            "config-version=1\n[commit]\nnew-option=1",
            "unknown commit configuration key(s) in {path}: new-option",
        ),
    ] {
        fs::write(dir.path().join(".yaga.toml"), source).unwrap();
        let check = error(dir.path(), &["policy", "check", "--strict", ".yaga.toml"]);
        let item = &check["policies"][0];
        let path = item["path"].as_str().unwrap();
        let expected = template.replace("{path}", path);
        assert_eq!(item["status"], "invalid");
        assert_eq!(item["message"], expected);
        let show = error(dir.path(), &["config", "show", "--config", ".yaga.toml"]);
        assert_eq!(show["error"]["kind"], "configuration");
        assert_eq!(show["error"]["message"], expected);
    }
}
