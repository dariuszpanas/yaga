//! Reuse the original terminal layout for established commands. New commands describe themselves
//! through their Clap definitions and use the same Rich panels and color styles.
use super::{Copy, HelpPanel, Page};
use clap::{Arg, Command};
use std::borrow::Cow;

fn styled(style: &str, text: String) -> Copy {
    Copy::Styled {
        spans: vec![(style.to_owned(), text)],
        base: String::new(),
    }
}

fn option(arg: &Arg, has_short: bool) -> Vec<Copy> {
    let (primary, short) = match (arg.get_long(), arg.get_short()) {
        (Some(long), Some(short)) => (
            styled("bold cyan", format!("--{long}")),
            styled("bold green", format!("-{short}")),
        ),
        (Some(long), None) => (
            styled("bold cyan", format!("--{long}")),
            Copy::Plain(String::new()),
        ),
        (None, Some(short)) => (
            styled("bold green", format!("-{short}")),
            Copy::Plain(String::new()),
        ),
        (None, None) => (
            styled("bold cyan", arg.get_id().to_string()),
            Copy::Plain(String::new()),
        ),
    };
    let choices = arg
        .get_possible_values()
        .into_iter()
        .filter(|v| !v.is_hide_set())
        .map(|v| v.get_name().to_owned())
        .collect::<Vec<_>>();
    let value = if !choices.is_empty() {
        format!("<{}>", choices.join("|"))
    } else if arg.get_action().takes_values() {
        format!(
            "<{}>",
            arg.get_value_names()
                .and_then(|s| s.first())
                .map(|s| s.as_str())
                .unwrap_or("value")
        )
    } else {
        String::new()
    };
    let mut description = vec![Copy::Plain(
        arg.get_help().map(ToString::to_string).unwrap_or_default(),
    )];
    let required = arg.is_required_set();
    if required {
        description.push(styled("dim red", "[required]".into()));
    } else if arg.get_action().takes_values() && !arg.get_default_values().is_empty() {
        let defaults = arg
            .get_default_values()
            .iter()
            .map(|v| v.to_string_lossy())
            .collect::<Vec<_>>();
        description.push(styled("dim", format!("[default: {}]", defaults.join(", "))));
    }
    let mut row = vec![
        styled("red", if required { "*".into() } else { String::new() }),
        primary,
    ];
    if has_short {
        row.push(short);
    }
    row.push(styled("bold yellow", value));
    row.push(Copy::Columns {
        columns: description,
    });
    row
}

fn from_command(command: &Command) -> Page {
    let usage = command.clone().render_usage().to_string();
    let usage = usage.strip_prefix("Usage: ").unwrap_or(&usage);
    let mut panels = Vec::new();
    for (positionals, title) in [(true, "Arguments"), (false, "Options")] {
        let args: Vec<_> = command
            .get_arguments()
            .filter(|arg| !arg.is_hide_set() && arg.is_positional() == positionals)
            .collect();
        let has_short = !positionals
            && args
                .iter()
                .any(|arg| arg.get_long().is_some() && arg.get_short().is_some());
        let rows: Vec<_> = args.into_iter().map(|arg| option(arg, has_short)).collect();
        if !rows.is_empty() {
            panels.push(HelpPanel {
                title: title.into(),
                rows,
                command_width: None,
            });
        }
    }
    Page {
        usage: Copy::Styled {
            spans: vec![
                ("bold yellow".into(), "Usage: ".into()),
                ("bold".into(), usage.into()),
            ],
            base: String::new(),
        },
        usage_widths: Vec::new(),
        about: command.get_about().map(|a| Copy::Plain(a.to_string())),
        panels,
        epilog: command.get_after_help().map(|e| Copy::Plain(e.to_string())),
    }
}

pub(super) fn for_command<'a>(existing: Option<&'a Page>, command: &Command) -> Cow<'a, Page> {
    let mut page = existing
        .map(Cow::Borrowed)
        .unwrap_or_else(|| Cow::Owned(from_command(command)));
    for child in command.get_subcommands().filter(|c| !c.is_hide_set()) {
        let name = child.get_name();
        let index = page
            .panels
            .iter()
            .position(|panel| panel.title == "Commands");
        if index.is_some_and(|i| {
            page.panels[i]
                .rows
                .iter()
                .any(|row| matches!(row.first(), Some(Copy::Plain(existing)) if existing == name))
        }) {
            continue;
        }
        let panels = &mut page.to_mut().panels;
        let index = index.unwrap_or_else(|| {
            panels.push(HelpPanel {
                title: "Commands".into(),
                rows: Vec::new(),
                command_width: Some(0),
            });
            panels.len() - 1
        });
        let panel = &mut panels[index];
        let about = child
            .get_about()
            .map(ToString::to_string)
            .unwrap_or_default();
        panel
            .rows
            .push(vec![Copy::Plain(name.into()), Copy::Plain(about)]);
        panel.command_width = Some(panel.command_width.unwrap_or(0).max(name.len() + 2));
    }
    page
}

#[cfg(test)]
mod tests {
    use clap::{Command, CommandFactory};
    use std::collections::BTreeSet;

    fn checkers(group: &Command, path: &str, found: &mut BTreeSet<String>) {
        if group.has_subcommands() {
            for child in group.get_subcommands().filter(|c| !c.is_hide_set()) {
                let next = format!("{path} {}", child.get_name());
                checkers(child, next.trim_start(), found);
            }
        } else if !matches!(
            path,
            "commit template" | "config init" | "config show" | "policy init" | "self update"
        ) {
            let title = if path.starts_with("gate agent-review ") && !path.contains(" policy ") {
                "gate agent-review <operation>"
            } else {
                path
            };
            found.insert(title.into());
        }
    }

    #[test]
    fn guide_matches_native_command_registry() {
        let workspace_docs =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../docs/commands.md");
        if !workspace_docs.is_file() {
            // Packaged crates.io tarballs do not bundle the repository docs tree.
            assert!(
                !std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../../Cargo.toml")
                    .is_file(),
                "missing docs/commands.md in workspace checkout"
            );
            return;
        }
        let mut expected = BTreeSet::new();
        checkers(&crate::Cli::command(), "", &mut expected);
        let documented = std::fs::read_to_string(&workspace_docs)
            .expect("read docs/commands.md")
            .lines()
            .filter_map(|line| {
                line.strip_prefix("| [`yaga ")?
                    .split_once("`](")
                    .map(|(cmd, _)| cmd.to_owned())
            })
            .collect::<BTreeSet<_>>();
        assert_eq!(documented, expected);
    }

    #[test]
    fn dynamic_help_renders_short_flags_when_present() {
        let cmd = Command::new("demo")
            .arg(
                clap::Arg::new("quiet")
                    .long("quiet")
                    .short('q')
                    .action(clap::ArgAction::SetTrue)
                    .help("Suppress output."),
            )
            .arg(
                clap::Arg::new("format")
                    .long("format")
                    .value_parser(["text", "json"]),
            );
        let page = super::from_command(&cmd);
        let options = page
            .panels
            .iter()
            .find(|p| p.title == "Options")
            .expect("options panel");
        assert_eq!(options.rows[0].len(), 5);
        assert_eq!(options.rows[0][1].text().plain_text(), "--quiet");
        assert_eq!(options.rows[0][2].text().plain_text(), "-q");
        assert_eq!(options.rows[1][1].text().plain_text(), "--format");
        assert_eq!(options.rows[1][2].text().plain_text(), "");
    }
}
