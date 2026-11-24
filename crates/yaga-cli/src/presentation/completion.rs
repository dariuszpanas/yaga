//! Completion uses the same command descriptions and option ordering as terminal help.
use super::{Copy, PAGES, catalog};
use clap::{Arg, ArgAction, Command};
use regex::Regex;
use std::sync::LazyLock;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub(crate) fn terminal_width() -> usize {
    super::Output::with_min_width(false, 0).context.width
}

pub(crate) fn wrap_help(help: &str, width: usize) -> String {
    static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*\S+\s*").unwrap());
    if width == 0 {
        return String::new();
    }
    let mut rendered = Vec::new();
    for line in help.split('\n') {
        if line.width() <= width {
            rendered.push(line.to_owned());
            continue;
        }
        // Rich assigns separating whitespace to the preceding word. When
        // folding an overlong word, that whitespace can occupy its own line.
        let mut breaks = vec![0];
        let mut used = 0;
        for word in WORD.find_iter(line) {
            let body_width = word.as_str().trim_end().width();
            if used + body_width <= width {
                used += word.as_str().width();
                continue;
            }
            if word.start() != 0 {
                breaks.push(word.start());
            }
            if body_width <= width {
                used = word.as_str().width();
                continue;
            }
            used = 0;
            for (offset, grapheme) in word.as_str().grapheme_indices(true) {
                let cells = grapheme.width();
                if used > 0 && used + cells > width {
                    breaks.push(word.start() + offset);
                    used = 0;
                }
                used += cells;
            }
        }
        breaks.push(line.len());
        for bounds in breaks.windows(2) {
            let segment = &line[bounds[0]..bounds[1]];
            let mut used = 0;
            let mut clipped = String::new();
            for grapheme in segment.graphemes(true) {
                let cells = grapheme.width();
                if used + cells > width {
                    clipped.extend(std::iter::repeat_n(' ', width - used));
                    break;
                }
                used += cells;
                clipped.push_str(grapheme);
            }
            rendered.push(clipped);
        }
    }
    rendered.join("\n").trim_end_matches('\n').to_owned()
}

pub(crate) struct OptionSpec {
    pub names: Vec<String>,
    pub help: String,
    pub choices: Vec<String>,
    pub takes_value: bool,
    pub repeatable: bool,
    pub ignore_case: bool,
}

pub(crate) struct Menu {
    pub commands: Vec<(String, String)>,
    pub options: Vec<OptionSpec>,
    pub arguments: Vec<OptionSpec>,
}

fn plain(cell: &Copy) -> String {
    match cell {
        Copy::Plain(text) => text.clone(),
        Copy::Styled { spans, .. } => spans.iter().map(|(_, text)| text.as_str()).collect(),
        Copy::Columns { columns } => columns.first().map(plain).unwrap_or_default(),
    }
}

fn declared(arg: &Arg) -> OptionSpec {
    let mut names = Vec::new();
    if let Some(long) = arg.get_long() {
        names.push(format!("--{long}"));
    }
    if let Some(short) = arg.get_short() {
        names.push(format!("-{short}"));
    }
    names.extend(
        arg.get_visible_aliases()
            .into_iter()
            .flatten()
            .map(|a| format!("--{a}")),
    );
    names.extend(
        arg.get_visible_short_aliases()
            .into_iter()
            .flatten()
            .map(|short| format!("-{short}")),
    );
    OptionSpec {
        names,
        help: arg.get_help().map(ToString::to_string).unwrap_or_default(),
        choices: arg
            .get_possible_values()
            .into_iter()
            .filter(|v| !v.is_hide_set())
            .map(|v| v.get_name().to_owned())
            .collect(),
        takes_value: arg.get_action().takes_values(),
        repeatable: matches!(arg.get_action(), ArgAction::Append | ArgAction::Count),
        ignore_case: arg.is_ignore_case_set(),
    }
}

pub(crate) fn menu(path: &str, command: &Command) -> Menu {
    let mut menu = Menu {
        commands: Vec::new(),
        options: Vec::new(),
        arguments: Vec::new(),
    };
    let mut declarations: Vec<_> = command
        .get_arguments()
        .filter(|arg| !arg.is_hide_set())
        .collect();
    let existing = PAGES.get(path);
    let page = catalog::for_command(existing, command);
    menu.commands = page
        .panels
        .iter()
        .filter(|p| p.title == "Commands")
        .flat_map(|p| &p.rows)
        .map(|row| (plain(&row[0]), plain(&row[1])))
        .filter(|(name, _)| {
            command
                .find_subcommand(name)
                .is_some_and(|c| !c.is_hide_set())
        })
        .collect();
    if let Some(page) = existing {
        for panel in &page.panels {
            for row in &panel.rows {
                let options = panel.title == "Options";
                if !options && panel.title != "Arguments" {
                    continue;
                }
                let value = plain(&row[row.len() - 2]);
                let names: Vec<_> = row[..row.len() - 2]
                    .iter()
                    .map(plain)
                    .filter(|s| s.starts_with('-'))
                    .collect();
                let declaration = declarations
                    .iter()
                    .position(|arg| {
                        if options {
                            arg.get_long().is_some_and(|long| {
                                names.iter().any(|n| n.strip_prefix("--") == Some(long))
                            })
                        } else {
                            arg.is_positional()
                        }
                    })
                    .map(|i| declarations.remove(i));
                let choices = value
                    .strip_prefix('<')
                    .and_then(|s| s.strip_suffix('>'))
                    .filter(|v| v.contains('|'))
                    .into_iter()
                    .flat_map(|v| v.split('|'))
                    .map(str::to_owned)
                    .collect();
                let spec = OptionSpec {
                    names,
                    help: plain(row.last().expect("help row")),
                    choices,
                    takes_value: !value.is_empty(),
                    repeatable: declaration.is_some_and(|arg| {
                        matches!(arg.get_action(), ArgAction::Append | ArgAction::Count)
                    }),
                    ignore_case: declaration.is_some_and(Arg::is_ignore_case_set),
                };
                if options {
                    menu.options.push(spec)
                } else {
                    menu.arguments.push(spec)
                }
            }
        }
    }
    // Include newly declared native arguments before their help
    // metadata has an established public layout.
    for arg in declarations {
        if arg.is_positional() {
            menu.arguments.push(declared(arg));
        } else {
            let i = menu
                .options
                .iter()
                .position(|p| p.names.iter().any(|s| s == "--help"))
                .unwrap_or(menu.options.len());
            menu.options.insert(i, declared(arg));
        }
    }
    menu
}
