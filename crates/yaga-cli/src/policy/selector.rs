use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind, KeyModifiers},
    terminal,
};
use std::{
    collections::HashMap,
    io::{self, IsTerminal, Write},
    ops::ControlFlow,
    path::PathBuf,
};
use yaga_core::policies::{
    Kind,
    selector::{Key, Location, Occupied, Selector},
};

struct Cursor(bool);
impl Drop for Cursor {
    fn drop(&mut self) {
        if self.0 {
            let _ = write!(io::stderr(), "\x1b[?25h");
        }
    }
}
struct Raw;
impl Drop for Raw {
    fn drop(&mut self) {
        let _ = terminal::disable_raw_mode();
    }
}

fn key() -> io::Result<event::KeyEvent> {
    terminal::enable_raw_mode()?;
    let _raw = Raw;
    loop {
        if let Event::Key(k) = event::read()?
            && k.kind != KeyEventKind::Release
        {
            return Ok(k);
        }
    }
}

#[cfg(windows)]
fn enable_stderr_vt() -> bool {
    use std::os::windows::io::{AsHandle, IntoRawHandle};
    let Ok(handle) = io::stderr().as_handle().try_clone_to_owned() else {
        return false;
    };
    // Transfer a duplicate of the console handle. Dropping it must leave stderr open.
    let handle = unsafe { crossterm_winapi::Handle::from_raw(handle.into_raw_handle().cast()) };
    let console = crossterm_winapi::ConsoleMode::from(handle);
    const ENABLE_VIRTUAL_TERMINAL_PROCESSING: u32 = 0x0004;
    console.mode().is_ok_and(|mode| {
        mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING != 0
            || console
                .set_mode(mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
                .is_ok()
    })
}

fn action(pressed: event::KeyEvent) -> Key {
    let control = pressed.modifiers.contains(KeyModifiers::CONTROL);
    let plain = (pressed.modifiers - KeyModifiers::SHIFT).is_empty();
    match pressed.code {
        KeyCode::Esc | KeyCode::Char('q' | 'Q') if plain => Key::Cancel,
        KeyCode::Char('d' | 'c') if control => Key::Cancel,
        KeyCode::Char('j' | 'm') if control => Key::Confirm,
        KeyCode::Enter => Key::Confirm,
        KeyCode::Down | KeyCode::Char('j' | 'J') if plain => Key::Down,
        KeyCode::Up | KeyCode::Char('k' | 'K') if plain => Key::Up,
        KeyCode::Char(' ') if plain => Key::Toggle,
        KeyCode::Char('a' | 'A') if plain => Key::All,
        _ => Key::Noop,
    }
}

pub(super) fn select(existing: &HashMap<Kind, PathBuf>) -> io::Result<Option<Vec<Kind>>> {
    let mut out = io::stderr().lock();
    let ansi = out.is_terminal()
        && std::env::var_os("NO_COLOR").is_none()
        && std::env::var_os("TERM").is_none_or(|s| s != "dumb");
    #[cfg(windows)]
    let ansi = ansi && enable_stderr_vt();
    let _cursor = Cursor(ansi);
    if ansi {
        write!(out, "\x1b[?25l")?;
    }
    let occupied: Occupied = existing
        .iter()
        .map(|(&kind, path)| {
            let location = if path.ends_with("pyproject.toml") {
                Location::Pyproject
            } else {
                Location::Starter
            };
            (kind, location)
        })
        .collect();
    let mut selector = Selector::new(&occupied);
    let newline = if cfg!(windows) { "\r\n" } else { "\n" };
    let erase = if ansi {
        format!("\x1b[{}A\r\x1b[J", Kind::ALL.len() + 2)
    } else {
        String::new()
    };
    loop {
        selector.draw(&mut out, &occupied, ansi, newline)?;
        let pressed = key()?;
        write!(out, "{erase}")?;
        if let ControlFlow::Break(result) = selector.press(action(pressed), &occupied) {
            return Ok(result);
        }
    }
}
