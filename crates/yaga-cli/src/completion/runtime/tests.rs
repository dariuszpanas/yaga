use super::{Protocol, candidates, render, split_words, zsh};
use clap::{Arg, Command, CommandFactory};

#[test]
fn help_folding_preserves_fitting_spaces_and_graphemes() {
    use crate::presentation::completion::wrap_help;
    assert_eq!(
        wrap_help("uv tool installation", 9),
        "uv tool \ninstallat\nion"
    );
    assert_eq!(wrap_help("x abcdef y", 3), "x \nabc\ndef\n y");
    assert_eq!(wrap_help("a 界界界", 3), "a \n界\n界\n界");
    assert_eq!(wrap_help("invisible", 0), "");
}

fn values(args: &[&str], prefix: &str) -> Vec<String> {
    let mut root = crate::Cli::command();
    root.build();
    let args: Vec<_> = args.iter().map(|s| s.to_string()).collect();
    candidates(&root, &args, prefix)
        .into_iter()
        .map(|(s, _)| s)
        .collect()
}

#[test]
fn unfinished_words_follow_posix_shlex_without_expansion() {
    assert_eq!(
        split_words(r#"yaga "" 'a b' "c\d" e\ f 'unfinished"#),
        ["yaga", "", "a b", r"c\d", "e f", "unfinished"]
    );
    assert_eq!(
        split_words("one\ttwo\u{a0}three four\\"),
        ["one", "two\u{a0}three", "four"]
    );
}

#[test]
fn catalog_completes_aliases_choices_and_repeated_options() {
    assert_eq!(
        values(&["branch", "check"], "-"),
        ["--policy", "--name", "--format", "--quiet", "-q", "--help"]
    );
    assert_eq!(
        values(&["branch", "check", "-q"], "-q"),
        Vec::<String>::new()
    );
    assert_eq!(values(&["branch", "check"], "--format=J"), ["json"]);
    assert_eq!(
        values(&["branch", "check", "--format"], "="),
        ["text", "json", "github"]
    );
    assert_eq!(
        values(&["repo", "check", "--check", "workflow"], "--check"),
        ["--check"]
    );
    assert_eq!(
        values(&["commit", "template", "--comment-char"], ""),
        ["#", ";"]
    );
    assert!(values(&["branch", "check", "--policy"], "some file").is_empty());
}

#[test]
fn native_commands_describe_their_visible_arguments() {
    let mut root = Command::new("yaga").subcommand(
        Command::new("extra")
            .arg(
                Arg::new("select")
                    .long("select")
                    .short('s')
                    .value_parser(["one", "two"]),
            )
            .arg(Arg::new("hidden").long("private-option").hide(true))
            .subcommand(Command::new("private-command").hide(true)),
    );
    root.build();
    assert_eq!(candidates(&root, &[], "ex")[0].0, "extra");
    assert_eq!(candidates(&root, &["extra".into()], "-s")[0].0, "-s");
    assert_eq!(
        candidates(&root, &["extra".into(), "--select".into()], "t")[0].0,
        "two"
    );
    assert!(candidates(&root, &["extra".into()], "private").is_empty());
    assert!(candidates(&root, &["extra".into()], "--private").is_empty());
}

#[test]
fn shell_output_keeps_control_and_text_distinct() {
    let options = [("alpha".into(), "literal".into())];
    assert_eq!(render(Protocol::Fish, &options, "is-args"), (0, None));
    assert_eq!(render(Protocol::Fish, &[], "is-args"), (1, None));
    assert_eq!(
        render(Protocol::Fish, &options, "unknown"),
        (0, Some(String::new()))
    );
    assert_eq!(render(Protocol::Zsh, &[], "").1.unwrap(), "_files");
    assert_eq!(
        render(Protocol::PowerShell, &[("json".into(), String::new())], "")
            .1
            .unwrap(),
        "json::: "
    );
    assert_eq!(
        zsh("it's a $word: \"quoted\""),
        r#"it''s a \$word\\: ""quoted"""#
    );
}
