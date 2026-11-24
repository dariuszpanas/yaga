//! Render the installed Typer command contract using native Rich components.
mod arguments;
mod catalog;
pub(crate) mod completion;
mod usage_error;

use clap::{CommandFactory, Parser, error::ErrorKind};
use fast_rich::{
    Padding, Panel, Style, Text,
    columns::Columns,
    console::RenderContext,
    padding::PaddingSpec,
    panel::BorderStyle,
    renderable::{Renderable, Segment, SharedRenderable, fit_line},
    style::Color,
    table::{Cell, Column, ColumnWidth, Row, Table},
    text::{Alignment, Overflow, Span},
};
use serde::Deserialize;
use std::{
    collections::HashMap,
    env,
    ffi::OsString,
    io::IsTerminal,
    process::ExitCode,
    sync::{Arc, LazyLock},
};

#[derive(Clone, Deserialize)]
#[serde(untagged)]
enum Copy {
    Plain(String),
    Styled {
        spans: Vec<(String, String)>,
        #[serde(default)]
        base: String,
    },
    Columns {
        columns: Vec<Copy>,
    },
}
impl Copy {
    fn text(&self) -> Text {
        match self {
            Self::Plain(text) => Text::from(text.clone()),
            Self::Styled { spans, base } => Text::from_spans(
                spans
                    .iter()
                    .map(|(style, text)| Span::styled(text.clone(), Style::parse(style))),
            )
            .style(Style::parse(base)),
            Self::Columns { .. } => {
                unreachable!("nested columns are only valid for option descriptions")
            }
        }
    }
    fn cell(&self) -> Cell {
        match self {
            Self::Columns { columns } => Cell::renderable(OptionDescription(Columns::renderables(
                columns.iter().map(|item| {
                    Arc::new(HelpCell(item.text().overflow(Overflow::WrapEllipsis)))
                        as SharedRenderable
                }),
            ))),
            Self::Styled { base, .. } if !base.is_empty() => {
                Cell::renderable(HelpCell(self.text().overflow(Overflow::Wrap)))
            }
            _ => Cell::Text(self.text()),
        }
    }
}

// A Rich Text base style also covers the justification space up to the
// cell's right edge. Span styles (such as highlighted flags) do not.
struct HelpCell(Text);
impl Renderable for HelpCell {
    fn render(&self, context: &RenderContext) -> Vec<Segment> {
        self.0
            .render(context)
            .into_iter()
            .map(|line| Segment::line(fit_line(line.spans, context.width, self.0.style)))
            .collect()
    }
    fn min_width(&self) -> usize {
        self.0.min_width()
    }
    fn max_width(&self) -> usize {
        self.0.max_width()
    }
}

// Rich Columns reports the entire available width to its parent table.
// The contained help, default and required labels choose their own row breaks.
struct OptionDescription(Columns);
impl Renderable for OptionDescription {
    fn render(&self, context: &RenderContext) -> Vec<Segment> {
        self.0.render(context)
    }
    fn min_width(&self) -> usize {
        0
    }
}

#[derive(Clone, Deserialize)]
struct HelpPanel {
    title: String,
    rows: Vec<Vec<Copy>>,
    command_width: Option<usize>,
}
#[derive(Clone, Deserialize)]
struct Page {
    usage: Copy,
    #[serde(default)]
    usage_widths: Vec<(usize, Copy)>,
    about: Option<Copy>,
    panels: Vec<HelpPanel>,
    epilog: Option<Copy>,
}
static PAGES: LazyLock<HashMap<String, Page>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("help.json")).expect("compatibility help catalog")
});

struct Output {
    context: RenderContext,
    usage_width: usize,
    styled: bool,
    colors: bool,
    border: BorderStyle,
    buffer: String,
}

fn flag(name: &str) -> bool {
    env::var_os(name).is_some_and(|value| !value.is_empty())
}

#[cfg(windows)]
fn legacy_console() -> bool {
    // Rich probes the inherited standard output handle, including for its
    // stderr console. Environment hints also survive redirects to pipes.
    const ENABLE_VIRTUAL_TERMINAL_PROCESSING: u32 = 0x0004;
    !crossterm_winapi::ConsoleMode::new()
        .and_then(|console| console.mode())
        .is_ok_and(|mode| mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING != 0)
}
#[cfg(not(windows))]
fn legacy_console() -> bool {
    false
}

impl Output {
    fn new(stderr: bool) -> Self {
        Self::with_min_width(stderr, 1)
    }

