from __future__ import annotations

import json
import os
import select
import shutil
import sys
import termios
import tty
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .models import Session
from .parsers import find_session, load_sessions
from .paths import path_doctor
from .summary import build_handoff
from .writers import native_import

SOURCES = [
    ("copilot", "GitHub Copilot", "VS Code workspaceStorage", "Best source for old Copilot Chat work."),
    ("codex", "Codex CLI", "~/.codex sessions", "Good for terminal-first coding sessions."),
    ("claude", "Claude Code", "~/.claude projects", "Good for Claude Code project transcripts."),
]
TARGETS = [
    ("codex", "Codex CLI"),
    ("claude", "Claude Code"),
    ("copilot", "GitHub Copilot"),
]
ACTIONS = [
    ("list", "Recent Sessions", "Browse recovered sessions without reading full bodies."),
    ("handoff", "Prompt Handoff", "Print a clean continuation prompt."),
    ("native", "Native Import", "Write a synthetic session; dry-run unless confirmed."),
]
SESSION_FETCH_LIMIT = 100
SESSION_PAGE_SIZE = 15


@dataclass
class TuiState:
    source: str = "copilot"
    action: str = "home"
    page: int = 1
    query: str = ""
    input_mode: str = ""
    filter_buffer: str = ""
    menu_index: int = 0
    selected_index: int = 0
    target_index: int = 0
    sessions: list[Session] = field(default_factory=list)
    selected: Session | None = None
    target: str | None = None
    pending_action: str = ""
    message: str = "Use arrows and Enter, or press L/H/N/P hotkeys."


def run_tui(home: Path) -> int:
    width = _terminal_width()
    state = TuiState()
    _clear_screen()
    print(render_state_screen(state, width=width))
    if os.environ.get("CHATBRIDGE_TUI_SMOKE") == "1":
        return 0
    if not sys.stdin.isatty():
        return 0

    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while True:
            key = _read_key(sys.stdin)
            if not _handle_key(home, state, key):
                return 0
            _clear_screen()
            print(render_state_screen(state, width=width))
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


