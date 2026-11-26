//! Read the environment contract used by the installed shell scripts. Only
//! candidate values and descriptions from trusted command metadata reach stdout.
use crate::presentation::completion::{Menu, OptionSpec, menu, terminal_width, wrap_help};
use clap::{Command, CommandFactory};
use std::{collections::HashSet, env, process::ExitCode};

#[cfg(test)]
mod tests;

#[derive(Clone, Copy, PartialEq)]
enum Protocol {
    Bash,
    Zsh,
    Fish,
    PowerShell,
}

fn split_words(raw: &str) -> Vec<String> {
    // POSIX shlex with whitespace splitting, comments disabled and incomplete
    // trailing quotes/escapes accepted. Shell expansions are always literal.
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quoted = None;
    let mut present = false;
    let mut chars = raw.chars();
    while let Some(c) = chars.next() {
        if quoted != Some('\'') && c == '\\' {
            if let Some(next) = chars.next() {
                if quoted == Some('"') && !matches!(next, '"' | '\\') {
                    word.push(c)
                }
                word.push(next);
            }
            present = true;
        } else if quoted == Some(c) {
            quoted = None;
        } else if quoted.is_none() && matches!(c, '\'' | '"') {
            quoted = Some(c);
            present = true;
        } else if quoted.is_none() && matches!(c, ' ' | '\n' | '\t' | '\r') {
            if present {
                words.push(std::mem::take(&mut word));
                present = false;
            }
        } else {
            word.push(c);
            present = true;
        }
    }
    if present {
        words.push(word)
    }
    words
}

fn request(shell: Protocol) -> Option<(Vec<String>, String)> {
    let raw = if shell == Protocol::Bash {
        env::var("COMP_WORDS").ok()?
    } else {
        env::var("_TYPER_COMPLETE_ARGS").unwrap_or_default()
    };
    if raw.len() > 65_536 {
        return None;
    }
    let mut words = split_words(&raw);
    if shell == Protocol::Bash {
        let index = env::var("COMP_CWORD").ok()?.trim().parse::<i64>().ok()?;
        let len = words.len() as i64;
        let i = if index < 0 {
            len.saturating_add(index)
        } else {
            index
        };
        let incomplete = usize::try_from(i)
            .ok()
            .and_then(|i| words.get(i))
            .cloned()
            .unwrap_or_default();
        let args = words
            .into_iter()
            .take(i.clamp(0, len) as usize)
            .skip(1)
            .collect();
        return Some((args, incomplete));
    }
    if !words.is_empty() {
        words.remove(0);
    }
    let incomplete = if shell == Protocol::PowerShell {
        let partial = env::var("_TYPER_COMPLETE_WORD_TO_COMPLETE").unwrap_or_default();
        if partial.len() > 65_536 {
            return None;
        }
        if !partial.is_empty() {
            words.pop();
        }
        partial
    } else if !raw.ends_with(' ') {
        words.pop().unwrap_or_default()
    } else {
        String::new()
    };
    Some((words, incomplete))
}

struct Context {
    menu: Menu,
    seen: HashSet<usize>,
    positionals: usize,
}

fn find_option(menu: &Menu, name: &str) -> Option<usize> {
    menu.options
        .iter()
        .position(|p| p.names.iter().any(|s| s == name))
}

fn resolve(root: &Command, args: &[String]) -> Context {
    let mut cmd = root;
    let mut path = String::new();
    let mut ctx = Context {
        menu: menu(&path, cmd),
        seen: HashSet::new(),
        positionals: 0,
    };
    let mut args = args.iter();
    let mut options = true;
    while let Some(word) = args.next() {
        if options && word == "--" {
            options = false;
            continue;
        }
        if options && word.starts_with("--") {
            let (key, inline) = word
                .split_once('=')
                .map_or((word.as_str(), false), |(k, _)| (k, true));
            let Some(i) = find_option(&ctx.menu, key) else {
                break;
            };
            let takes_value = ctx.menu.options[i].takes_value;
            if (takes_value && !inline && args.next().is_none()) || (!takes_value && inline) {
                break;
            }
            ctx.seen.insert(i);
            continue;
        }
        if options && word.starts_with('-') && word.len() > 1 {
            let mut flags = word[1..].chars().peekable();
            let mut recognized = true;
            while let Some(flag) = flags.next() {
                let Some(i) = find_option(&ctx.menu, &format!("-{flag}")) else {
                    recognized = false;
                    break;
                };
                if ctx.menu.options[i].takes_value {
                    if flags.peek().is_none() && args.next().is_none() {
                        recognized = false;
                        break;
                    }
                    ctx.seen.insert(i);
                    break;
                }
                ctx.seen.insert(i);
            }
            if !recognized {
                break;
            }
            continue;
        }
        if cmd.has_subcommands() {
            let Some(child) = cmd.find_subcommand(word).filter(|c| !c.is_hide_set()) else {
                break;
            };
            cmd = child;
            if !path.is_empty() {
                path.push(' ')
            }
            path.push_str(cmd.get_name());
            ctx = Context {
                menu: menu(&path, cmd),
                seen: HashSet::new(),
                positionals: 0,
            };
            options = true;
        } else {
            ctx.positionals += 1
        }
    }
    ctx
}

fn short_help(description: &str) -> String {
    let words = description
        .split("\n\n")
        .next()
        .unwrap_or_default()
        .split_whitespace()
        .collect::<Vec<_>>();
    let mut short = String::new();
    for (i, w) in words.iter().enumerate() {
        let size = short.chars().count() + usize::from(i > 0) + w.chars().count();
        if size > 45 || (size == 45 && i + 1 < words.len() && !w.ends_with('.')) {
            while short.chars().count() > 42 {
                short.truncate(short.rfind(' ').unwrap_or(0));
            }
            short.push_str("...");
            return short;
        }
        if i > 0 {
            short.push(' ')
        }
        short.push_str(w);
        if w.ends_with('.') {
            break;
        }
    }
    short
}