    fn with_min_width(stderr: bool, min_width: usize) -> Self {
        let terminal = if stderr {
            std::io::stderr().is_terminal()
        } else {
            std::io::stdout().is_terminal()
        };
        // Match Rich's legacy Windows pipe fallback and reserve its final column.
        let legacy = legacy_console();
        let columns = env::var("COLUMNS")
            .ok()
            .and_then(|s| s.parse().ok())
            .filter(|w| *w > 0);
        let attached_console = std::io::stdout().is_terminal()
            || std::io::stderr().is_terminal()
            || (!cfg!(windows) && std::io::stdin().is_terminal());
        let dimensions = columns
            .or_else(|| {
                attached_console
                    .then(crossterm::terminal::size)
                    .and_then(Result::ok)
                    .map(|(w, _)| usize::from(w))
                    .filter(|w| *w > 0)
            })
            .unwrap_or(80usize);
        let configured = env::var("TERMINAL_WIDTH")
            .ok()
            .and_then(|s| s.parse().ok())
            .or_else(|| columns.map(|w| w.saturating_sub(usize::from(legacy))));
        let forced = flag("GITHUB_ACTIONS") || flag("FORCE_COLOR") || flag("PY_COLORS");
        // Typer's explicit terminal setting overrides Rich's automatic rules.
        // In automatic mode Rich checks TTY_COMPATIBLE, then FORCE_COLOR
        // (even when it is empty), then the output stream.
        let automatic_terminal = match env::var("TTY_COMPATIBLE").as_deref() {
            Ok("1") => true,
            Ok("0") => false,
            _ => terminal && env::var_os("FORCE_COLOR").is_none(),
        };
        let tty = (forced || automatic_terminal) && !flag("_TYPER_FORCE_DISABLE_TERMINAL");
        let dumb = tty
            && env::var("TERM")
                .is_ok_and(|s| matches!(s.to_ascii_lowercase().as_str(), "dumb" | "unknown"));
        let height = env::var("LINES").ok().and_then(|s| s.parse::<usize>().ok());
        let width = match (configured, height) {
            (Some(w), Some(_)) => w.saturating_sub(usize::from(legacy)),
            _ if dumb => 80,
            (Some(w), _) => w,
            _ => dimensions.saturating_sub(usize::from(legacy)),
        }
        .clamp(min_width, 65535);
        Self {
            usage_width: dimensions.saturating_sub(2).clamp(50, 78),
            context: RenderContext {
                width,
                height: None,
            },
            styled: tty && !dumb && !legacy,
            colors: !flag("NO_COLOR"),
            border: if legacy {
                BorderStyle::Square
            } else {
                BorderStyle::Rounded
            },
            buffer: String::new(),
        }
    }
    fn append(&mut self, item: &impl Renderable) {
        for segment in item.render(&self.context) {
            for span in segment.spans {
                let prefix = if self.styled {
                    sgr(&span.style, self.colors)
                } else {
                    String::new()
                };
                if !prefix.is_empty() && !span.text.is_empty() {
                    self.buffer.push_str("\x1b[");
                    self.buffer.push_str(&prefix);
                    self.buffer.push('m');
                    self.buffer.push_str(&span.text);
                    self.buffer.push_str("\x1b[0m");
                } else {
                    self.buffer.push_str(&span.text);
                }
            }
            if segment.newline {
                self.buffer.push('\n');
            }
        }
    }
    fn panel(
        &self,
        title: &str,
        item: impl Renderable + Send + Sync + 'static,
        style: Style,
    ) -> Panel {
        Panel::renderable(item)
            .title(title)
            .title_align(Alignment::Left)
            .title_style(style)
            .border_style(self.border)
            .style(style)
            .expand(true)
            .padding(1, 0)
    }
    fn render_help(&mut self, page: &Page, explicit: bool) {
        self.append(&Padding::all(self.usage(page, 2), 1).style(Style::new().bold()));
        if let Some(about) = &page.about {
            self.append(&Padding::new(about.text(), PaddingSpec::new(0, 1, 1, 1)));
        }
        for p in &page.panels {
            let mut table = Table::new()
                .show_header(false)
                .show_border(false)
                .show_edge(false)
                .show_row_lines(false)
                .pad_edge(false)
                .padding(1)
                .expand(true);
            for i in 0..p.rows[0].len() {
                let mut col = Column::new("");
                if p.command_width.is_none() && i + 2 == p.rows[0].len() {
                    col = col.overflow(Overflow::Wrap);
                }
                if let Some(w) = p.command_width {
                    col = if i == 0 {
                        col.width(ColumnWidth::Fixed(w))
                            .no_wrap(true)
                            .style(Style::parse("bold cyan"))
                    } else {
                        col.ratio(10)
                    };
                }
                table.add_column(col);
            }
            for row in &p.rows {
                table.add_row_obj(Row::cells(row.iter().map(Copy::cell)));
            }
            self.append(&self.panel(&p.title, table, Style::new().dim()));
        }
        if let Some(epilog) = &page.epilog {
            self.append(&Padding::all(epilog.text(), 1));
        }
        if explicit {
            self.buffer.push('\n');
        }
    }
    fn usage(&self, page: &Page, padding: usize) -> Text {
        let text = page
            .usage_widths
            .iter()
            .rev()
            .find(|(w, _)| *w <= self.usage_width)
            .map(|(_, value)| value)
            .unwrap_or(&page.usage)
            .text();
        // Click indents continuation lines. Rich includes that indentation in
        // the first word when folding it, even on very narrow terminals.
        let width = self.context.width.saturating_sub(padding).max(1);
        let mut result = Text::new();
        let (mut column, mut started, mut first) = (0, false, true);
        for span in text.spans {
            let mut fragment = String::new();
            for ch in span.text.chars() {
                if ch == '\n' {
                    (column, started, first) = (0, false, true);
                } else if first {
                    if ch == ' ' && started {
                        first = false;
                    } else {
                        if column == width {
                            fragment.push('\n');
                            column = 0;
                        }
                        column += 1;
                        started |= ch != ' ';
                    }
                }
                fragment.push(ch);
            }
            result.push_styled(fragment, span.style);
        }
        result
    }
}

