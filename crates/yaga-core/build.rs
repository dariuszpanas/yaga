use std::fmt::Write;
use std::path::PathBuf;
use std::{env, fs};

fn main() {
    const CATALOG: &str = "src/policies/catalog.tsv";
    println!("cargo:rerun-if-changed={CATALOG}");
    let input = fs::read_to_string(CATALOG).expect("policy catalog must be UTF-8");
    let mut lines = input.lines();
    assert_eq!(
        lines.next(),
        Some("kind\ttitle\tdefault_path\tversion_key\tcurrent\tdescription")
    );
    let mut entries = String::from("kinds! {\n");
    for line in lines {
        let fields: Vec<_> = line.split('\t').collect();
        let [
            name,
            _title,
            default_path,
            version_key,
            current,
            description,
        ] = fields.as_slice()
        else {
            panic!("policy catalog rows require six fields");
        };
        let mut variant = String::new();
        for word in name.split('-') {
            assert!(!word.is_empty() && word.bytes().all(|ch| ch.is_ascii_lowercase()));
            variant.push_str(&word[..1].to_ascii_uppercase());
            variant.push_str(&word[1..]);
        }
        let current: u32 = current.parse().expect("integer policy version");
        writeln!(
            entries,
            "{variant}, {name:?}, {default_path:?}, {version_key:?}, {current}, {description:?};"
        )
        .unwrap();
    }
    entries.push_str("}\n");
    let output = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo build output directory"));
    fs::write(output.join("policy_catalog.rs"), entries).expect("write generated policy metadata");
}
