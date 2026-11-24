use super::{Output, Page};
use clap::{
    Arg, Command, Error,
    error::{ContextKind, ContextValue, ErrorKind},
};
use fast_rich::{Style, Text};
use regex::Regex;
use std::{ffi::OsString, sync::LazyLock};
use yaga_core::text::safe_text;

fn values(error: &Error, key: ContextKind) -> Vec<String> {
    match error.get(key) {
        Some(ContextValue::String(s)) => vec![s.clone()],
        Some(ContextValue::Strings(s)) => s.clone(),
        Some(other) => vec![other.to_string()],
        None => Vec::new(),
    }
}
fn arg_name(value: &str) -> &str {
    value.split_whitespace().next().unwrap_or(value)
}
fn options(arg: &Arg) -> String {
    let mut flags = Vec::new();
    if let Some(short) = arg.get_short() {
        flags.push(format!("-{short}"));
    }
    if let Some(long) = arg.get_long() {
        flags.push(format!("--{long}"));
    }
    flags.join(" / ")
}
fn literal(text: &str) -> String {
    format!("'{}'", safe_text(text, 500))
}

pub(super) fn unknown_eager_option(at: &Command, args: &[OsString]) -> Option<String> {
    let mut args = args.iter().filter_map(|s| s.to_str());
    while let Some(token) = args.next() {
        if token == "--" || (!token.starts_with('-') && at.has_subcommands()) {
            break;
        }
        if !token.starts_with('-') {
            continue;
        }
        let flag = token.split('=').next().unwrap_or(token);
        let arg = at.get_arguments().find(|arg| {
            arg.get_long()
                .is_some_and(|long| flag.strip_prefix("--") == Some(long))
                || (flag.len() == 2
                    && arg
                        .get_short()
                        .is_some_and(|c| !matches!(c, 'h' | 'V') && flag.ends_with(c)))
        });
        let Some(arg) = arg else {
            return Some(flag.to_owned());
        };
        if !arg.get_action().takes_values() && token.contains('=') {
            return Some(token.to_owned());
        }
        if arg.get_action().takes_values() && !token.contains('=') {
            args.next();
        }
    }
    None
}

// Click's long-option hints use SequenceMatcher's ordered longest common blocks.
fn matched(a: &[u8], b: &[u8]) -> usize {
    let mut best = (0usize, 0usize, 0usize);
    let mut prior = vec![0usize; b.len() + 1];
    for (i, c) in a.iter().enumerate() {
        let mut next = vec![0; b.len() + 1];
        for (j, d) in b.iter().enumerate() {
            if c == d {
                let n = prior[j] + 1;
                next[j + 1] = n;
                if n > best.2 {
                    best = (i + 1 - n, j + 1 - n, n);
                }
            }
        }
        prior = next;
    }
    let (i, j, n) = best;
    if n == 0 {
        return 0;
    }
    n + matched(&a[..i], &b[..j]) + matched(&a[i + n..], &b[j + n..])
}
fn suggestions(at: &Command, flag: &str) -> Vec<String> {
    if !flag.starts_with("--") || flag.len() > 128 {
        return Vec::new();
    }
    let mut matches = Vec::new();
    for name in at.get_arguments().filter_map(|arg| arg.get_long()) {
        let option = format!("--{name}");
        let size = option.len() + flag.len();
        let common = matched(option.as_bytes(), flag.as_bytes());
        if common * 10 >= size * 3 {
            matches.push((2.0 * common as f64 / size as f64, option));
        }
    }
    matches.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| b.1.cmp(&a.1)));
    let mut options: Vec<_> = matches.into_iter().take(3).map(|(_, name)| name).collect();
    options.sort();
    options
}
fn highlighted(value: &str) -> Text {
    static OPTIONS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"--[\w-]+|-\w+|<[^>]+>").unwrap());
    let mut out = Text::default();
    let mut cursor = 0;
    for found in OPTIONS.find_iter(value) {
        if found.start() > 0
            && value[..found.start()]
                .chars()
                .next_back()
                .is_some_and(|c| c.is_alphanumeric() || c == '_')
        {
            continue;
        }
        out.push(value[cursor..found.start()].to_owned());
        let m = found.as_str();
        out.push_styled(
            m.to_owned(),
            Style::parse(if m.starts_with("--") {
                "bold cyan"
            } else if m.starts_with('-') {
                "bold green"
            } else {
                "bold yellow"
            }),
        );
        cursor = found.end();
    }
    out.push(value[cursor..].to_owned());
    out
}

