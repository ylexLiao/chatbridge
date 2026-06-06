from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .models import Session
from .parsers import SUPPORTED_SOURCES, count_sessions, find_session, load_sessions
from .paths import path_doctor
from .summary import build_handoff
from .writers import native_import

TARGETS = {"copilot", "codex", "claude"}


def add_api_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    api_cmd = subparsers.add_parser("api", help="Machine-readable JSON API for the Rust TUI.")
    api_sub = api_cmd.add_subparsers(dest="api_command", required=True)

    sessions_cmd = api_sub.add_parser("sessions", help="List source sessions as JSON.")
    sessions_cmd.add_argument("--source", required=True, choices=sorted(SUPPORTED_SOURCES))
    sessions_cmd.add_argument("--limit", type=int, default=100)
    sessions_cmd.add_argument("--project")

    handoff_cmd = api_sub.add_parser("handoff", help="Build a handoff prompt as JSON.")
    handoff_cmd.add_argument("--from", dest="source", required=True, choices=sorted(SUPPORTED_SOURCES))
    handoff_cmd.add_argument("--to", dest="target", required=True, choices=sorted(TARGETS))
    handoff_cmd.add_argument("--session")
    handoff_cmd.add_argument("--project")
    handoff_cmd.add_argument("--level", choices=["brief", "normal", "full"], default="normal")

    native_cmd = api_sub.add_parser("native-import", help="Run native import as JSON.")
    native_cmd.add_argument("--from", dest="source", required=True, choices=sorted(SUPPORTED_SOURCES))
    native_cmd.add_argument("--to", dest="target", required=True, choices=sorted(TARGETS))
    native_cmd.add_argument("--session")
    native_cmd.add_argument("--project")
    native_cmd.add_argument("--level", choices=["brief", "normal", "full"], default="normal")
    native_cmd.add_argument("--allow-duplicate", action="store_true")
    native_cmd.add_argument("--force", action="store_true")
    mode = native_cmd.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")

    paths_cmd = api_sub.add_parser("paths", help="Inspect configured history paths as JSON.")
    paths_sub = paths_cmd.add_subparsers(dest="paths_command", required=True)
    paths_sub.add_parser("doctor")


def handle_api(args: argparse.Namespace, home: Path) -> int:
    try:
        if args.api_command == "sessions":
            limit = max(1, int(args.limit or 1))
            fast_limited = not args.project
            sessions = load_sessions(args.source, home, metadata_only=True, limit=limit if fast_limited else None)
            if args.project:
                sessions = [s for s in sessions if s.project_path == args.project]
            sessions = sorted(sessions, key=lambda s: str(s.updated_at or s.created_at or ""), reverse=True)
            visible = sessions[:limit]
            total = count_sessions(args.source, home, args.project)
            return _write_ok(
                {
                    "sessions": [_session_to_api(session, args.source) for session in visible],
                    "limit": limit,
                    "loaded": len(visible),
                    "total": total,
                    "hasMore": len(visible) < total,
                }
            )
        if args.api_command == "handoff":
            session = find_session(args.source, home, args.session, args.project)
            return _write_ok({"text": build_handoff(session, args.target, args.level)})
        if args.api_command == "native-import":
            session = find_session(args.source, home, args.session, None)
            text = native_import(
                session,
                args.target,
                home,
                apply=bool(args.apply),
                project=args.project,
                level=args.level,
                allow_duplicate=bool(args.allow_duplicate),
                force_running_vscode=bool(args.force),
            )
            return _write_ok({"text": text})
        if args.api_command == "paths" and args.paths_command == "doctor":
            return _write_ok({"text": path_doctor(home)})
    except SystemExit as exc:
        return _write_error(str(exc), _error_kind(str(exc)))
    except Exception as exc:
        return _write_error(str(exc), "error")
    return _write_error("Unsupported api command.", "error")


def _session_to_api(session: Session, source: str) -> dict[str, Any]:
    return {
        "source": session.source,
        "sourceLabel": session.source_label,
        "sessionId": session.session_id,
        "title": session.title,
        "projectPath": session.project_path,
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "rawPath": str(session.raw_path) if session.raw_path else None,
        "scope": _session_scope(session, source),
    }


def _session_scope(session: Session, source: str) -> str:
    if source != "copilot" and session.source != "copilot":
        return ""
    project = session.project_path or ""
    return "REMOTE" if project.startswith("vscode-remote://") else "LOCAL"


def _error_kind(message: str) -> str:
    if "Duplicate native import" in message:
        return "duplicate"
    if "VS Code is currently running" in message:
        return "vscode_running"
    return "error"


def _write_ok(data: dict[str, Any]) -> int:
    return _write_json({"ok": True, "data": data})


def _write_error(message: str, kind: str) -> int:
    payload: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "message": message,
    }
    next_title = _extract_next_title(message)
    if next_title:
        payload["nextTitle"] = next_title
    return _write_json(payload)


def _extract_next_title(message: str) -> str | None:
    match = re.search(r"import another copy as (?P<title>.+?)\.$", message, flags=re.DOTALL)
    if not match:
        return None
    return " ".join(match.group("title").split())


def _write_json(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0
