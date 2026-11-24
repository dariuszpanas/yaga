//! PyYAML SafeLoader's YAML 1.1 implicit scalar tags. Classification never constructs objects.
use libyaml_safer::ScalarStyle;
use regex::Regex;
use std::sync::LazyLock;

static RESOLVERS: LazyLock<Vec<(&str, Regex)>> = LazyLock::new(|| {
    [
        (
            "bool",
            r#"(?x)^(?:yes|Yes|YES|no|No|NO
                    |true|True|TRUE|false|False|FALSE
                    |on|On|ON|off|Off|OFF)$"#,
        ),
        (
            "null",
            r#"(?x)^(?: ~
                    |null|Null|NULL
                    | )$"#,
        ),
        (
            "float",
            r#"(?x)^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
                    |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
                    |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
                    |[-+]?\.(?:inf|Inf|INF)
                    |\.(?:nan|NaN|NAN))$"#,
        ),
        (
            "int",
            r#"(?x)^(?:[-+]?0b[0-1_]+
                    |[-+]?0[0-7_]+
                    |[-+]?(?:0|[1-9][0-9_]*)
                    |[-+]?0x[0-9a-fA-F_]+
                    |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$"#,
        ),
        (
            "timestamp",
            r#"(?x)^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]
                    |[0-9][0-9][0-9][0-9] -[0-9][0-9]? -[0-9][0-9]?
                     (?:[Tt]|[\x20\t]+)[0-9][0-9]?
                     :[0-9][0-9] :[0-9][0-9] (?:\.[0-9]*)?
                     (?:[\x20\t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$"#,
        ),
        ("merge", r#"(?x)^(?:<<)$"#),
        ("value", r#"(?x)^(?:=)$"#),
        ("yaml", r#"(?x)^(?:!|&|\*)$"#),
    ]
    .into_iter()
    .map(|(tag, pattern)| (tag, Regex::new(pattern).expect("fixed YAML scalar rule")))
    .collect()
});

pub(super) fn resolve(value: &str, style: ScalarStyle) -> String {
    let tag = if style == ScalarStyle::Plain {
        RESOLVERS
            .iter()
            .find(|(_, regex)| regex.is_match(value))
            .map(|(tag, _)| *tag)
            .unwrap_or("str")
    } else {
        "str"
    };
    format!("tag:yaml.org,2002:{tag}")
}
