"""Interactive shell checkbox selector for yaga policy init."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from yaga.policies.schemas import POLICY_SPECS, PolicyKind

KEY_UP = "up"
KEY_DOWN = "down"
KEY_SPACE = "space"
KEY_ALL = "all"
KEY_ENTER = "enter"
KEY_CANCEL = "cancel"
KEY_NOOP = "noop"


def is_interactive_terminal(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    """Return True when both input and output are attached to an interactive TTY."""
    in_stream = sys.stdin if stdin is None else stdin
    out_stream = sys.stdout if stdout is None else stdout
    return bool(
        getattr(in_stream, "isatty", lambda: False)()
        and getattr(out_stream, "isatty", lambda: False)()
    )


def select_policies_interactively(
    existing: Mapping[PolicyKind, Path | None],
    *,
    read_key: Callable[[], str] | None = None,
    output: TextIO | None = None,
) -> tuple[PolicyKind, ...] | None:
    """Run an interactive checkbox selector; return selected kinds or None if cancelled."""
    stream = sys.stderr if output is None else output
    use_ansi = _enable_ansi(stream)
    kinds = tuple(POLICY_SPECS.keys())
    selectable = tuple(kind for kind in kinds if existing.get(kind) is None)
    selected: set[PolicyKind] = set()
    cursor = 0
    for index, kind in enumerate(kinds):
        if existing.get(kind) is None:
            cursor = index
            break

    key_reader = _default_key_reader if read_key is None else read_key
    rendered_lines = 0

    if use_ansi:
        stream.write("\x1b[?25l")
        stream.flush()
    try:
        while True:
            frame = _render_frame(
                kinds,
                existing=existing,
                selected=selected,
                cursor=cursor,
                use_ansi=use_ansi,
            )
            if use_ansi and rendered_lines > 0:
                stream.write(f"\x1b[{rendered_lines}A\r\x1b[J")
            stream.write(frame)
            stream.flush()
            rendered_lines = frame.count("\n")

            action = key_reader()
            if action == KEY_UP:
                cursor = (cursor - 1) % len(kinds)
            elif action == KEY_DOWN:
                cursor = (cursor + 1) % len(kinds)
            elif action == KEY_SPACE:
                current = kinds[cursor]
                if existing.get(current) is None:
                    if current in selected:
                        selected.remove(current)
                    else:
                        selected.add(current)
            elif action == KEY_ALL:
                if selectable and selectable_all_checked(selectable, selected):
                    selected.clear()
                else:
                    selected.update(selectable)
            elif action == KEY_ENTER:
                if use_ansi and rendered_lines > 0:
                    stream.write(f"\x1b[{rendered_lines}A\r\x1b[J")
                    stream.flush()
                return tuple(kind for kind in kinds if kind in selected)
            elif action == KEY_CANCEL:
                if use_ansi and rendered_lines > 0:
                    stream.write(f"\x1b[{rendered_lines}A\r\x1b[J")
                    stream.flush()
                return None
    finally:
        if use_ansi:
            stream.write("\x1b[?25h")
            stream.flush()


def selectable_all_checked(
    selectable: tuple[PolicyKind, ...],
    selected: set[PolicyKind],
) -> bool:
    return all(kind in selected for kind in selectable)


def _render_frame(
    kinds: tuple[PolicyKind, ...],
    *,
    existing: Mapping[PolicyKind, Path | None],
    selected: set[PolicyKind],
    cursor: int,
    use_ansi: bool,
) -> str:
    bold = "\x1b[1m" if use_ansi else ""
    dim = "\x1b[2m" if use_ansi else ""
    cyan = "\x1b[36m" if use_ansi else ""
    green = "\x1b[32m" if use_ansi else ""
    reset = "\x1b[0m" if use_ansi else ""

    lines = [
        f"{bold}Select YAGA policies to initialize{reset} "
        f"{dim}(Up/Down move, Space toggle, a all, Enter confirm, Esc/q cancel){reset}"
    ]
    for index, kind in enumerate(kinds):
        spec = POLICY_SPECS[kind]
        pointer = f"{cyan}>{reset}" if index == cursor else " "
        already_exists = existing.get(kind) is not None
        if already_exists:
            checkbox = f"{dim}[-]{reset}"
            status = f"{dim}(already exists: {spec.default_relpath}){reset}"
            label = f"{dim}{kind.value:<13}{reset}"
        elif kind in selected:
            checkbox = f"{green}[x]{reset}"
            status = f"{dim}{spec.default_relpath} (v{spec.current_version_int}) - {spec.description}{reset}"
            label = f"{bold}{kind.value:<13}{reset}"
        else:
            checkbox = "[ ]"
            status = f"{dim}{spec.default_relpath} (v{spec.current_version_int}) - {spec.description}{reset}"
            label = f"{kind.value:<13}"
        lines.append(f" {pointer} {checkbox} {label} {status}")
    lines.append(f" {dim}{len(selected)} selected{reset}")
    return "\n".join(lines) + "\n"


def _enable_ansi(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12 if stream is sys.stderr else -11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        enable_vt = 0x0004
        return not (
            (mode.value & enable_vt) == 0
            and kernel32.SetConsoleMode(handle, mode.value | enable_vt) == 0
        )
    except Exception:
        return False


def _default_key_reader() -> str:
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


def _read_key_windows() -> str:
    if sys.platform != "win32":
        return KEY_CANCEL
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ext = msvcrt.getwch()
        if ext == "H":
            return KEY_UP
        if ext == "P":
            return KEY_DOWN
        return KEY_NOOP
    if ch == "\x1b":
        if msvcrt.kbhit():
            nxt = msvcrt.getwch()
            if nxt == "[" and msvcrt.kbhit():
                code = msvcrt.getwch()
                if code == "A":
                    return KEY_UP
                if code == "B":
                    return KEY_DOWN
            return KEY_NOOP
        return KEY_CANCEL
    return _classify_common_char(ch)


def _read_key_posix() -> str:
    if sys.platform == "win32":
        return KEY_CANCEL
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = os.read(fd, 1).decode("utf-8", errors="ignore")
        if first == "\x1b":
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                seq = os.read(fd, 2).decode("utf-8", errors="ignore")
                if seq == "[A":
                    return KEY_UP
                if seq == "[B":
                    return KEY_DOWN
                return KEY_NOOP
            return KEY_CANCEL
        return _classify_common_char(first)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _classify_common_char(ch: str) -> str:
    if ch in ("\x03", "\x04", "q", "Q"):
        return KEY_CANCEL
    if ch in ("\r", "\n"):
        return KEY_ENTER
    if ch == " ":
        return KEY_SPACE
    if ch in ("k", "K"):
        return KEY_UP
    if ch in ("j", "J"):
        return KEY_DOWN
    if ch in ("a", "A"):
        return KEY_ALL
    return KEY_NOOP