fn sgr(style: &Style, colors: bool) -> String {
    let mut bits = Vec::new();
    for (on, code) in [
        (style.bold, "1"),
        (style.dim, "2"),
        (style.italic, "3"),
        (style.underline, "4"),
        (style.reverse, "7"),
    ] {
        if on {
            bits.push(code);
        }
    }
    if colors && let Some(c) = style.foreground {
        let code = match c {
            Color::Black => "30",
            Color::Red => "31",
            Color::Green => "32",
            Color::Yellow => "33",
            Color::Blue => "34",
            Color::Magenta => "35",
            Color::Cyan => "36",
            Color::White => "37",
            _ => "",
        };
        if !code.is_empty() {
            bits.push(code);
        }
    }
    bits.join(";")
}

pub(super) fn parse() -> Result<super::Cli, ExitCode> {
    let mut args: Vec<OsString> = env::args_os().skip(1).collect();
    if args
        .iter()
        .any(|s| s == "--" || (cfg!(windows) && s.as_encoded_bytes().starts_with(b"~")))
    {
        arguments::prepare(&super::Cli::command(), &mut args);
    }
    let mut error = match super::Cli::try_parse_from(
        std::iter::once(std::ffi::OsStr::new("yaga")).chain(args.iter().map(OsString::as_os_str)),
    ) {
        Ok(cli) => return Ok(cli),
        Err(error) => error,
    };
    let mut command = super::Cli::command();
    command.build();
    let mut at = &command;
    let mut names = Vec::new();
    for part in &args {
        let Some(found) = part.to_str().and_then(|name| at.find_subcommand(name)) else {
            break;
        };
        names.push(found.get_name());
        at = found;
    }
    let key = names.join(" ");
    let page = catalog::for_command(PAGES.get(&key), at);
    let remaining = &args[names.len()..];
    let no_args = remaining.is_empty() && at.has_subcommands();
    let eager_args = if names.is_empty()
        && remaining.first().is_some_and(|s| s == "--")
        && remaining.get(1).is_some_and(|s| s == "--show-completion")
    {
        // A rejected print-only request should identify its invalid option.
        // This only inspects the tokens; it never executes the parsed flags.
        Some(&remaining[1..])
    } else if matches!(
        error.kind(),
        ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
    ) {
        Some(remaining)
    } else {
        None
    };
    if let Some(bad) = eager_args.and_then(|tokens| usage_error::unknown_eager_option(at, tokens)) {
        error = clap::Error::new(ErrorKind::UnknownArgument);
        error.insert(
            clap::error::ContextKind::InvalidArg,
            clap::error::ContextValue::String(bad),
        );
    }
    match error.kind() {
        kind if no_args
            || (kind == ErrorKind::DisplayHelp && remaining.iter().any(|s| s == "--help")) =>
        {
            let mut stream = Output::new(false);
            stream.render_help(&page, !no_args);
            print!("{}", stream.buffer);
            Err(ExitCode::from(if no_args { 2 } else { 0 }))
        }
        ErrorKind::DisplayVersion => {
            println!("yaga {}", env!("CARGO_PKG_VERSION"));
            Err(ExitCode::SUCCESS)
        }
        _ => {
            let mut stream = Output::new(true);
            usage_error::render(&mut stream, &page, at, &key, remaining, &error);
            eprint!(
                "{}",
                stream
                    .buffer
                    .replace('\n', if cfg!(windows) { "\r\n" } else { "\n" })
            );
            Err(ExitCode::from(2))
        }
    }
}