def render_state_screen(state: TuiState, width: int = 100) -> str:
    width = max(86, min(width, 132))
    lines: list[str] = []
    lines.append(_rule(width, color="dim"))
    tabs = "  " + "  ".join(_chip(label, accent=(key == state.source)) for key, label, _, _ in SOURCES)
    lines.append(_line(_bold(" ChatBridge TUI ") + tabs, _chip("AI History Bridge", accent=False), width))
    lines.append(_line(" Bridge local AI chat history. Source/action/session all stay in this panel.", "", width))
    lines.append(_line(_nav("STATE") + f" Source: {_source_name(state.source)} | View: {_content_title(state)} | Cursor: {_cursor_label(state)}", _state_badge(state), width))
    lines.append(_rule(width, color="dim"))

    content_width = width - 4
    left_width = max(30, min(38, content_width // 3))
    right_width = content_width - left_width - 3
    lines.append(_two_col(_panel_top(left_width, "Menu"), _panel_top(right_width, _content_title(state)), left_width, right_width, width))
    menu = _state_menu(state)
    main = _state_content(state, right_width)
    for left, right in _zip_fill(menu, main):
        lines.append(_two_col(_panel_body(left, left_width), _panel_body(right, right_width), left_width, right_width, width))
    lines.append(_two_col(_panel_bottom(left_width), _panel_bottom(right_width), left_width, right_width, width))
    lines.append(_rule(width, color="dim"))
    lines.append(_line(_nav("NAV") + "  ↑/↓ move  Enter select  ←/→ source/target  q/Esc back", _nav("ACT") + "  L list  H handoff  N import  P paths  / filter", width))
    lines.append(_rule(width, color="dim"))
    return "\n".join(lines)


def render_home_screen(width: int = 100) -> str:
    return render_state_screen(TuiState(), width=width)



def format_session_rows(
    sessions: list[Session],
    source: str | None = None,
    page: int = 1,
    page_size: int = SESSION_PAGE_SIZE,
    width: int = 100,
    selected_index: int | None = None,
) -> str:
    width = max(72, min(width, 140))
    page_size = max(1, page_size)
    total = len(sessions)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = min(start + page_size, total)
    visible = sessions[start:end]
    number_width = max(2, len(str(max(end, 1))))
    marker_width = 1 if selected_index is not None else 0
    show_scope = source == "copilot" or any(session.source == "copilot" for session in visible)
    scope_width = 6 if show_scope else 0
    when_width = 10
    id_width = 10
    project_width = max(18, min(34, width // 3))
    separators = 2 + 2 + 2 + 2
    fixed = marker_width + number_width + when_width + id_width + project_width + separators
    if marker_width:
        fixed += 2
    if show_scope:
        fixed += scope_width + 2
    title_width = max(16, width - fixed)

    lines = [
        _clip(f"Showing {start + 1 if total else 0}-{end} of {total} sessions | Page {page}/{total_pages}", width),
    ]
    header_parts = [_pad("", marker_width) if marker_width else "", _pad("#", number_width), _pad("Scope", scope_width) if show_scope else "", _pad("When", when_width), _pad("Title", title_width), _pad("ID", id_width), _pad("Project", project_width)]
    lines.append(_join_columns(header_parts))
    lines.append("-" * min(width, len(lines[-1])))
    for offset, session in enumerate(visible, start=start + 1):
        parts = [
            _pad(">" if selected_index == offset - 1 else " ", marker_width) if marker_width else "",
            _pad(str(offset), number_width, align="right"),
            _pad(_session_scope(session, source), scope_width) if show_scope else "",
            _pad(_session_time_label(session), when_width),
            _pad(_clip(session.title, title_width), title_width),
            _pad(_short_id(session.session_id), id_width),
            _pad(_clip(_project_label(session.project_path), project_width), project_width),
        ]
        lines.append(_join_columns(parts))
    if total_pages > 1:
        lines.append(_clip("Commands: n next page | p previous page | /text filter | number/id select | q back", width))
    else:
        lines.append(_clip("Commands: number/id select | blank latest | /text filter | q back", width))
    return "\n".join(lines)


def _read_key(stream: object) -> str:
    return _read_key_fd(stream.fileno())


def _read_key_fd(fd: int) -> str:
    data = os.read(fd, 1)
    if not data:
        return ""
    if data == b"\x1b":
        sequence = bytearray(data)
        while True:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                break
            sequence.extend(os.read(fd, 1))
            if len(sequence) == 3 and sequence[1] in {ord("["), ord("O")}:
                break
            if len(sequence) >= 6:
                break
        return _normalize_key(bytes(sequence).decode("utf-8", errors="ignore"))
    return _normalize_key(data.decode("utf-8", errors="ignore"))


def _normalize_key(value: str) -> str:
    mapping = {
        "\x1b[A": "up",
        "\x1b[B": "down",
        "\x1b[C": "right",
        "\x1b[D": "left",
        "\x1bOA": "up",
        "\x1bOB": "down",
        "\x1bOC": "right",
        "\x1bOD": "left",
        "\r": "enter",
        "\n": "enter",
        "\x1b": "escape",
        "\x03": "ctrl-c",
        "\x7f": "backspace",
        "\b": "backspace",
    }
    return mapping.get(value, value.lower() if len(value) == 1 else value)


def _handle_key(home: Path, state: TuiState, key: str) -> bool:
    key = _normalize_key(key)
    if key == "ctrl-c":
        return False
    if state.input_mode == "filter":
        return _handle_filter_key(state, key)
    if key == "escape":
        return _go_back(state)
    if key == "/":
        if state.action in {"list", "handoff", "native"}:
            state.input_mode = "filter"
            state.filter_buffer = state.query
            state.message = f"Filter: {state.filter_buffer}"
        return True
    if state.action == "home":
        return _handle_home_key(home, state, key)
    if state.action in {"list", "handoff", "native"}:
        return _handle_session_key(home, state, key)
    if state.action == "target":
        return _handle_target_key(home, state, key)
    if state.action == "confirm":
        if key in {"y", "enter"}:
            return _handle_confirm_command(home, state, "y")
        if key in {"n", "q"}:
            return _handle_confirm_command(home, state, "n")
        return True
    if state.action in {"result", "paths"}:
        if key in {"q", "escape", "left", "backspace"}:
            return _go_back(state)
        if key in {"1", "2", "3", "l", "h", "n", "p"}:
            return _handle_home_key(home, state, key)
    return True


def _handle_home_key(home: Path, state: TuiState, key: str) -> bool:
    if key == "up":
        state.menu_index = (state.menu_index - 1) % 8
        return True
    if key == "down":
        state.menu_index = (state.menu_index + 1) % 8
        return True
    if key == "left":
        _cycle_source(state, -1)
        return True
    if key == "right":
        _cycle_source(state, 1)
        return True
    if key == "enter":
        return _activate_home_menu(home, state)
    if key in {"1", "2", "3"}:
        state.menu_index = int(key) - 1
        return _activate_home_menu(home, state)
    if key in {"l", "h", "n", "p", "q"}:
        command = {"l": "list", "h": "handoff", "n": "import", "p": "paths", "q": "q"}[key]
        return _handle_command(home, state, command)
    return True


def _activate_home_menu(home: Path, state: TuiState) -> bool:
    if state.menu_index <= 2:
        state.source = SOURCES[state.menu_index][0]
        state.action = "home"
        state.message = f"Source changed to {_source_name(state.source)}."
        return True
    if state.menu_index == 3:
        _load_sessions_into_state(home, state, "list")
        return True
    if state.menu_index == 4:
        _load_sessions_into_state(home, state, "handoff")
        return True
    if state.menu_index == 5:
        _load_sessions_into_state(home, state, "native")
        return True
    if state.menu_index == 6:
        state.action = "paths"
        state.message = path_doctor(home)
        return True
    return False


def _cycle_source(state: TuiState, delta: int) -> None:
    index = next((idx for idx, (key, _, _, _) in enumerate(SOURCES) if key == state.source), 0)
    index = (index + delta) % len(SOURCES)
    state.source = SOURCES[index][0]
    state.menu_index = index
    state.action = "home"
    state.message = f"Source changed to {_source_name(state.source)}."


def _handle_session_key(home: Path, state: TuiState, key: str) -> bool:
    filtered = _filter_sessions(state.sessions, state.query)
    if not filtered:
        if key in {"q", "escape", "left"}:
            return _go_back(state)
        return True
    if key == "up":
        state.selected_index = max(0, state.selected_index - 1)
        _sync_page_to_selection(state, len(filtered))
        return True
    if key == "down":
        state.selected_index = min(len(filtered) - 1, state.selected_index + 1)
        _sync_page_to_selection(state, len(filtered))
        return True
    if key == "left":
        state.page = max(1, state.page - 1)
        state.selected_index = min(state.selected_index, (state.page - 1) * SESSION_PAGE_SIZE)
        return True
    if key == "right":
        total_pages = max(1, (len(filtered) + SESSION_PAGE_SIZE - 1) // SESSION_PAGE_SIZE)
        state.page = min(total_pages, state.page + 1)
        state.selected_index = min(len(filtered) - 1, (state.page - 1) * SESSION_PAGE_SIZE)
        return True
    if key in {"n", "pagedown"}:
        return _handle_session_command(home, state, "next")
    if key in {"p", "pageup"}:
        return _handle_session_command(home, state, "previous")
    if key == "enter":
        return _select_session(home, state, filtered[state.selected_index])
    if key in {"q", "escape"}:
        return _go_back(state)
    if key.isdigit():
        return _handle_session_command(home, state, key)
    return True


def _sync_page_to_selection(state: TuiState, total: int) -> None:
    if total <= 0:
        state.page = 1
        state.selected_index = 0
        return
    state.selected_index = max(0, min(state.selected_index, total - 1))
    state.page = state.selected_index // SESSION_PAGE_SIZE + 1


def _handle_target_key(home: Path, state: TuiState, key: str) -> bool:
    if key in {"up", "left"}:
        state.target_index = (state.target_index - 1) % len(TARGETS)
        return True
    if key in {"down", "right"}:
        state.target_index = (state.target_index + 1) % len(TARGETS)
        return True
    if key == "enter":
        return _handle_target_command(home, state, str(state.target_index + 1))
    if key in {"1", "2", "3"}:
        state.target_index = int(key) - 1
        return _handle_target_command(home, state, key)
    if key in {"q", "escape"}:
        return _go_back(state)
    return True


def _handle_filter_key(state: TuiState, key: str) -> bool:
    if key == "enter":
        state.query = state.filter_buffer.strip().lower()
        state.input_mode = ""
        state.selected_index = 0
        state.page = 1
        state.message = f"Filter set to '{state.query}'." if state.query else "Filter cleared."
        return True
    if key == "escape":
        state.input_mode = ""
        state.filter_buffer = state.query
        state.message = "Filter cancelled."
        return True
    if key == "backspace":
        state.filter_buffer = state.filter_buffer[:-1]
        state.message = f"Filter: {state.filter_buffer}"
        return True
    if len(key) == 1 and key.isprintable():
        state.filter_buffer += key
        state.message = f"Filter: {state.filter_buffer}"
    return True


def _go_back(state: TuiState) -> bool:
    if state.action == "home":
        return False
    state.action = "home"
    state.selected = None
    state.target = None
    state.pending_action = ""
    state.input_mode = ""
    state.message = "Back home. Use arrows and Enter, or L/H/N/P hotkeys."
    return True


def _handle_command(home: Path, state: TuiState, value: str) -> bool:
    command = value.strip()
    lowered = command.lower()
    if lowered in {"q", "quit", "exit"}:
        if state.action == "home":
            return False
        state.action = "home"
        state.selected = None
        state.target = None
        state.message = "Back home. Use arrows and Enter, or L/H/N/P hotkeys."
        return True
    if lowered in {"1", "2", "3"}:
        state.source = SOURCES[int(lowered) - 1][0]
        state.action = "home"
        state.page = 1
        state.query = ""
        state.sessions = []
        state.selected = None
        state.message = f"Source changed to {_source_name(state.source)}."
        return True
    if lowered in {"l", "list"}:
        _load_sessions_into_state(home, state, "list")
        return True
    if lowered in {"h", "handoff"}:
        _load_sessions_into_state(home, state, "handoff")
        return True
    if lowered in {"i", "import"} or (lowered == "n" and state.action == "home"):
        _load_sessions_into_state(home, state, "native")
        return True
    if lowered in {"paths", "path", "p"} and state.action == "home":
        state.menu_index = 6
        state.action = "paths"
        state.message = path_doctor(home)
        return True
    if state.action in {"list", "handoff", "native"}:
        return _handle_session_command(home, state, command)
    if state.action == "target":
        return _handle_target_command(home, state, command)
    if state.action == "confirm":
        return _handle_confirm_command(home, state, command)
    state.message = "Unknown command. Use 1-3, L, H, N, P, or q."
    return True


def _handle_session_command(home: Path, state: TuiState, command: str) -> bool:
    lowered = command.lower()
    filtered = _filter_sessions(state.sessions, state.query)
    total_pages = max(1, (len(filtered) + SESSION_PAGE_SIZE - 1) // SESSION_PAGE_SIZE)
    if lowered in {"n", "next"}:
        state.page = min(state.page + 1, total_pages)
        return True
    if lowered in {"p", "prev", "previous"}:
        state.page = max(state.page - 1, 1)
        return True
    if command.startswith("/"):
        state.query = command[1:].strip().lower()
        state.page = 1
        state.message = f"Filter set to '{state.query}'." if state.query else "Filter cleared."
        return True
    if not command:
        if not filtered:
            state.message = "No session to select."
            return True
        session = filtered[0]
    elif command.isdigit() and 1 <= int(command) <= len(filtered):
        session = filtered[int(command) - 1]
    else:
        try:
            session = find_session(state.source, home, command, None)
        except SystemExit as exc:
            state.message = str(exc)
            return True
    return _select_session(home, state, session)


def _select_session(home: Path, state: TuiState, session: Session) -> bool:
    state.selected = session
    if state.action == "list":
        state.message = _session_detail(session)
        return True
    state.pending_action = state.action
    state.action = "target"
    state.target = None
    state.target_index = 0
    state.message = "Select target: 1 Codex, 2 Claude Code, 3 GitHub Copilot."
    return True


def _handle_target_command(home: Path, state: TuiState, command: str) -> bool:
    if state.selected is None:
        state.action = "home"
        state.message = "No selected session."
        return True
    if command not in {"1", "2", "3"}:
        state.message = "Select target: 1 Codex, 2 Claude Code, 3 GitHub Copilot."
        return True
    target = TARGETS[int(command) - 1][0]
    state.target = target
    if state.pending_action == "native":
        state.action = "confirm"
        state.message = "Apply native import? y = write with backup, anything else = dry-run."
        return True
    try:
        state.message = build_handoff(state.selected, target, level="normal")
        state.action = "result"
    except SystemExit as exc:
        state.message = str(exc)
        state.action = "result"
    return True


def _handle_confirm_command(home: Path, state: TuiState, command: str) -> bool:
    if state.selected is None or state.target is None:
        state.action = "home"
        state.message = "No selected session or target."
        return True
    apply = command.strip().lower() in {"y", "yes"}
    try:
        state.message = native_import(state.selected, state.target, home, apply=apply, project=None, level="full")
    except SystemExit as exc:
        state.message = str(exc)
    state.action = "result"
    return True


def _load_sessions_into_state(home: Path, state: TuiState, action: str) -> None:
    state.action = action
    state.menu_index = _menu_index_for_action(action, fallback=state.menu_index)
    state.page = 1
    state.query = ""
    state.input_mode = ""
    state.filter_buffer = ""
    state.selected_index = 0
    state.selected = None
    state.target = None
    state.sessions = _recent_sessions(home, state.source)
    state.pending_action = action
    label = {"list": "Recent Sessions", "handoff": "Prompt Handoff", "native": "Native Import"}[action]
    state.message = f"Action: {label}. Select a session by number/id, use n/p to page, /text to filter."


def _content_title(state: TuiState) -> str:
    return {
        "home": "Import Console",
        "list": "Recent Sessions",
        "handoff": "Prompt Handoff",
        "native": "Native Import",
        "target": "Target",
        "confirm": "Confirm",
        "result": "Result",
        "paths": "Path Doctor",
    }.get(state.action, "Import Console")


def _cursor_label(state: TuiState) -> str:
    if state.input_mode == "filter":
        return f"Filter: {state.filter_buffer or '-'}"
    if state.action == "home":
        return _home_menu_label(state.menu_index)
    if state.action in {"list", "handoff", "native"}:
        session = _selected_visible_session(state)
        return session.title if session else "No session"
    if state.action == "target":
        return TARGETS[state.target_index][1]
    if state.action == "confirm":
        return "Apply native import"
    if state.action == "paths":
        return "Detected paths"
    if state.action == "result":
        return "Result"
    return _content_title(state)


def _state_badge(state: TuiState) -> str:
    if state.input_mode == "filter":
        return "typing filter"
    if state.action in {"list", "handoff", "native"}:
        total = len(_filter_sessions(state.sessions, state.query))
        return f"{total} sessions"
    if state.action == "target":
        return "choose target"
    if state.action == "confirm":
        return "dry-run by default"
    return "ready"


def _home_menu_label(index: int) -> str:
    if 0 <= index < len(SOURCES):
        return SOURCES[index][1]
    actions = {
        3: "Recent Sessions",
        4: "Prompt Handoff",
        5: "Native Import",
        6: "Path Doctor",
        7: "Back / Quit",
    }
    return actions.get(index, "Menu")


def _state_menu(state: TuiState) -> list[str]:
    rows: list[str] = []
    active_index = _active_menu_index(state)
    for index, (_key, name, location, _) in enumerate(SOURCES, start=1):
        text = f"{index}. {name:<16} {location}"
        rows.append(_selected(text) if active_index == index - 1 else text)
    rows.append("")
    for offset, text in enumerate(["L. Recent Sessions", "H. Prompt Handoff", "N. Native Import", "P. Path Doctor", "Q. Back / Quit"], start=3):
        rows.append(_selected(text) if active_index == offset else text)
    return rows


def _active_menu_index(state: TuiState) -> int:
    if state.action == "home":
        return state.menu_index
    if state.action == "paths":
        return 6
    if state.action in {"target", "confirm", "result"} and state.pending_action:
        return _menu_index_for_action(state.pending_action, fallback=state.menu_index)
    return _menu_index_for_action(state.action, fallback=state.menu_index)


def _menu_index_for_action(action: str, fallback: int = 0) -> int:
    return {
        "list": 3,
        "handoff": 4,
        "native": 5,
        "paths": 6,
    }.get(action, fallback)


def _state_content(state: TuiState, width: int) -> list[str]:
    if state.action == "home":
        return _home_preview_lines(state)
    if state.action in {"list", "handoff", "native"}:
        filtered = _filter_sessions(state.sessions, state.query)
        rows = format_session_rows(
            filtered,
            source=state.source,
            page=state.page,
            page_size=SESSION_PAGE_SIZE,
            width=max(72, width - 4),
            selected_index=state.selected_index,
        ).splitlines()
        if state.input_mode == "filter":
            rows.append(f"Filter: {state.filter_buffer}")
        elif state.query:
            rows.append(f"Active filter: {state.query}")
        rows.extend(_session_selection_lines(state, filtered))
        return rows
    if state.action == "target":
        selected = state.selected.title if state.selected else "-"
        target_lines = [
            _selected(f"{index}. {label}") if state.target_index == index - 1 else f"{index}. {label}"
            for index, (_, label) in enumerate(TARGETS, start=1)
        ]
        return [
            f"Selected: {selected}",
            "",
            *target_lines,
            "",
            state.message,
        ]
    if state.action == "confirm":
        selected = state.selected.title if state.selected else "-"
        target = state.target or "-"
        return [
            f"Selected: {selected}",
            f"Target: {target}",
            "",
            state.message,
            "",
            "Type y to apply. Any other input shows dry-run.",
        ]
    if state.action in {"result", "paths"}:
        return _wrap_lines(state.message, max(40, width - 4))[:18]
    return [state.message]


def _home_preview_lines(state: TuiState) -> list[str]:
    highlighted = _home_menu_label(state.menu_index)
    if 0 <= state.menu_index < len(SOURCES):
        key, name, location, description = SOURCES[state.menu_index]
        return [
            _bold("Highlighted Source"),
            f"Highlighted: {name}",
            f"Location: {location}",
            f"Current: {'yes' if key == state.source else 'no'}",
            "",
            f"Enter: switch source to {name}",
            f"Left/Right: cycle source tabs",
            "",
            description,
            "Copilot sessions show LOCAL/REMOTE scope in the browser.",
            "",
            state.message,
        ]
    previews = {
        3: [
            _bold("Browse"),
            "Highlighted: Recent Sessions",
            "Enter: browse recovered sessions",
            f"Loads up to {SESSION_FETCH_LIMIT} records, {SESSION_PAGE_SIZE} per page.",
            "",
            "Next: select a session to inspect title, project, raw file, and time.",
        ],
        4: [
            _bold("Prompt Handoff"),
            "Highlighted: Prompt Handoff",
            "Enter: choose source session, then target tool",
            "",
            "Output: a clean continuation prompt you can paste into another agent.",
            "Good when native history import is risky or unsupported.",
        ],
        5: [
            _bold("Native Import"),
            "Highlighted: Native Import",
            "Enter: choose session, target, then dry-run/apply",
            "",
            "Default: dry-run. Apply writes backup-protected native history.",
            "Duplicate imports are detected before writing.",
        ],
        6: [
            _bold("Path Doctor"),
            "Highlighted: Path Doctor",
            "Enter: inspect detected history paths",
            "",
            "Use this when Copilot/Codex/Claude histories are not discovered.",
            "CLI path overrides are available with `chatbridge paths set`.",
        ],
        7: [
            _bold("Exit"),
            "Highlighted: Back / Quit",
            "Enter: quit ChatBridge",
            "",
            "Esc/q backs out of sub-views; on Home it exits.",
        ],
    }
    return [*previews.get(state.menu_index, [f"Highlighted: {highlighted}"]), "", state.message]


def _session_selection_lines(state: TuiState, filtered: list[Session]) -> list[str]:
    session = _selected_visible_session(state, filtered)
    if session is None:
        return [
            "",
            "Selected: -",
            "Next: no session available",
        ]
    return [
        "",
        f"Selected: {session.title}",
        f"Project: {_project_label(session.project_path)}",
        f"Next: {_session_next_action(state.action)}",
    ]


def _selected_visible_session(state: TuiState, filtered: list[Session] | None = None) -> Session | None:
    visible = _filter_sessions(state.sessions, state.query) if filtered is None else filtered
    if not visible:
        return None
    index = max(0, min(state.selected_index, len(visible) - 1))
    return visible[index]


def _session_next_action(action: str) -> str:
    return {
        "list": "Enter opens session details",
        "handoff": "Enter chooses this session for Prompt Handoff",
        "native": "Enter chooses this session for Native Import",
    }.get(action, "Enter chooses this session")


def _source_name(source: str) -> str:
    for key, name, _, _ in SOURCES:
        if key == source:
            return name
    return source


def _session_detail(session: Session) -> str:
    lines = [
        f"Title: {session.title}",
        f"ID: {session.session_id}",
        f"Project: {session.project_path or '-'}",
        f"Updated: {session.updated_at or session.created_at or '-'}",
        f"Raw: {session.raw_path or '-'}",
    ]
    return "\n".join(lines)


def _wrap_lines(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for raw in str(text).splitlines():
        line = raw
        while len(line) > width:
            lines.append(line[: width - 3] + "...")
            line = line[width - 3 :]
        lines.append(line)
    return lines or [""]

def _terminal_width() -> int:
    return shutil.get_terminal_size((100, 30)).columns


def _clear_screen() -> None:
    if sys.stdout.isatty() and os.environ.get("CHATBRIDGE_NO_CLEAR") != "1":
        print("\033[2J\033[H", end="")


def _rule(width: int, color: str | None = None) -> str:
    line = "+" + "-" * (width - 2) + "+"
    return _color(line, color) if color else line


def _line(left: str, right: str, width: int) -> str:
    body_width = width - 4
    left_plain = _strip_ansi(left)
    right_plain = _strip_ansi(right)
    if right:
        gap = max(1, body_width - len(left_plain) - len(right_plain))
        body = left + " " * gap + right
    else:
        body = left
    plain_len = len(_strip_ansi(body))
    if plain_len > body_width:
        body = _clip_ansi(body, body_width)
        plain_len = len(_strip_ansi(body))
    return "| " + body + " " * max(0, body_width - plain_len) + " |"


def _section(title: str, width: int) -> str:
    return _line(f"-- {title} ", "", width)


def _content(text: str, width: int) -> str:
    return _line(text, "", width)


def _card(title: str, subtitle: str, desc: str, width: int) -> list[str]:
    body_width = width - 8
    lines = [
        _line(f"  + {title}", subtitle, width),
        _line(f"    {desc}", "", width),
    ]
    if body_width < 80:
        return lines
    return lines


def _two_col(left: str, right: str, left_width: int, right_width: int, width: int) -> str:
    left = _clip_ansi(left, left_width)
    right = _clip_ansi(right, right_width)
    left_pad = left + " " * max(0, left_width - len(_strip_ansi(left)))
    right_pad = right + " " * max(0, right_width - len(_strip_ansi(right)))
    return _line(left_pad + " | " + right_pad, "", width)


def _panel_top(width: int, title: str) -> str:
    label = f" {title} "
    if len(label) + 2 >= width:
        return "+" + "-" * (width - 2) + "+"
    return "+" + label + "-" * (width - len(label) - 2) + "+"


def _panel_body(text: str, width: int) -> str:
    inner = width - 4
    body = _clip_ansi(text, inner)
    return "| " + body + " " * max(0, inner - len(_strip_ansi(body))) + " |"


def _panel_bottom(width: int) -> str:
    return "+" + "-" * (width - 2) + "+"


def _zip_fill(left: list[str], right: list[str]) -> list[tuple[str, str]]:
    count = max(len(left), len(right))
    return [(left[i] if i < len(left) else "", right[i] if i < len(right) else "") for i in range(count)]


def _section_title(text: str) -> str:
    return _color(_bold(text), "accent")


def _selected(text: str) -> str:
    return _color("> " + text, "selected")


def _chip(text: str, accent: bool = True) -> str:
    return _color(f" {text} ", "chip" if accent else "selected")


def _nav(text: str) -> str:
    return _color(f" {text} ", "selected")


def _bold(text: str) -> str:
    return _color(text, "bold")


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("CHATBRIDGE_NO_COLOR") != "1"


def _color(text: str, style: str | None) -> str:
    if not style or not _use_color():
        return text
    styles = {
        "bold": "1",
        "dim": "38;5;62",
        "accent": "38;5;159;1",
        "chip": "38;5;235;48;5;159;1",
        "selected": "38;5;235;48;5;159;1",
    }
    code = styles.get(style)
    if not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _clip_ansi(text: str, width: int) -> str:
    if len(_strip_ansi(text)) <= width:
        return text
    return _clip(_strip_ansi(text), width)


def _clip(text: str, width: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= width:
        return clean
    if width <= 3:
        return clean[:width]
    return clean[: width - 3] + "..."


def _pad(text: str, width: int, align: str = "left") -> str:
    if width <= 0:
        return ""
    value = _clip(text, width)
    if align == "right":
        return value.rjust(width)
    return value.ljust(width)


def _join_columns(parts: list[str]) -> str:
    return "  ".join(part for part in parts if part != "")


def _short_id(value: str) -> str:
    if len(value) <= 10:
        return value
    return value[:8] + ".."


def _session_scope(session: Session, source: str | None) -> str:
    if source != "copilot" and session.source != "copilot":
        return ""
    project = session.project_path or ""
    return "REMOTE" if project.startswith("vscode-remote://") else "LOCAL"


def _session_time_label(session: Session) -> str:
    value = session.updated_at or session.created_at
    if value in (None, ""):
        return "unknown"
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            seconds = number / 1000 if number > 100000000000 else number
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return _clip(str(value), 10)


def _project_label(project_path: str | None) -> str:
    if not project_path:
        return "-"
    if not project_path.startswith("vscode-remote://"):
        return project_path
    parsed = urlsplit(project_path)
    authority = unquote(parsed.netloc)
    kind = "remote"
    host = authority
    if "+" in authority:
        raw_kind, raw_host = authority.split("+", 1)
        kind = raw_kind.replace("-remote", "")
        host = _decode_remote_host(raw_host) or raw_host
    path = parsed.path or ""
    return f"{kind}:{host}{path}"


def _decode_remote_host(value: str) -> str | None:
    try:
        data = json.loads(bytes.fromhex(value).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        host = data.get("hostName") or data.get("host")
        if isinstance(host, str) and host:
            return host
    return None


def _recent_sessions(home: Path, source: str) -> list[Session]:
    sessions = load_sessions(source, home, metadata_only=True, limit=SESSION_FETCH_LIMIT)
    return sorted(sessions, key=lambda s: str(s.updated_at or s.created_at or ""), reverse=True)[:SESSION_FETCH_LIMIT]


def _filter_sessions(sessions: list[Session], query: str) -> list[Session]:
    if not query:
        return sessions
    result = []
    for session in sessions:
        haystack = " ".join(
            [
                session.title,
                session.session_id,
                session.project_path or "",
                _project_label(session.project_path),
            ]
        ).lower()
        if query in haystack:
            result.append(session)
    return result