fn values(param: &OptionSpec, partial: &str) -> Vec<(String, String)> {
    let needle = if param.ignore_case {
        partial.to_lowercase()
    } else {
        partial.to_owned()
    };
    param
        .choices
        .iter()
        .filter(|s| {
            if param.ignore_case {
                s.to_lowercase().starts_with(&needle)
            } else {
                s.starts_with(&needle)
            }
        })
        .map(|s| (s.clone(), String::new()))
        .collect()
}

fn candidates(root: &Command, args: &[String], partial: &str) -> Vec<(String, String)> {
    let ctx = resolve(root, args);
    let after_terminator = args.iter().any(|s| s == "--");
    let mut previous = args.last().map(String::as_str);
    let mut prefix = partial;
    if !after_terminator {
        if partial == "=" {
            prefix = ""
        } else if partial.starts_with('-')
            && let Some((name, value)) = partial.split_once('=')
        {
            previous = Some(name);
            prefix = value
        }
    }
    if !prefix.starts_with('-') || after_terminator {
        if !after_terminator
            && let Some(i) = previous.and_then(|v| find_option(&ctx.menu, v))
            && ctx.menu.options[i].takes_value
        {
            return values(&ctx.menu.options[i], prefix);
        }
        if let Some(param) = ctx
            .menu
            .arguments
            .iter()
            .enumerate()
            .find(|(i, p)| p.repeatable || *i >= ctx.positionals)
            .map(|(_, p)| p)
        {
            return values(param, prefix);
        }
        if after_terminator {
            return Vec::new();
        }
    }
    let mut result: Vec<_> = ctx
        .menu
        .commands
        .iter()
        .filter(|(name, _)| name.starts_with(prefix))
        .map(|(name, desc)| (name.clone(), short_help(desc)))
        .collect();
    if prefix.starts_with('-') {
        for (i, p) in ctx.menu.options.iter().enumerate() {
            if p.repeatable || !ctx.seen.contains(&i) {
                result.extend(
                    p.names
                        .iter()
                        .filter(|n| n.starts_with(prefix))
                        .map(|n| (n.clone(), p.help.clone())),
                );
            }
        }
    }
    result
}

fn zsh(text: &str) -> String {
    text.replace('"', "\"\"")
        .replace('\'', "''")
        .replace('$', "\\$")
        .replace('`', "\\`")
        .replace(':', r"\\:")
}

fn render(shell: Protocol, items: &[(String, String)], fish: &str) -> (u8, Option<String>) {
    if shell == Protocol::Fish && fish == "is-args" {
        return (u8::from(items.is_empty()), None);
    }
    if shell == Protocol::Fish && fish != "get-args" {
        return (0, Some(String::new()));
    }
    let width = terminal_width();
    let lines = items
        .iter()
        .map(|(name, help)| match shell {
            Protocol::Bash => name.clone(),
            Protocol::Zsh if help.is_empty() => format!("\"{}\"", zsh(name)),
            Protocol::Zsh => format!("\"{}\":\"{}\"", zsh(name), wrap_help(&zsh(help), width)),
            Protocol::Fish if !help.is_empty() => {
                format!(
                    "{name}\t{}",
                    wrap_help(&help.replace(char::is_whitespace, " "), width)
                )
            }
            Protocol::Fish => name.clone(),
            Protocol::PowerShell if help.is_empty() => format!("{name}::: "),
            Protocol::PowerShell => format!("{name}:::{}", wrap_help(help, width)),
        })
        .collect::<Vec<_>>()
        .join("\n");
    let out = match shell {
        Protocol::Zsh if items.is_empty() => "_files".to_owned(),
        Protocol::Zsh => format!("_arguments '*: :(({lines}))'"),
        _ => lines,
    };
    (0, Some(out))
}

pub(crate) fn run() -> Option<ExitCode> {
    let mode = env::var("_YAGA_COMPLETE").ok()?;
    if mode.is_empty() {
        return None;
    }
    let Some((instruction, name)) = mode.split_once('_') else {
        eprintln!("Invalid completion instruction.");
        return Some(ExitCode::FAILURE);
    };
    let shell = match name {
        "bash" => Protocol::Bash,
        "fish" => Protocol::Fish,
        "zsh" => Protocol::Zsh,
        "pwsh" | "powershell" => Protocol::PowerShell,
        _ => {
            eprintln!(
                "Shell {} not supported.",
                yaga_core::text::safe_text(name, 100)
            );
            return Some(ExitCode::FAILURE);
        }
    };
    if instruction == "source" {
        println!(
            "{}",
            super::profile::script(name).expect("supported completion shell")
        );
        return Some(ExitCode::SUCCESS);
    }
    if instruction != "complete" {
        eprintln!(
            "Completion instruction \"{}\" not supported.",
            yaga_core::text::safe_text(instruction, 100)
        );
        return Some(ExitCode::FAILURE);
    }
    let Some((args, prefix)) = request(shell) else {
        eprintln!("Invalid shell completion request.");
        return Some(ExitCode::FAILURE);
    };
    let mut root = crate::Cli::command();
    root.build();
    let items = candidates(&root, &args, &prefix);
    let (code, out) = render(
        shell,
        &items,
        &env::var("_TYPER_COMPLETE_FISH_ACTION").unwrap_or_default(),
    );
    if let Some(s) = out {
        println!("{s}")
    }
    Some(ExitCode::from(code))
}
