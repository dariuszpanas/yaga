use clap::Command;
use std::ffi::OsString;

/// Click ends option parsing independently for each group. Clap treats a
/// group terminator as the beginning of positional arguments instead.
pub(super) fn prepare(root: &Command, args: &mut Vec<OsString>) {
    let mut at = root;
    let mut index = 0;
    while at.has_subcommands() {
        let marker = args.get(index).is_some_and(|v| v == "--");
        let Some(word) = args
            .get(index + usize::from(marker))
            .and_then(|s| s.to_str())
        else {
            return;
        };
        let Some(child) = at.find_subcommand(word).filter(|c| !c.is_hide_set()) else {
            // A group can display help or version after a marker. Never
            // promote a completion installer or private Action operation.
            if marker && (word == "--help" || (index == 0 && word == "--version")) {
                args.remove(index);
            }
            return;
        };
        if marker {
            args.remove(index);
        }
        index += 1;
        at = child;
    }

    #[cfg(windows)]
    windows_path_inputs(at, args, index);

    // A value-taking option consumes the next word even when it is literally
    // "--". Attach just that value, leaving an actual leaf terminator alone.
    if !args[index..].iter().any(|word| word == "--") {
        return;
    }
    let mut words = args.split_off(index).into_iter();
    while let Some(mut word) = words.next() {
        if word == "--" {
            args.push(word);
            args.extend(words);
            break;
        }
        if let Some(flag) = word.to_str()
            && next_word_option(at, flag).is_some()
            && let Some(value) = words.next()
        {
            if value == "--" {
                let suffix = if flag.starts_with("--") { "=--" } else { "--" };
                word.push(suffix);
            } else {
                args.push(word);
                args.push(value);
                continue;
            }
        }
        args.push(word);
    }
}

fn next_word_option<'a>(at: &'a Command, word: &str) -> Option<&'a clap::Arg> {
    if let Some(long) = word.strip_prefix("--") {
        return at.get_arguments().find(|arg| {
            arg.get_action().takes_values()
                && arg
                    .get_long_and_visible_aliases()
                    .is_some_and(|names| names.contains(&long))
        });
    }
    if let Some(shorts) = word.strip_prefix('-') {
        let mut chars = shorts.chars();
        while let Some(short) = chars.next() {
            let Some(arg) = at.get_arguments().find(|arg| {
                arg.get_short_and_visible_aliases()
                    .is_some_and(|names| names.contains(&short))
            }) else {
                break;
            };
            if arg.get_action().takes_values() {
                return chars.as_str().is_empty().then_some(arg);
            }
        }
    }
    None
}

#[cfg(windows)]
fn windows_path_inputs(at: &Command, args: &mut [OsString], index: usize) {
    let (names, operands) = args.split_at_mut(index);
    // The new inspection command has no installed Typer predecessor. Hidden
    // commands are rejected during group traversal before reaching this point.
    if names == ["repo", "inspect"] {
        return;
    }
    let is_path = |arg: &clap::Arg| {
        arg.get_value_parser().type_id() == std::any::TypeId::of::<std::path::PathBuf>()
    };
    // Only workflow and policy checks have public path positionals, each a
    // single repeatable operand. Other positional words keep their own grammar.
    let positional = at.get_positionals().any(is_path);
    let mut options = true;
    let mut words = operands.iter_mut();
    while let Some(word) = words.next() {
        if options && word == "--" {
            options = false;
            continue;
        }
        let input = if options && word.as_encoded_bytes().starts_with(b"-") {
            let Some(option) = word.to_str().and_then(|word| next_word_option(at, word)) else {
                continue;
            };
            let Some(value) = words.next() else { break };
            if !is_path(option) {
                continue;
            }
            value
        } else if positional {
            word
        } else {
            continue;
        };
        if let Some(mut expanded) = yaga_core::fs::windows_cli_home(input) {
            // A home is a filesystem path. Keep even an empty or option-like
            // profile value a path operand when passing it on to Clap.
            if expanded.is_empty() {
                expanded.push(".");
            } else if expanded.as_encoded_bytes().starts_with(b"-") {
                let mut literal = OsString::from(".\\");
                literal.push(expanded);
                expanded = literal;
            }
            *input = expanded;
        }
    }
}