pub(super) fn render(
    out: &mut Output,
    page: &Page,
    at: &Command,
    path: &str,
    args: &[OsString],
    error: &Error,
) {
    let invalid = values(error, ContextKind::InvalidArg);
    let option = invalid.first().map(|s| arg_name(s)).unwrap_or("");
    let raw = args.first().and_then(|s| s.to_str()).unwrap_or("");
    let name = if option.is_empty() { raw } else { option };
    let wrong = values(error, ContextKind::InvalidValue)
        .into_iter()
        .next()
        .unwrap_or_default();
    // After a leaf's option terminator even an option-looking token is a
    // positional. Only apply its precedence to an error on that suffix.
    let terminator = args.iter().position(|s| s == "--");
    let trailing = terminator.filter(|&index| {
        !at.has_subcommands() && args.get(index + 1).is_some_and(|arg| arg == name)
    });
    let mut usage = true;
    let report = match error.kind() {
        _ if at.has_subcommands() && args.len() == 1 && raw == "--" => {
            "Missing command.".to_owned()
        }
        _ if at.has_subcommands() && raw == "--" && args.get(1).is_some_and(|s| s == "--") => {
            let suggestion = args
                .get(2)
                .and_then(|s| s.to_str())
                .and_then(|name| at.find_subcommand(name))
                .filter(|cmd| !cmd.is_hide_set());
            match suggestion {
                Some(cmd) => format!(
                    "No such command '--'. Did you mean {}?",
                    literal(cmd.get_name())
                ),
                None => "No such command '--'.".to_owned(),
            }
        }
        ErrorKind::InvalidSubcommand
            if raw == "--"
                && args
                    .get(1)
                    .and_then(|s| s.to_str())
                    .is_some_and(|s| s.starts_with('-')) =>
        {
            format!(
                "No such option: {}",
                safe_text(&args[1].to_string_lossy(), 500)
            )
        }
        ErrorKind::InvalidSubcommand => format!(
            "No such command {}.",
            literal(
                &values(error, ContextKind::InvalidSubcommand)
                    .into_iter()
                    .next()
                    .unwrap_or_else(|| raw.to_owned())
            )
        ),
        ErrorKind::MissingRequiredArgument => {
            let first = page
                .panels
                .iter()
                .flat_map(|p| p.rows.iter())
                .filter_map(|row| {
                    let first = row.first()?.text().plain_text();
                    if first != "*" {
                        return None;
                    }
                    let flag = row.get(1)?.text().plain_text();
                    invalid.iter().any(|s| arg_name(s) == flag).then_some(flag)
                })
                .next()
                .unwrap_or_else(|| name.to_owned());
            format!("Missing option {}.", literal(&first))
        }
        ErrorKind::InvalidValue | ErrorKind::ValueValidation => {
            if wrong.is_empty() && args.last().is_some_and(|s| s == name) {
                usage = false;
                format!("Option {} requires an argument.", literal(name))
            } else {
                let arg = at.get_arguments().find(|a| {
                    a.get_long()
                        .is_some_and(|s| name.trim_start_matches('-') == s)
                        || (a.is_positional() && a.to_string() == name)
                });
                let mut choices = values(error, ContextKind::ValidValue);
                if choices.is_empty()
                    && let Some(i) = arg.and_then(Arg::get_index)
                {
                    // A domain FromStr parser may not expose its choices to
                    // Clap. The published positional metadata retains them.
                    if let Some(spec) = super::completion::menu(path, at)
                        .arguments
                        .into_iter()
                        .nth(i - 1)
                    {
                        choices = spec.choices;
                    }
                }
                let shown = arg
                    .map(|a| {
                        if a.is_positional() {
                            a.get_id().to_string()
                        } else {
                            options(a)
                        }
                    })
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| name.to_owned());
                if !choices.is_empty() {
                    format!(
                        "Invalid value for {}: {} is not one of {}.",
                        literal(&shown),
                        literal(&wrong),
                        choices
                            .iter()
                            .map(|s| literal(s))
                            .collect::<Vec<_>>()
                            .join(", ")
                    )
                } else {
                    format!(
                        "Invalid value for {}: {} is not a valid integer.",
                        literal(&shown),
                        literal(&wrong)
                    )
                }
            }
        }
        _ if args.iter().filter_map(|a| a.to_str()).any(|raw| {
            raw.split_once('=').is_some_and(|(flag, _)| {
                Some(flag) == name.split('=').next()
                    && at.get_arguments().any(|option| {
                        !option.get_action().takes_values()
                            && option
                                .get_long()
                                .is_some_and(|long| flag.strip_prefix("--") == Some(long))
                    })
            })
        }) =>
        {
            usage = false;
            format!(
                "Option {} does not take a value.",
                literal(name.split('=').next().unwrap_or(name))
            )
        }
        _ => {
            if name.starts_with('-') && trailing.is_none() {
                let flag = name.split('=').next().unwrap_or(name);
                let related = suggestions(at, flag);
                let suggestion = if flag.starts_with("--") && !related.is_empty() {
                    format!(" (Possible options: {})", related.join(", "))
                } else {
                    String::new()
                };
                format!("No such option: {}{suggestion}", safe_text(flag, 500))
            } else if at.has_subcommands() {
                format!("No such command {}.", literal(name))
            } else {
                let mut missing = page.panels.iter().flat_map(|p| &p.rows).filter_map(|row| {
                    if row.first()?.text().plain_text() != "*" {
                        return None;
                    }
                    let flag = row.get(1)?.text().plain_text();
                    (flag.starts_with("--")
                        && !args[..trailing.unwrap_or(args.len())].iter().any(|a| {
                            a.to_str()
                                .is_some_and(|v| v.split('=').next() == Some(&flag))
                        }))
                    .then_some(flag)
                });
                if let Some(flag) = missing.next() {
                    format!("Missing option {}.", literal(&flag))
                } else {
                    let extra = args
                        .iter()
                        .skip_while(|a| a.to_str() != Some(name))
                        .map(|a| a.to_string_lossy())
                        .collect::<Vec<_>>()
                        .join(" ");
                    format!(
                        "Got unexpected extra argument(s) ({})",
                        safe_text(if extra.is_empty() { name } else { &extra }, 500)
                    )
                }
            }
        }
    };
    if usage {
        out.append(&out.usage(page, 0));
        if !out.buffer.ends_with('\n') {
            out.buffer.push('\n');
        }
        let location = if path.is_empty() {
            "yaga".into()
        } else {
            format!("yaga {path}")
        };
        let mut hint = Text::from_spans([
            fast_rich::Span::styled("Try ", Style::new().dim()),
            fast_rich::Span::styled(format!("'{location} "), Style::parse("dim blue")),
            fast_rich::Span::styled("--help", Style::parse("bold dim blue")),
            fast_rich::Span::styled("'", Style::parse("dim blue")),
            fast_rich::Span::styled(" for help.", Style::new().dim()),
        ]);
        hint.overflow = fast_rich::text::Overflow::Wrap;
        out.append(&hint);
        if !out.buffer.ends_with('\n') {
            out.buffer.push('\n');
        }
    }
    let text = highlighted(&report);
    out.append(&out.panel("Error", text, Style::parse("red")));
}
