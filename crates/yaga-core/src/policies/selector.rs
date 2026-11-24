//! Checkbox navigation and frames for interactive policy initialization.

use super::Kind;
use std::{
    collections::{HashMap, HashSet},
    io::{self, Write},
    ops::ControlFlow,
};

#[derive(Clone, Copy)]
pub enum Location {
    Starter,
    Pyproject,
}

pub type Occupied = HashMap<Kind, Location>;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Key {
    Up,
    Down,
    Toggle,
    All,
    Confirm,
    Cancel,
    Noop,
}

pub struct Selector {
    cursor: usize,
    selected: HashSet<Kind>,
    selectable: Vec<Kind>,
}

impl Selector {
    pub fn new(existing: &Occupied) -> Self {
        Self {
            cursor: Kind::ALL
                .iter()
                .position(|kind| !existing.contains_key(kind))
                .unwrap_or(0),
            selected: HashSet::new(),
            selectable: Kind::ALL
                .iter()
                .copied()
                .filter(|kind| !existing.contains_key(kind))
                .collect(),
        }
    }

    pub fn press(&mut self, key: Key, existing: &Occupied) -> ControlFlow<Option<Vec<Kind>>> {
        let kinds = Kind::ALL;
        match key {
            Key::Cancel => return ControlFlow::Break(None),
            Key::Confirm => {
                let selected = kinds
                    .iter()
                    .copied()
                    .filter(|kind| self.selected.contains(kind))
                    .collect();
                return ControlFlow::Break(Some(selected));
            }
            Key::Down => self.cursor = (self.cursor + 1) % kinds.len(),
            Key::Up => self.cursor = (self.cursor + kinds.len() - 1) % kinds.len(),
            Key::Toggle => {
                let kind = kinds[self.cursor];
                if !existing.contains_key(&kind) && !self.selected.remove(&kind) {
                    self.selected.insert(kind);
                }
            }
            Key::All => {
                if !self.selectable.is_empty()
                    && self
                        .selectable
                        .iter()
                        .all(|kind| self.selected.contains(kind))
                {
                    self.selected.clear();
                } else {
                    self.selected.extend(self.selectable.iter().copied());
                }
            }
            Key::Noop => {}
        }
        ControlFlow::Continue(())
    }

    /// Use LF for a text stream. Raw Windows console output uses CRLF.
    pub fn draw(
        &self,
        out: &mut impl Write,
        existing: &Occupied,
        ansi: bool,
        newline: &str,
    ) -> io::Result<()> {
        let mut out = io::BufWriter::new(out);
        let (bold, dim, cyan, green, reset) = if ansi {
            ("\x1b[1m", "\x1b[2m", "\x1b[36m", "\x1b[32m", "\x1b[0m")
        } else {
            ("", "", "", "", "")
        };
        write!(
            out,
            "{bold}Select YAGA policies to initialize{reset} {dim}(Up/Down move, Space toggle, a all, Enter confirm, Esc/q cancel){reset}{newline}"
        )?;
        for (index, kind) in Kind::ALL.iter().enumerate() {
            if index == self.cursor {
                write!(out, " {cyan}>{reset} ")?;
            } else {
                write!(out, "   ")?;
            }
            let label = kind.as_str();
            if let Some(location) = existing.get(kind) {
                let file = match location {
                    Location::Pyproject => "pyproject.toml",
                    Location::Starter => kind.default_path(),
                };
                write!(
                    out,
                    "{dim}[-]{reset} {dim}{label:<13}{reset} {dim}(already exists: {file}){reset}{newline}"
                )?;
            } else {
                if self.selected.contains(kind) {
                    write!(out, "{green}[x]{reset} {bold}{label:<13}{reset} ")?;
                } else {
                    write!(out, "[ ] {label:<13} ")?;
                }
                write!(
                    out,
                    "{dim}{} (v{}) - {}{reset}{newline}",
                    kind.default_path(),
                    kind.current(),
                    kind.description()
                )?;
            }
        }
        write!(
            out,
            " {dim}{} selected{reset}{newline}",
            self.selected.len()
        )?;
        out.flush()
    }
}
