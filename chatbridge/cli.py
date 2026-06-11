from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .export import load_bundle, write_bundle
from .parsers import SUPPORTED_SOURCES, find_session, load_sessions
from .paths import edit_path_overrides, path_doctor, set_path_overrides
from .summary import build_handoff
from .update import update_release_install
from .util import timestamp_sort_key
from .writers import native_import, repair_claude_imports, repair_codex_imports, repair_copilot_imports
from .api import add_api_parser, handle_api
from .launcher import launch_tui

TARGETS = {"copilot", "codex", "claude"}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return launch_tui(Path.home())
    parser = argparse.ArgumentParser(prog="chatbridge", description="Bridge local AI chat histories between Copilot, Codex, and Claude Code.")
    parser.add_argument("--home", default=str(Path.home()), help="Home directory to read/write histories from. Defaults to current HOME.")
    parser.add_argument("--version", action="version", version=f"chatbridge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    tui_cmd = sub.add_parser("tui", help="Open the interactive TUI.")
    update_cmd = sub.add_parser("update", help="Update a release-installer ChatBridge install.")
    update_cmd.add_argument("--version", default="latest", help="Release tag to install. Default: latest")

    list_cmd = sub.add_parser("list", help="List recovered sessions from one source.")
    list_cmd.add_argument("--source", required=True, choices=sorted(SUPPORTED_SOURCES))
    list_cmd.add_argument("--project")
    list_cmd.add_argument("--limit", type=int, default=20)

    handoff_cmd = sub.add_parser("handoff", help="Print a handoff prompt for a source session.")
    handoff_cmd.add_argument("--from", dest="source", required=True, choices=sorted(SUPPORTED_SOURCES))
    handoff_cmd.add_argument("--to", dest="target", required=True, choices=sorted(TARGETS))
    handoff_cmd.add_argument("--session")
    handoff_cmd.add_argument("--project")
    handoff_cmd.add_argument("--level", choices=["brief", "normal", "full"], default="normal")
    handoff_cmd.add_argument("--last", action="store_true", help="Use the most recent matching session. This is the default when --session is omitted.")

    export_cmd = sub.add_parser("export", help="Export a session to a portable ChatBridge bundle (.json) that any machine can native-import.")
    export_cmd.add_argument("--from", dest="source", required=True, choices=sorted(SUPPORTED_SOURCES))
    export_cmd.add_argument("--session")
    export_cmd.add_argument("--project")
    export_cmd.add_argument("--out", help="Output file or directory. Default: ./chatbridge-export-<source>-<session>.json")

    native_cmd = sub.add_parser("native-import", help="Import a source session summary into a target native history format.")
    native_cmd.add_argument("--from", dest="source", choices=sorted(SUPPORTED_SOURCES), help="Source tool to read the session from. Omit when using --bundle.")
    native_cmd.add_argument("--to", dest="target", required=True, choices=sorted(TARGETS))
    native_cmd.add_argument("--session")
    native_cmd.add_argument("--bundle", help="Import from a ChatBridge export bundle file instead of a local source tool.")
    native_cmd.add_argument("--project", help="Destination project path for the import. Default: the session's own project path.")
    native_cmd.add_argument("--level", choices=["brief", "normal", "full"], default="normal")
    native_cmd.add_argument("--allow-duplicate", action="store_true", help="Import another copy when a matching native import already exists, suffixing the title.")
    native_cmd.add_argument("--force", action="store_true", help="For Copilot imports, write even if VS Code is running and may overwrite the import.")
    mode = native_cmd.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show what would be imported without writing. Default.")
    mode.add_argument("--apply", action="store_true", help="Write the imported synthetic session after creating backups.")

    repair_cmd = sub.add_parser("repair-codex-imports", help="Repair older ChatBridge Codex imports so Codex resume can see them.")
    repair_mode = repair_cmd.add_mutually_exclusive_group()
    repair_mode.add_argument("--dry-run", action="store_true", help="Show imported Codex sessions that would be repaired. Default.")
    repair_mode.add_argument("--apply", action="store_true", help="Repair metadata after creating backups.")

    repair_claude_cmd = sub.add_parser("repair-claude-imports", help="Repair older ChatBridge Claude Code imports so Claude resume can see them.")
    repair_claude_mode = repair_claude_cmd.add_mutually_exclusive_group()
    repair_claude_mode.add_argument("--dry-run", action="store_true", help="Show imported Claude Code sessions that would be repaired. Default.")
    repair_claude_mode.add_argument("--apply", action="store_true", help="Repair metadata after creating backups.")

    repair_copilot_cmd = sub.add_parser("repair-copilot-imports", help="Repair older ChatBridge Copilot imports so VS Code chat history can see them.")
    repair_copilot_mode = repair_copilot_cmd.add_mutually_exclusive_group()
    repair_copilot_mode.add_argument("--dry-run", action="store_true", help="Show imported Copilot sessions that would be repaired. Default.")
    repair_copilot_mode.add_argument("--apply", action="store_true", help="Repair chat session JSONL and VS Code index after creating backups.")
    repair_copilot_cmd.add_argument("--force", action="store_true", help="Write even if VS Code is running and may overwrite the repair.")

    paths_cmd = sub.add_parser("paths", help="Inspect or override local history paths.")
    paths_sub = paths_cmd.add_subparsers(dest="paths_command", required=True)
    paths_sub.add_parser("doctor", help="Show detected paths and cross-platform candidates.")
    paths_edit = paths_sub.add_parser("edit", help="Open ~/.chatbridge/config.json in VISUAL/EDITOR.")
    paths_edit.add_argument("--editor", help="Editor command to use instead of VISUAL/EDITOR.")
    paths_set = paths_sub.add_parser("set", help="Write path overrides to ~/.chatbridge/config.json.")
    paths_set.add_argument("--copilot-workspace-storage")
    paths_set.add_argument("--codex-home")
    paths_set.add_argument("--claude-home")

    add_api_parser(sub)

    args = parser.parse_args(argv)
    home = Path(args.home).expanduser()
    try:
        if args.command == "api":
            return handle_api(args, home)
        if args.command == "tui":
            return launch_tui(home)
        if args.command == "update":
            return update_release_install(args.version)
        if args.command == "list":
            return _list(args, home)
        if args.command == "handoff":
            session = find_session(args.source, home, args.session, args.project)
            sys.stdout.write(build_handoff(session, args.target, args.level))
            return 0
        if args.command == "export":
            session = find_session(args.source, home, args.session, args.project)
            bundle_path = write_bundle(session, Path(args.out).expanduser() if args.out else None)
            print(f"Exported {session.source_label} session {session.session_id} to {bundle_path}")
            return 0
        if args.command == "native-import":
            if args.bundle:
                session = load_bundle(Path(args.bundle))
            elif args.source:
                session = find_session(args.source, home, args.session, None)
            else:
                raise SystemExit("Provide --from SOURCE or --bundle FILE.")
            sys.stdout.write(
                native_import(
                    session,
                    args.target,
                    home,
                    apply=bool(args.apply),
                    project=args.project,
                    level=args.level,
                    allow_duplicate=bool(args.allow_duplicate),
                    force_running_vscode=bool(args.force),
                )
            )
            return 0
        if args.command == "repair-codex-imports":
            sys.stdout.write(repair_codex_imports(home, apply=bool(args.apply)))
            return 0
        if args.command == "repair-claude-imports":
            sys.stdout.write(repair_claude_imports(home, apply=bool(args.apply)))
            return 0
        if args.command == "repair-copilot-imports":
            sys.stdout.write(repair_copilot_imports(home, apply=bool(args.apply), force_running_vscode=bool(args.force)))
            return 0
        if args.command == "paths":
            if args.paths_command == "doctor":
                sys.stdout.write(path_doctor(home))
                return 0
            if args.paths_command == "edit":
                config_path = edit_path_overrides(home, editor=args.editor)
                sys.stdout.write(f"Edited ChatBridge path config: {config_path}\n")
                return 0
            if args.paths_command == "set":
                if not (args.copilot_workspace_storage or args.codex_home or args.claude_home):
                    raise SystemExit("Provide at least one path override.")
                config_path = set_path_overrides(
                    home,
                    copilot_workspace_storage=args.copilot_workspace_storage,
                    codex_home=args.codex_home,
                    claude_home=args.claude_home,
                )
                sys.stdout.write(f"Wrote ChatBridge path config: {config_path}\n")
                return 0
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        if code:
            print(code, file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"chatbridge error: {exc}", file=sys.stderr)
        return 1
    return 2


def _list(args: argparse.Namespace, home: Path) -> int:
    limit = max(1, int(args.limit or 1))
    # With a project filter, load everything first so older projects are not
    # silently hidden by the recency-limited fast path.
    sessions = load_sessions(args.source, home, metadata_only=True, limit=None if args.project else limit)
    if args.project:
        sessions = [s for s in sessions if s.project_path == args.project]
    sessions = sorted(
        sessions,
        key=lambda s: timestamp_sort_key(s.updated_at if s.updated_at not in (None, "") else s.created_at),
        reverse=True,
    )
    for session in sessions[:limit]:
        updated = session.updated_at or session.created_at or ""
        project = session.project_path or ""
        print(f"{session.session_id}\t{session.title}\t{updated}\t{project}")
    return 0
