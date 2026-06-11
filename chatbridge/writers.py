from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .models import Session
from .paths import resolve_paths
from .summary import build_handoff
from .util import append_jsonl, backup_paths, iter_jsonl, now_iso, parse_timestamp, project_to_claude_slug, read_json, redact, sanitize_embedded_images, text_from_any, write_jsonl

IMPORT_MARKER = "Imported by ChatBridge. Treat this as context, not verified fact."
LEGACY_IMPORT_MARKER = "Imported by ChatNBridge. Treat this as context, not verified fact."
CODEX_EXTERNAL_IMPORT_MARKER = "<EXTERNAL SESSION IMPORTED>"
COPILOT_CHAT_INDEX_KEY = "chat.ChatSessionStore.index"
COPILOT_AGENT_CACHE_KEY = "agentSessions.model.cache"
COPILOT_AGENT_STATE_KEY = "agentSessions.state.cache"


class NativeImportError(SystemExit):
    """Structured native-import failure consumed by the JSON api layer."""

    kind = "error"


class DuplicateImportError(NativeImportError):
    kind = "duplicate"

    def __init__(self, message: str, next_title: str) -> None:
        super().__init__(message)
        self.next_title = next_title


class VSCodeRunningError(NativeImportError):
    kind = "vscode_running"


@dataclass(frozen=True)
class ProjectedMessage:
    role: str
    text: str
    timestamp: Any = None


def native_import(
    session: Session,
    target: str,
    home: Path,
    apply: bool,
    project: str | None = None,
    level: str = "normal",
    allow_duplicate: bool = False,
    force_running_vscode: bool = False,
) -> str:
    handoff = build_handoff(session, target, level=level)
    base_title = f"[Imported from {session.source_label}] {session.title}"
    title = base_title
    project_path = _native_import_target_project(session, target, home, project)
    if not apply:
        mode = "structured transcript" if _project_session_messages(session) else "handoff fallback"
        return (
            f"DRY RUN: would import {session.source_label} session {session.session_id} into {target} using {mode}.\n"
            f"Target project: {project_path}\n\n{handoff}"
        )
    existing_titles = _native_import_existing_titles(target, home, project_path, base_title)
    if existing_titles:
        next_title = _next_native_import_title(base_title, existing_titles)
        if not allow_duplicate:
            raise DuplicateImportError(
                f"Duplicate native import detected for {base_title} into {target} project {project_path}.\n"
                f"Use --allow-duplicate to import another copy as {next_title}.",
                next_title,
            )
        title = next_title
    if target == "codex":
        return _insert_target_project_line(_write_codex(session, home, title, handoff, project_path), project_path)
    if target == "claude":
        return _insert_target_project_line(_write_claude(session, home, title, handoff, project_path), project_path)
    if target == "copilot":
        return _insert_target_project_line(
            _write_copilot(session, home, title, handoff, project_path, force_running_vscode=force_running_vscode),
            project_path,
        )
    raise SystemExit(f"Unsupported native import target: {target}")


def _insert_target_project_line(text: str, project_path: str) -> str:
    head, sep, tail = text.partition("\n")
    if not sep:
        return f"{text}\nTarget project: {project_path}\n"
    return f"{head}{sep}Target project: {project_path}\n{tail}"


def _native_import_target_project(session: Session, target: str, home: Path, project: str | None) -> str:
    if project:
        return project
    session_project = str(session.project_path or "").strip()
    if session_project:
        if target in {"codex", "claude"}:
            local = _vscode_remote_to_local_path(session_project)
            if local:
                return local
        return session_project
    return _current_directory_project(home) or str(home)


def _vscode_remote_to_local_path(value: str) -> str | None:
    """Map vscode-remote://<authority>/<path> to the filesystem path component.

    Used when importing into filesystem-rooted tools (Codex/Claude): on the
    machine the remote URI points at, the trailing path is the local project.
    """
    if not value.startswith("vscode-remote://"):
        return None
    rest = value[len("vscode-remote://") :]
    slash = rest.find("/")
    if slash == -1:
        return None
    path = unquote(rest[slash:])
    return path or None


def _current_project_candidates(home: Path) -> list[str]:
    candidates: list[Path] = []
    explicit = os.environ.get("CHATBRIDGE_PROJECT")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd())
    pwd = os.environ.get("PWD")
    if pwd:
        candidates.append(Path(pwd).expanduser())

    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        for candidate in _path_with_parents(path, home):
            text = str(candidate)
            if text not in seen:
                seen.add(text)
                result.append(text)
    return result


def _path_with_parents(path: Path, home: Path) -> list[Path]:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    roots = [expanded.absolute()]
    try:
        resolved = expanded.resolve()
    except OSError:
        resolved = expanded.absolute()
    if resolved != roots[0]:
        roots.append(resolved)

    try:
        home_resolved = home.expanduser().resolve()
    except OSError:
        home_resolved = home.expanduser().absolute()

    paths: list[Path] = []
    seen: set[str] = set()
    for current in roots:
        for candidate in [current, *current.parents]:
            text = str(candidate)
            if text not in seen:
                seen.add(text)
                paths.append(candidate)
            if candidate == home_resolved or candidate.parent == candidate:
                break
    return paths


def _current_directory_project(home: Path) -> str | None:
    candidates = _current_project_candidates(home)
    return candidates[0] if candidates else None


def _native_import_existing_titles(target: str, home: Path, project_path: str, base_title: str) -> list[str]:
    if target == "codex":
        return _codex_existing_import_titles(resolve_paths(home).codex_home, project_path, base_title)
    if target == "claude":
        return _claude_existing_import_titles(resolve_paths(home).claude_home, project_path, base_title)
    if target == "copilot":
        return _copilot_existing_import_titles(home, project_path, base_title)
    return []


def _next_native_import_title(base_title: str, existing_titles: list[str]) -> str:
    used = {
        index
        for title in existing_titles
        for index in [_native_import_copy_index(title, base_title)]
        if index is not None
    }
    if 0 not in used:
        return base_title
    index = 1
    while index in used:
        index += 1
    return f"{base_title} ({index})"


def _native_import_copy_index(title: str, base_title: str) -> int | None:
    prompt_title = _codex_import_prompt_title(str(title)).strip()
    if prompt_title == base_title:
        return 0
    match = re.fullmatch(re.escape(base_title) + r" \((\d+)\)", prompt_title)
    if not match:
        return None
    return int(match.group(1))


def _append_matching_import_title(titles: list[str], title: Any, base_title: str) -> None:
    if not isinstance(title, str) or _native_import_copy_index(title, base_title) is None:
        return
    if title not in titles:
        titles.append(title)


def _codex_existing_import_titles(root: Path, project_path: str, base_title: str) -> list[str]:
    titles: list[str] = []
    db_path = root / "state_5.sqlite"
    db_checked = False
    if db_path.exists():
        try:
            con = sqlite3.connect(db_path, timeout=10)
            table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
            if table:
                db_checked = True
                rows = con.execute(
                    """
                    SELECT title
                    FROM threads
                    WHERE cwd = ?
                      AND instr(title, '[Imported from ') = 1
                    """,
                    (project_path,),
                ).fetchall()
                for row in rows:
                    _append_matching_import_title(titles, row[0], base_title)
            con.close()
        except sqlite3.Error:
            pass
    if not db_checked:
        for row in iter_jsonl(root / "session_index.jsonl"):
            if isinstance(row, dict):
                _append_matching_import_title(titles, row.get("thread_name"), base_title)
    return titles


def _claude_existing_import_titles(root: Path, project_path: str, base_title: str) -> list[str]:
    titles: list[str] = []
    for row in iter_jsonl(root / "history.jsonl"):
        if isinstance(row, dict) and row.get("project") == project_path:
            _append_matching_import_title(titles, row.get("display"), base_title)
    project_dir = root / "projects" / project_to_claude_slug(project_path)
    if project_dir.exists():
        for path in project_dir.glob("*.jsonl"):
            for row in iter_jsonl(path):
                if isinstance(row, dict) and row.get("type") == "last-prompt":
                    _append_matching_import_title(titles, row.get("lastPrompt"), base_title)
    return titles


def _copilot_existing_import_titles(home: Path, project_path: str, base_title: str) -> list[str]:
    workspace = _find_copilot_workspace(resolve_paths(home).copilot_workspace_storage, project_path)
    if workspace is None:
        return []
    titles: list[str] = []
    chat_dir = workspace / "chatSessions"
    if not chat_dir.exists():
        return titles
    for path in chat_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            _append_matching_import_title(titles, payload.get("customTitle"), base_title)
    return titles


def repair_codex_imports(home: Path, apply: bool) -> str:
    root = resolve_paths(home).codex_home
    db_path = root / "state_5.sqlite"
    if not db_path.exists():
        return "No Codex state database found.\n"
    rows = _find_codex_imports_to_repair(root)
    if not rows:
        return "No Codex imports need repair.\n"
    lines = [f"{row['id']} | {row['desired_title']} | {row['cwd']}" for row in rows]
    if not apply:
        return "DRY RUN: would repair Codex imported sessions:\n" + "\n".join(lines) + "\n"

    rollout_paths = [Path(str(row["rollout_path"])) for row in rows if row["rollout_path"]]
    backup = backup_paths(home, [db_path, root / "state_5.sqlite-wal", root / "state_5.sqlite-shm", *rollout_paths])
    defaults = _codex_native_defaults(root)
    now = int(time.time())
    now_ms = now * 1000
    con = sqlite3.connect(db_path, timeout=10)
    try:
        for row in rows:
            old_id = str(row["id"])
            new_id = old_id if _is_codex_v7_id(old_id) else _codex_session_id()
            row["new_id"] = new_id
            old_title = str(row.get("title") or "")
            title = str(row["desired_title"])
            prompt = _codex_import_user_prompt(_codex_import_prompt_title(title))
            rollout = _repair_codex_rollout_file(root, row, defaults)
            con.execute(
                """
                UPDATE threads
                SET id = ?, rollout_path = ?, updated_at = ?, updated_at_ms = ?, source = ?, thread_source = ?,
                    model_provider = ?, model = ?, cli_version = ?, has_user_event = 0, archived = 0,
                    sandbox_policy = ?, approval_mode = ?, memory_mode = ?, reasoning_effort = ?,
                    title = ?, first_user_message = ?, preview = ?
                WHERE id = ?
                """,
                (
                    new_id,
                    str(rollout),
                    now,
                    now_ms,
                    defaults["source"],
                    defaults["thread_source"],
                    defaults["model_provider"],
                    defaults["model"],
                    defaults["cli_version"],
                    defaults["sandbox_policy"],
                    defaults["approval_mode"],
                    defaults["memory_mode"],
                    defaults["reasoning_effort"],
                    title,
                    prompt[:2000],
                    title[:500],
                    old_id,
                ),
            )
            if new_id != old_id or title != old_title or str(row.get("index_title") or title) != title:
                append_jsonl(root / "session_index.jsonl", {"id": new_id, "thread_name": title, "updated_at": now_iso()})
            if new_id != old_id:
                append_jsonl(root / "history.jsonl", {"session_id": new_id, "ts": now, "text": prompt})
        con.commit()
    finally:
        con.close()
    return f"Repaired {len(rows)} Codex imported session(s).\nBackup: {backup}\n"


def repair_claude_imports(home: Path, apply: bool) -> str:
    root = resolve_paths(home).claude_home
    history_path = root / "history.jsonl"
    rows = _find_claude_imports_to_repair(root)
    if not rows:
        return "No Claude Code imports need repair.\n"
    lines = [f"{row['old_session_id']} -> {row['new_session_id']} | {row['title']} | {row['project_path']}" for row in rows]
    if not apply:
        return "DRY RUN: would repair Claude Code imported sessions:\n" + "\n".join(lines) + "\n"

    backup = backup_paths(home, [history_path, *[Path(str(row["old_path"])).parent for row in rows if row.get("old_path")]])
    history_rows = [row for row in iter_jsonl(history_path)]
    repaired_by_old_id = {str(row["old_session_id"]): row for row in rows}
    now_ms = int(time.time() * 1000)
    for item in history_rows:
        if not isinstance(item, dict):
            continue
        repair = repaired_by_old_id.get(str(item.get("sessionId") or ""))
        if not repair:
            continue
        item["sessionId"] = repair["new_session_id"]
        item["project"] = repair["project_path"]
        item["timestamp"] = now_ms
    write_jsonl(history_path, history_rows)

    for row in rows:
        _repair_claude_session_file(row)
    return f"Repaired {len(rows)} Claude Code imported session(s).\nBackup: {backup}\n"


def _repair_claude_session_file(row: dict[str, Any]) -> None:
    target = Path(str(row["target_path"]))
    old_path = Path(str(row["old_path"])) if row.get("old_path") else None
    new_sid = str(row["new_session_id"])
    title = str(row["title"])
    project_path = str(row["project_path"])
    preserved = _claude_preserved_message_rows(old_path)
    if preserved:
        _write_claude_repaired_rows(target, new_sid, title, project_path, preserved)
    else:
        _write_claude_session_rows(target, new_sid, title, str(row["handoff"]), project_path)
    if old_path and old_path.exists() and old_path.resolve() != target.resolve():
        old_path.unlink()


def _claude_preserved_message_rows(path: Path | None) -> list[dict[str, Any]]:
    """Collect original user/assistant rows so repair keeps the full transcript."""
    if not path or not path.exists():
        return []
    preserved: list[dict[str, Any]] = []
    for row in iter_jsonl(path):
        if not isinstance(row, dict) or row.get("type") not in {"user", "assistant"}:
            continue
        message = row.get("message")
        if not message or not isinstance(message, (dict, str)):
            continue
        preserved.append(dict(row))
    return preserved


def _write_claude_repaired_rows(target: Path, sid: str, title: str, project_path: str, message_rows: list[dict[str, Any]]) -> None:
    version = _claude_native_version(target.parent.parent)
    timestamp = now_iso()
    rows: list[dict[str, Any]] = []
    parent_uuid: str | None = None
    leaf_uuid: str | None = None
    for original in message_rows:
        row = dict(original)
        row["sessionId"] = sid
        row["cwd"] = project_path
        row.setdefault("isSidechain", False)
        row.setdefault("userType", "external")
        row.setdefault("entrypoint", "cli")
        row.setdefault("gitBranch", "HEAD")
        row.setdefault("version", version)
        row.setdefault("timestamp", timestamp)
        row_uuid = str(row.get("uuid") or uuid.uuid4())
        row["uuid"] = row_uuid
        row["parentUuid"] = parent_uuid
        message = row.get("message")
        if isinstance(message, str):
            if row.get("type") == "user":
                row["message"] = {"role": "user", "content": message}
            else:
                row["message"] = _claude_message_payload("assistant", message, row_uuid)
        rows.append(row)
        parent_uuid = row_uuid
        leaf_uuid = row_uuid
    rows.extend([
        {"type": "last-prompt", "lastPrompt": title, "leafUuid": leaf_uuid, "sessionId": sid},
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "permission-mode", "permissionMode": "default", "sessionId": sid},
    ])
    write_jsonl(target, rows)


def _project_session_messages(session: Session) -> list[ProjectedMessage]:
    projected: list[ProjectedMessage] = []
    seen: set[tuple[str, str]] = set()
    for message in session.messages:
        role = _project_role(message.role)
        if role is None:
            continue
        text = sanitize_embedded_images(redact(str(message.text or ""))).strip()
        if not text:
            continue
        key = (role, text)
        if key in seen:
            continue
        seen.add(key)
        projected.append(ProjectedMessage(role=role, text=text, timestamp=message.timestamp))

    for artifact in session.artifacts:
        text = sanitize_embedded_images(redact(str(artifact.text or ""))).strip()
        if not text:
            continue
        label = artifact.kind
        if artifact.path:
            label = f"{label} ({artifact.path})"
        artifact_text = f"[Recovered artifact: {label}]\n{text}"
        key = ("user", artifact_text)
        if key in seen:
            continue
        seen.add(key)
        projected.append(ProjectedMessage(role="user", text=artifact_text, timestamp=session.updated_at))
    return projected


def _project_role(role: Any) -> str | None:
    clean = str(role or "").lower().replace("-", "_")
    if clean in {"user", "human", "input", "user_message", "request"}:
        return "user"
    if clean in {"assistant", "agent", "agent_message", "assistant_message", "response", "message"}:
        return "assistant"
    return None


def _source_timestamp_iso(value: Any, fallback: str) -> str:
    dt = _source_datetime(value)
    if not dt:
        return fallback
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_timestamp_ms(value: Any, fallback: int) -> int:
    dt = _source_datetime(value)
    if not dt:
        return fallback
    return int(dt.timestamp() * 1000)


def _source_timestamp_seconds(value: Any) -> int | None:
    dt = _source_datetime(value)
    if not dt:
        return None
    return int(dt.timestamp())


def _approx_token_count(messages: list[ProjectedMessage]) -> int:
    total = sum(len(message.text.encode("utf-8")) for message in messages)
    return max(1, total // 4) if total else 0


def _write_codex(session: Session, home: Path, title: str, handoff: str, project_path: str) -> str:
    root = resolve_paths(home).codex_home
    backup = backup_paths(home, [root / "session_index.jsonl", root / "history.jsonl", root / "state_5.sqlite", root / "state_5.sqlite-wal", root / "state_5.sqlite-shm"])
    defaults = _codex_native_defaults(root)
    sid = _codex_session_id()
    timestamp = now_iso()
    cwd = project_path
    display_title = _codex_import_display_title(session, title)
    prompt = _codex_import_user_prompt(title)
    assistant_context = _codex_import_assistant_context(handoff)
    transcript = [
        *_project_session_messages(session),
        ProjectedMessage(role="user", text=prompt, timestamp=session.updated_at or session.created_at),
        ProjectedMessage(role="assistant", text=assistant_context, timestamp=session.updated_at or session.created_at),
    ]
    rollout = _codex_rollout_path(root, sid)
    rows = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": sid,
                "timestamp": timestamp,
                "cwd": cwd,
                "originator": "codex-tui",
                "source": defaults["source"],
                "thread_source": defaults["thread_source"],
                "cli_version": defaults["cli_version"],
                "instructions": None,
                "model_provider": defaults["model_provider"],
                "model": defaults["model"],
                "reasoning_effort": defaults["reasoning_effort"],
            },
        },
    ]
    rows.extend(_codex_import_visible_preview_rows(session, timestamp))
    rows.extend(_codex_transcript_rows(transcript, timestamp))
    rows.extend([
        {
            "timestamp": timestamp,
            "type": "turn_context",
            "payload": {
                "cwd": cwd,
                "approval_policy": defaults["approval_mode"],
                "sandbox_policy": _codex_turn_context_sandbox_policy(defaults["sandbox_policy"]),
                "model": defaults["model"],
                "effort": defaults["reasoning_effort"],
                "summary": display_title,
            },
        },
    ])
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    append_jsonl(root / "session_index.jsonl", {"id": sid, "thread_name": display_title, "updated_at": timestamp})
    append_jsonl(root / "history.jsonl", {"session_id": sid, "ts": int(time.time()), "text": prompt})
    _upsert_codex_thread(root, sid, display_title, rollout, cwd, prompt, defaults)
    return f"Imported into Codex session {sid}\nBackup: {backup}\nSession file: {rollout}\n"


def _codex_transcript_rows(messages: list[ProjectedMessage], fallback_timestamp: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    turn_id: str | None = None
    last_agent_message: str | None = None
    user_turn_count = 0

    for message in messages:
        timestamp = _source_timestamp_iso(message.timestamp, fallback_timestamp)
        if message.role == "user":
            if turn_id:
                rows.append(_codex_turn_complete_row(fallback_timestamp, turn_id, last_agent_message, None))
            user_turn_count += 1
            turn_id = f"chatbridge-import-turn-{user_turn_count}"
            last_agent_message = None
            rows.append(_codex_turn_started_row(timestamp, turn_id, message.timestamp))
            rows.extend(_codex_message_rows(message, timestamp))
            continue

        if not turn_id:
            user_turn_count += 1
            turn_id = f"chatbridge-import-turn-{user_turn_count}"
            rows.append(_codex_turn_started_row(timestamp, turn_id, message.timestamp))
        rows.extend(_codex_message_rows(message, timestamp))
        last_agent_message = message.text

    if turn_id:
        rows.append(
            {
                "timestamp": fallback_timestamp,
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": CODEX_EXTERNAL_IMPORT_MARKER, "phase": None, "memory_citation": None},
            }
        )
        rows.append(_codex_token_count_row(fallback_timestamp, messages))
        rows.append(_codex_turn_complete_row(fallback_timestamp, turn_id, last_agent_message, messages[-1].timestamp if messages else None))
    return rows


def _codex_message_rows(message: ProjectedMessage, timestamp: str) -> list[dict[str, Any]]:
    if message.role == "user":
        return [
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message.text}],
                },
            },
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": message.text, "images": [], "local_images": [], "text_elements": []},
            },
        ]
    return [
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": message.text}],
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": message.text, "phase": "final_answer", "memory_citation": None},
        },
    ]


def _codex_turn_started_row(timestamp: str, turn_id: str, source_timestamp: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "task_started", "turn_id": turn_id, "trace_id": None, "model_context_window": None}
    started_at = _source_timestamp_seconds(source_timestamp)
    if started_at is not None:
        payload["started_at"] = started_at
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _codex_turn_complete_row(timestamp: str, turn_id: str, last_agent_message: str | None, source_timestamp: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "task_complete",
        "turn_id": turn_id,
        "last_agent_message": last_agent_message,
        "duration_ms": None,
        "time_to_first_token_ms": None,
    }
    completed_at = _source_timestamp_seconds(source_timestamp)
    if completed_at is not None:
        payload["completed_at"] = completed_at
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _codex_token_count_row(timestamp: str, messages: list[ProjectedMessage]) -> dict[str, Any]:
    total = _approx_token_count(messages)
    usage = {"total_tokens": total}
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage, "last_token_usage": usage, "model_context_window": None},
            "rate_limits": None,
        },
    }


def _codex_native_defaults(root: Path) -> dict[str, str]:
    defaults = {
        "source": "cli",
        "thread_source": "user",
        "model_provider": "codex",
        "cli_version": "0.137.0",
        "model": "gpt-5.5",
        "sandbox_policy": "workspace-write",
        "approval_mode": "on-request",
        "memory_mode": "enabled",
        "reasoning_effort": "medium",
    }
    db_path = root / "state_5.sqlite"
    if not db_path.exists():
        return defaults
    try:
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
        if not table:
            con.close()
            return defaults
        row = con.execute(
            """
            SELECT source, thread_source, model_provider, cli_version, model, sandbox_policy,
                   approval_mode, memory_mode, reasoning_effort
            FROM threads
            WHERE model_provider = 'codex'
              AND model IS NOT NULL AND model <> ''
              AND IFNULL(reasoning_effort, '') <> ''
              AND rollout_path NOT LIKE '%-imported-%'
              AND rollout_path NOT LIKE '%/sessions/imported/%'
              AND instr(title, '[Imported from ') = 0
            ORDER BY CASE WHEN source = 'cli' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return defaults
    if not row:
        return defaults
    for key in defaults:
        value = row[key]
        if value not in (None, ""):
            defaults[key] = str(value)
    defaults["source"] = "cli"
    defaults["thread_source"] = "user"
    defaults["model_provider"] = "codex"
    return defaults


def _codex_rollout_path(root: Path, sid: str, when: int | None = None) -> Path:
    dt = datetime.fromtimestamp(when) if when else datetime.now()
    folder = root / "sessions" / dt.strftime("%Y") / dt.strftime("%m") / dt.strftime("%d")
    return folder / f"rollout-{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"


def _codex_session_id() -> str:
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def _is_codex_v7_id(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 7
    except ValueError:
        return False


def _codex_import_user_prompt(title: str) -> str:
    return title


def _codex_import_display_title(session: Session, title: str) -> str:
    started = _format_source_start_time(session.created_at)
    if not started:
        return title
    return f"{title} · {session.source_label} started {started}"


def _codex_import_prompt_title(title: str) -> str:
    for source_label in ("Copilot", "Claude Code", "Codex"):
        marker = f" · {source_label} started "
        if marker in title:
            return title.split(marker, 1)[0]
    return title


def _format_source_start_time(value: Any) -> str:
    dt = _source_datetime(value)
    if not dt:
        return ""
    local = dt.astimezone()
    zone = local.strftime("%Z") or _format_utc_offset(local)
    return f"{local.strftime('%Y-%m-%d %H:%M')} {zone}".rstrip()


# Shared timestamp parsing lives in util.parse_timestamp; keep the local name
# used throughout this module.
_source_datetime = parse_timestamp


def _format_utc_offset(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return ""
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _codex_import_visible_preview_rows(session: Session, timestamp: str) -> list[dict[str, Any]]:
    last_query, last_reply = _codex_last_exchange(session)
    rows: list[dict[str, Any]] = []
    if last_query:
        query = f"[{session.source_label} last query]\n{_compact_codex_visible_text(last_query, 700)}"
        rows.extend([
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": query}]},
            },
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "user_message", "message": query, "images": [], "local_images": [], "text_elements": []},
            },
        ])
    if last_reply:
        reply = f"[{session.source_label} last reply]\n{_compact_codex_visible_text(last_reply, 900)}"
        rows.extend([
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": reply}]},
            },
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": reply, "phase": "final_answer", "memory_citation": None},
            },
        ])
    return rows


def _codex_last_exchange(session: Session) -> tuple[str, str]:
    last_query = ""
    last_reply = ""
    for message in session.messages:
        role = str(message.role).lower()
        if role == "user":
            last_query = message.text
            last_reply = ""
        elif role in {"assistant", "agent"} and last_query:
            last_reply = message.text
    return last_query, last_reply


def _compact_codex_visible_text(text: str, limit: int) -> str:
    clean = " ".join(redact(str(text)).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _codex_import_assistant_context(handoff: str) -> str:
    clean = handoff.replace(IMPORT_MARKER, "").replace(LEGACY_IMPORT_MARKER, "").rstrip()
    return f"{clean}\n\n{IMPORT_MARKER}"


def _codex_turn_context_sandbox_policy(value: str) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict) and parsed.get("type") == "managed":
        return {"type": "workspace-write", "network_access": False, "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}
    return parsed


def _upsert_codex_thread(root: Path, sid: str, title: str, rollout: Path, cwd: str, first_user_message: str, defaults: dict[str, str]) -> None:
    db_path = root / "state_5.sqlite"
    if not db_path.exists():
        return
    created = int(time.time())
    created_ms = int(created * 1000)
    preview = title[:500]
    try:
        con = sqlite3.connect(db_path, timeout=10)
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
        if not table:
            con.close()
            return
        available = {str(row[1]) for row in con.execute("PRAGMA table_info(threads)").fetchall()}
        values: dict[str, Any] = {
            "id": sid,
            "rollout_path": str(rollout),
            "created_at": created,
            "updated_at": created,
            "source": defaults["source"],
            "model_provider": defaults["model_provider"],
            "cwd": cwd,
            "title": title,
            "sandbox_policy": defaults["sandbox_policy"],
            "approval_mode": defaults["approval_mode"],
            "tokens_used": 0,
            "has_user_event": 0,
            "archived": 0,
            "cli_version": defaults["cli_version"],
            "first_user_message": first_user_message[:2000],
            "memory_mode": defaults["memory_mode"],
            "model": defaults["model"],
            "reasoning_effort": defaults["reasoning_effort"],
            "created_at_ms": created_ms,
            "updated_at_ms": created_ms,
            "thread_source": defaults["thread_source"],
            "preview": preview,
        }
        columns = [column for column in values if column in available]
        if not columns:
            con.close()
            return
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        con.execute(
            f"INSERT OR REPLACE INTO threads ({column_sql}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )
        con.commit()
        con.close()
    except sqlite3.Error:
        return


def _find_codex_imports_to_repair(root: Path) -> list[dict[str, Any]]:
    db_path = root / "state_5.sqlite"
    index_titles = _codex_import_index_titles(root)
    raw_index_titles = _codex_import_index_titles(root, localize=False)
    try:
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
        if not table:
            con.close()
            return []
        params: list[Any] = []
        id_clause = ""
        if index_titles:
            placeholders = ",".join("?" for _ in index_titles)
            id_clause = f"id IN ({placeholders}) OR"
            params.extend(index_titles.keys())
        rows = con.execute(
            f"""
            SELECT id, title, rollout_path, cwd, created_at, updated_at, first_user_message,
                   source, thread_source, model_provider, has_user_event, model, reasoning_effort, sandbox_policy
            FROM threads
            WHERE {id_clause}
                  instr(title, '[Imported from ') = 1
                  OR rollout_path LIKE '%/sessions/imported/%'
                  OR rollout_path LIKE '%-imported-%'
            ORDER BY updated_at DESC
            """,
            params,
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []

    repairs: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        desired_title = index_titles.get(str(item["id"]))
        if not desired_title and str(item.get("title") or "").startswith("[Imported from "):
            desired_title = str(item["title"])
        if not desired_title:
            continue
        first_user_message = str(item.get("first_user_message") or "")
        item["desired_title"] = desired_title
        item["index_title"] = raw_index_titles.get(str(item["id"]))
        needs_repair = (
            item.get("source") != "cli"
            or item.get("thread_source") != "user"
            or item.get("model_provider") != "codex"
            or int(item.get("has_user_event") or 0) != 0
            or not item.get("model")
            or not item.get("reasoning_effort")
            or str(item.get("sandbox_policy") or "").strip() in {"", '{"type":"read-only"}'}
            or not _is_codex_v7_id(str(item["id"]))
            or _is_codex_imported_folder(root, Path(str(item["rollout_path"])))
            or "-imported-" in Path(str(item["rollout_path"])).name
            or _codex_rollout_has_duplicate_marker(Path(str(item["rollout_path"])))
            or str(item.get("title") or "") != desired_title
            or str(item.get("index_title") or desired_title) != desired_title
            or not first_user_message.startswith(_codex_import_prompt_title(desired_title))
        )
        if needs_repair:
            repairs.append(item)
    return repairs


def _codex_import_index_titles(root: Path, localize: bool = True) -> dict[str, str]:
    titles: dict[str, str] = {}
    for row in iter_jsonl(root / "session_index.jsonl"):
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        title = row.get("thread_name")
        if isinstance(sid, str) and isinstance(title, str) and title.startswith("[Imported from "):
            titles[sid] = _localize_import_title_timezone(title) if localize else title
    return titles


def _localize_import_title_timezone(title: str) -> str:
    marker = " started "
    if marker not in title or not title.endswith(" UTC"):
        return title
    prefix, started = title.rsplit(marker, 1)
    try:
        dt = datetime.strptime(started, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return title
    return f"{prefix}{marker}{_format_source_start_time(dt.isoformat())}"


def _codex_rollout_has_duplicate_marker(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text.count(IMPORT_MARKER) + text.count(LEGACY_IMPORT_MARKER) > 1
    except OSError:
        return False


def _repair_codex_rollout_file(root: Path, row: dict[str, Any], defaults: dict[str, str]) -> Path:
    old_id = str(row["id"])
    new_id = str(row.get("new_id") or old_id)
    old_path = Path(str(row["rollout_path"]))
    target = old_path
    if old_path.exists() and (_is_codex_imported_folder(root, old_path) or "-imported-" in old_path.name or new_id != old_id):
        target = _codex_rollout_path(root, new_id, int(row["created_at"] or time.time()))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_path, target)
    if target.exists():
        _rewrite_codex_rollout_metadata(target, defaults, str(row["cwd"]), str(row["desired_title"]), sid=new_id)
    return target


def _is_codex_imported_folder(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to((root / "sessions" / "imported").resolve())
        return True
    except ValueError:
        return False


def _rewrite_codex_rollout_metadata(path: Path, defaults: dict[str, str], cwd: str, title: str, sid: str | None = None) -> None:
    rows = [row for row in iter_jsonl(path)]
    handoff = _extract_codex_handoff(rows)
    prompt = _codex_import_user_prompt(_codex_import_prompt_title(title))
    assistant_context = _codex_import_assistant_context(handoff) if handoff else "Imported handoff context. Verify workspace state before continuing."
    assistant_context_payload = _codex_assistant_context_payload(rows)
    user_rewritten = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload")
        if row.get("type") == "session_meta" and isinstance(payload, dict):
            if sid:
                payload["id"] = sid
            payload["cwd"] = cwd
            payload["originator"] = "codex-tui"
            payload["source"] = defaults["source"]
            payload["thread_source"] = defaults["thread_source"]
            payload["model_provider"] = defaults["model_provider"]
            payload["cli_version"] = defaults["cli_version"]
            payload["model"] = defaults["model"]
            payload["reasoning_effort"] = defaults["reasoning_effort"]
        elif row.get("type") == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
            role = payload.get("role")
            if role == "user" and not user_rewritten:
                payload["content"] = [{"type": "input_text", "text": prompt}]
                user_rewritten = True
            elif role == "assistant" and payload is assistant_context_payload:
                payload["content"] = [{"type": "output_text", "text": assistant_context}]
            elif role == "assistant":
                _strip_codex_import_marker(payload)
        elif row.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
            payload["message"] = prompt
            payload.setdefault("images", [])
        elif row.get("type") == "event_msg" and isinstance(payload, dict):
            _strip_codex_import_marker(payload)
        elif row.get("type") == "turn_context" and isinstance(payload, dict):
            payload["cwd"] = cwd
            payload["approval_policy"] = defaults["approval_mode"]
            payload["sandbox_policy"] = _codex_turn_context_sandbox_policy(defaults["sandbox_policy"])
            payload["model"] = defaults["model"]
            payload["effort"] = defaults["reasoning_effort"]
            payload["summary"] = title
    if rows:
        write_jsonl(path, rows)


def _codex_assistant_context_payload(rows: list[Any]) -> dict[str, Any] | None:
    fallback: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload")
        if not (isinstance(payload, dict) and row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant"):
            continue
        fallback = payload
        text = text_from_any(payload.get("content") or payload.get("message") or payload.get("text"))
        if "[Handoff:" in text:
            fallback = payload
    return fallback


def _strip_codex_import_marker(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(IMPORT_MARKER, "").replace(LEGACY_IMPORT_MARKER, "").strip()
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _strip_codex_import_marker(item)
    elif isinstance(value, dict):
        for key, item in list(value.items()):
            value[key] = _strip_codex_import_marker(item)
    return value


def _extract_codex_handoff(rows: list[Any]) -> str:
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        text = ""
        if isinstance(payload, dict):
            text = text_from_any(payload.get("content") or payload.get("message") or payload.get("text"))
        if "[Handoff:" in text:
            return text
    return ""


def _write_claude(session: Session, home: Path, title: str, handoff: str, project_path: str) -> str:
    root = resolve_paths(home).claude_home
    slug = project_to_claude_slug(project_path)
    project_dir = root / "projects" / slug
    backup = backup_paths(home, [root / "history.jsonl", project_dir])
    sid = str(uuid.uuid4())
    path = project_dir / f"{sid}.jsonl"
    display_title = _codex_import_display_title(session, title)
    _write_claude_session_rows(path, sid, display_title, handoff, project_path, session=session)
    append_jsonl(root / "history.jsonl", {"display": display_title, "pastedContents": {}, "timestamp": int(time.time() * 1000), "project": project_path, "sessionId": sid})
    return f"Imported into Claude Code session {sid}\nBackup: {backup}\nSession file: {path}\n"


def _write_claude_session_rows(path: Path, sid: str, title: str, handoff: str, project_path: str, session: Session | None = None) -> None:
    timestamp = now_iso()
    base = {
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": project_path,
        "sessionId": sid,
        "version": _claude_native_version(path.parent.parent),
        "gitBranch": "HEAD",
    }
    projected = _project_session_messages(session) if session else []
    if not projected:
        projected = [
            ProjectedMessage(role="user", text=handoff, timestamp=None),
            ProjectedMessage(
                role="assistant",
                text="Imported handoff context. Verify workspace state before continuing.",
                timestamp=None,
            ),
        ]
    rows: list[dict[str, Any]] = []
    parent_uuid: str | None = None
    leaf_uuid: str | None = None
    for message in projected:
        row_uuid = str(uuid.uuid4())
        row_timestamp = _source_timestamp_iso(message.timestamp, timestamp)
        row = {
            **base,
            "parentUuid": parent_uuid,
            "type": message.role,
            "uuid": row_uuid,
            "timestamp": row_timestamp,
            "message": _claude_message_payload(message.role, message.text, row_uuid),
        }
        rows.append(row)
        parent_uuid = row_uuid
        leaf_uuid = row_uuid
    rows.extend([
        {"type": "last-prompt", "lastPrompt": title, "leafUuid": leaf_uuid, "sessionId": sid},
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "permission-mode", "permissionMode": "default", "sessionId": sid},
    ])
    write_jsonl(path, rows)


def _claude_message_payload(role: str, text: str, row_uuid: str) -> dict[str, Any]:
    if role == "user":
        return {"role": "user", "content": text}
    return {
        "id": row_uuid,
        "type": "message",
        "role": "assistant",
        "model": "chatbridge-import",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {},
    }


def _claude_native_version(root: Path) -> str:
    projects = root / "projects" if root.name == ".claude" else root
    if not projects.exists():
        return "2.1.0"
    candidates = sorted(projects.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    for candidate in candidates[:50]:
        if "subagents" in candidate.parts:
            continue
        for row in iter_jsonl(candidate):
            if isinstance(row, dict) and row.get("version"):
                return str(row["version"])
    return "2.1.0"


def _find_claude_imports_to_repair(root: Path) -> list[dict[str, Any]]:
    history_path = root / "history.jsonl"
    if not history_path.exists():
        return []
    repairs: list[dict[str, Any]] = []
    for row in iter_jsonl(history_path):
        if not isinstance(row, dict):
            continue
        title = str(row.get("display") or "")
        old_sid = str(row.get("sessionId") or "")
        project_path = str(row.get("project") or "")
        if not title.startswith("[Imported from ") or not old_sid or not project_path:
            continue
        target_dir = root / "projects" / project_to_claude_slug(project_path)
        old_path = _find_claude_session_file(root, old_sid)
        native_ok = old_path is not None and old_path.parent == target_dir and _is_uuid(old_sid) and _claude_session_native_shaped(old_path, old_sid)
        title_ok = native_ok and _claude_session_last_prompt(old_path, old_sid) == title
        if native_ok and title_ok:
            continue
        new_sid = old_sid if _is_uuid(old_sid) else str(uuid.uuid4())
        handoff = _extract_claude_handoff(old_path) if old_path else title
        repairs.append(
            {
                "old_session_id": old_sid,
                "new_session_id": new_sid,
                "title": title,
                "project_path": project_path,
                "old_path": old_path,
                "target_path": target_dir / f"{new_sid}.jsonl",
                "handoff": handoff,
            }
        )
    return repairs


def _find_claude_session_file(root: Path, sid: str) -> Path | None:
    projects = root / "projects"
    if not projects.exists():
        return None
    for path in projects.rglob(f"{sid}.jsonl"):
        if "subagents" not in path.parts:
            return path
    return None


def _claude_session_native_shaped(path: Path, sid: str) -> bool:
    for row in iter_jsonl(path):
        if not isinstance(row, dict) or row.get("type") not in {"user", "assistant"}:
            continue
        message = row.get("message")
        if row.get("sessionId") != sid or not row.get("uuid") or "parentUuid" not in row:
            return False
        if not isinstance(message, dict) or not message.get("role"):
            return False
    return True


def _claude_session_last_prompt(path: Path | None, sid: str) -> str:
    if not path:
        return ""
    for row in iter_jsonl(path):
        if isinstance(row, dict) and row.get("type") == "last-prompt" and row.get("sessionId") == sid:
            return str(row.get("lastPrompt") or "")
    return ""


def _extract_claude_handoff(path: Path | None) -> str:
    if not path:
        return ""
    for row in iter_jsonl(path):
        if not isinstance(row, dict):
            continue
        text = text_from_any(row.get("message") or row.get("content") or row.get("text"))
        if "[Handoff:" in text:
            return text
    for row in iter_jsonl(path):
        if isinstance(row, dict):
            text = text_from_any(row.get("message") or row.get("content") or row.get("text"))
            if text:
                return text
    return ""


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _write_copilot(
    session: Session,
    home: Path,
    title: str,
    handoff: str,
    project_path: str,
    *,
    force_running_vscode: bool = False,
) -> str:
    storage = resolve_paths(home).copilot_workspace_storage
    _guard_copilot_write_when_vscode_running(home, storage, force=force_running_vscode)
    workspace = _find_or_create_copilot_workspace(storage, project_path)
    chat_dir = workspace / "chatSessions"
    backup = backup_paths(home, [chat_dir, workspace / "state.vscdb", workspace / "state.vscdb.backup"])
    sid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    requests = _copilot_projected_requests(session, handoff, now_ms)
    created_ms = _source_timestamp_ms(session.created_at, now_ms)
    updated_ms = _source_timestamp_ms(session.updated_at, requests[-1]["timestamp"] if requests else now_ms)
    payload = {
        "version": 3,
        "sessionId": sid,
        "customTitle": title,
        "creationDate": created_ms,
        "lastMessageDate": updated_ms,
        "initialLocation": "panel",
        "isImported": True,
        "requesterUsername": "chatbridge",
        "responderUsername": "GitHub Copilot",
        "requests": requests,
    }
    json_path, jsonl_path = _write_copilot_session_files(chat_dir, sid, payload)
    _upsert_copilot_chat_index(workspace, sid, title, created_ms, updated_ms)
    _upsert_copilot_agent_session_cache(workspace, sid, title, created_ms, updated_ms, project_path)
    warning = _vscode_running_warning(home, storage)
    return (
        f"Imported into Copilot/VS Code chat session {sid}\n"
        f"Backup: {backup}\n"
        f"Session file: {jsonl_path}\n"
        f"JSON mirror: {json_path}\n"
        f"Index: {workspace / 'state.vscdb'}\n"
        f"{warning}"
    )


def repair_copilot_imports(home: Path, apply: bool, force_running_vscode: bool = False) -> str:
    root = resolve_paths(home).copilot_workspace_storage
    if apply:
        _guard_copilot_write_when_vscode_running(home, root, force=force_running_vscode)
    repairs = _find_copilot_imports_to_repair(root)
    if not repairs:
        return "No Copilot imports need repair.\n"
    lines = [
        f"{row['session_id']} | {row['title']} | {', '.join(row['reasons'])} | {row['workspace']}"
        for row in repairs
    ]
    if not apply:
        return "Copilot imports needing repair:\n" + "\n".join(lines) + "\n" + _vscode_running_warning(home, root)
    backup = backup_paths(
        home,
        [
            path
            for row in repairs
            for path in (
                Path(row["workspace"]) / "chatSessions",
                Path(row["workspace"]) / "state.vscdb",
                Path(row["workspace"]) / "state.vscdb.backup",
            )
        ],
    )
    for row in repairs:
        workspace = Path(row["workspace"])
        payload = row["payload"]
        sid = str(row["session_id"])
        chat_dir = workspace / "chatSessions"
        _write_copilot_session_files(chat_dir, sid, payload)
        _upsert_copilot_chat_index(
            workspace,
            sid,
            str(row["title"]),
            int(payload.get("creationDate") or payload.get("lastMessageDate") or time.time() * 1000),
            int(payload.get("lastMessageDate") or payload.get("creationDate") or time.time() * 1000),
        )
        _upsert_copilot_agent_session_cache(
            workspace,
            sid,
            str(row["title"]),
            int(payload.get("creationDate") or payload.get("lastMessageDate") or time.time() * 1000),
            int(payload.get("lastMessageDate") or payload.get("creationDate") or time.time() * 1000),
            _read_copilot_workspace_path(workspace),
        )
    return f"Repaired {len(repairs)} Copilot imported session(s).\nBackup: {backup}\n{_vscode_running_warning(home, root)}"


def _write_copilot_session_files(chat_dir: Path, sid: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    chat_dir.mkdir(parents=True, exist_ok=True)
    payload = _normalize_copilot_payload(payload)
    json_path = chat_dir / f"{sid}.json"
    jsonl_path = chat_dir / f"{sid}.jsonl"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    jsonl_path.write_text(json.dumps({"kind": 0, "v": payload}, ensure_ascii=False) + "\n", encoding="utf-8")
    return json_path, jsonl_path


def _find_copilot_imports_to_repair(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    repairs: list[dict[str, Any]] = []
    for workspace in sorted(path for path in root.iterdir() if path.is_dir()):
        chat_dir = workspace / "chatSessions"
        if not chat_dir.exists():
            continue
        index = _read_copilot_chat_index(workspace)
        entries = index.get("entries") if isinstance(index.get("entries"), dict) else {}
        agent_cache = _read_vscode_json_key(workspace, COPILOT_AGENT_CACHE_KEY, [])
        cached_resources = {
            str(item.get("resource"))
            for item in agent_cache
            if isinstance(item, dict) and item.get("resource")
        } if isinstance(agent_cache, list) else set()
        for path in sorted(chat_dir.glob("*.json")):
            payload = read_json(path)
            if not isinstance(payload, dict) or not _is_chatbridge_copilot_payload(payload):
                continue
            sid = str(payload.get("sessionId") or path.stem)
            title = str(payload.get("customTitle") or payload.get("title") or sid)
            jsonl_ok = (chat_dir / f"{sid}.jsonl").exists()
            index_ok = sid in entries
            cache_ok = _vscode_local_chat_session_uri(sid) in cached_resources
            shape_ok = not _copilot_payload_needs_native_shape(payload)
            internal_cleanup_ok = not _copilot_payload_needs_internal_cleanup(payload)
            image_cleanup_ok = not _copilot_payload_needs_image_cleanup(payload)
            if jsonl_ok and index_ok and cache_ok and shape_ok and internal_cleanup_ok and image_cleanup_ok:
                continue
            reasons = []
            if not jsonl_ok:
                reasons.append("jsonl missing")
            if not index_ok:
                reasons.append("chat index missing")
            if not cache_ok:
                reasons.append("agent session cache missing")
            if not shape_ok:
                reasons.append("message parts need native shape")
            if not internal_cleanup_ok:
                reasons.append("internal bootstrap messages need cleanup")
            if not image_cleanup_ok:
                reasons.append("embedded image data URL needs cleanup")
            repairs.append(
                {
                    "workspace": str(workspace),
                    "session_id": sid,
                    "title": title,
                    "payload": payload,
                    "reasons": reasons,
                }
            )
    return repairs


def _is_chatbridge_copilot_payload(payload: dict[str, Any]) -> bool:
    title = str(payload.get("customTitle") or payload.get("title") or "")
    return bool(payload.get("isImported")) or title.startswith("[Imported from ")


def _guard_copilot_write_when_vscode_running(home: Path, storage: Path, *, force: bool) -> None:
    if force or not _is_live_copilot_storage(home, storage) or not _is_vscode_running():
        return
    raise VSCodeRunningError(
        "VS Code is currently running, so Copilot imports cannot be safely applied. "
        "VS Code keeps chat history and Agent Sessions caches in memory and can overwrite "
        "offline ChatBridge changes on reload or quit.\n"
        "Fully quit all VS Code windows, then run `chatbridge repair-copilot-imports --apply`. "
        "Use --force only if you intentionally want to write while VS Code may overwrite it."
    )


def _vscode_running_warning(home: Path, storage: Path) -> str:
    if not _is_live_copilot_storage(home, storage) or not _is_vscode_running():
        return ""
    return (
        "WARNING: VS Code appears to be running. VS Code keeps the chat history index and Agent "
        "Sessions cache in memory, so reload-window can overwrite offline ChatBridge changes. "
        "Fully quit all VS Code windows before running `chatbridge repair-copilot-imports --apply`.\n"
    )


def _is_vscode_running() -> bool:
    if os.name == "nt":
        return _is_vscode_running_windows()
    try:
        output = subprocess.run(
            ["ps", "-axo", "args"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except Exception:
        return False
    patterns = (
        "Visual Studio Code.app/Contents/MacOS/Code",
        "Visual Studio Code - Insiders.app/Contents/MacOS/Electron",
        "VSCodium.app/Contents/MacOS/Electron",
        "Cursor.app/Contents/MacOS/Cursor",
        "Code.exe",
        "/usr/share/code/code",
        "/snap/code/",
    )
    return any(pattern in output for pattern in patterns)


def _is_vscode_running_windows() -> bool:
    try:
        output = subprocess.run(
            ["tasklist", "/FO", "CSV"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return False
    patterns = ("Code.exe", "Code - Insiders.exe", "VSCodium.exe", "Cursor.exe")
    return any(pattern in output for pattern in patterns)


def _is_live_copilot_storage(home: Path, storage: Path) -> bool:
    try:
        actual_home = _real_user_home().expanduser().resolve()
        requested_home = home.expanduser().resolve()
    except OSError:
        return False
    if requested_home != actual_home:
        return False
    try:
        storage_resolved = storage.expanduser().resolve()
    except OSError:
        storage_resolved = storage.expanduser().absolute()
    live_roots = (
        actual_home / "Library/Application Support/Code/User/workspaceStorage",
        actual_home / "Library/Application Support/Code - Insiders/User/workspaceStorage",
        actual_home / "Library/Application Support/VSCodium/User/workspaceStorage",
        actual_home / "Library/Application Support/Cursor/User/workspaceStorage",
        actual_home / ".config/Code/User/workspaceStorage",
        actual_home / ".config/Code - Insiders/User/workspaceStorage",
        actual_home / ".config/VSCodium/User/workspaceStorage",
        actual_home / ".config/Cursor/User/workspaceStorage",
    )
    for root in live_roots:
        try:
            if storage_resolved == root.expanduser().resolve():
                return True
        except OSError:
            if storage_resolved == root.expanduser().absolute():
                return True
    return False


def _real_user_home() -> Path:
    if os.name != "nt":
        try:
            import pwd

            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception:
            pass
    return Path.home()


def _copilot_projected_requests(session: Session, handoff: str, now_ms: int) -> list[dict[str, Any]]:
    projected = _project_session_messages(session)
    if not projected:
        return [
            _copilot_request(
                handoff,
                "Imported handoff context. Verify workspace state before continuing.",
                now_ms,
            )
        ]

    requests: list[dict[str, Any]] = []
    current_user = ""
    current_timestamp = now_ms
    assistant_parts: list[str] = []

    def flush() -> None:
        nonlocal current_user, current_timestamp, assistant_parts
        if not current_user and not assistant_parts:
            return
        user_text = current_user or f"Imported context from {session.source_label}"
        response_text = "\n\n".join(part for part in assistant_parts if part)
        requests.append(_copilot_request(user_text, response_text, current_timestamp))
        current_user = ""
        current_timestamp = now_ms
        assistant_parts = []

    for message in projected:
        if message.role == "user":
            flush()
            current_user = message.text
            current_timestamp = _source_timestamp_ms(message.timestamp, now_ms)
        else:
            assistant_parts.append(message.text)
            if not current_user:
                current_timestamp = _source_timestamp_ms(message.timestamp, now_ms)
    flush()
    return requests


def _copilot_request(user_text: str, response_text: str, timestamp_ms: int) -> dict[str, Any]:
    response = [_copilot_response_part(response_text)] if response_text else []
    return {
        "requestId": str(uuid.uuid4()),
        "responseId": str(uuid.uuid4()),
        "timestamp": timestamp_ms,
        "message": _copilot_message(user_text),
        "variableData": {"variables": []},
        "response": response,
        "modelState": {"value": 1, "completedAt": timestamp_ms},
        "isCanceled": False,
    }


def _normalize_copilot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    requests = normalized.get("requests")
    if not isinstance(requests, list):
        normalized["requests"] = []
        return normalized
    normalized_requests = [_normalize_copilot_request(req) for req in requests if isinstance(req, dict)]
    cleaned_requests = [
        req for req in normalized_requests if not _is_internal_copilot_import_request(req)
    ]
    normalized["requests"] = cleaned_requests if cleaned_requests else normalized_requests
    return normalized


def _normalize_copilot_request(req: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(req)
    timestamp_ms = int(normalized.get("timestamp") or time.time() * 1000)
    user_text = sanitize_embedded_images(_copilot_message_text(normalized.get("message")))
    normalized["requestId"] = str(normalized.get("requestId") or uuid.uuid4())
    normalized["responseId"] = str(normalized.get("responseId") or uuid.uuid4())
    normalized["timestamp"] = timestamp_ms
    normalized["message"] = _copilot_message(user_text)
    normalized["variableData"] = _copilot_variable_data(normalized.get("variableData"))
    normalized["response"] = _normalize_copilot_response(normalized.get("response"))
    normalized["modelState"] = _copilot_model_state(normalized.get("modelState"), timestamp_ms)
    normalized["isCanceled"] = bool(normalized.get("isCanceled", False))
    return normalized


def _copilot_message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str):
            return text
        parts = message.get("parts")
        if isinstance(parts, list):
            return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    return text_from_any(message)


def _copilot_message(text: str) -> dict[str, Any]:
    safe_text = sanitize_embedded_images(str(text))
    return {"text": safe_text, "parts": [_copilot_text_part(safe_text)]}


def _copilot_text_part(text: str) -> dict[str, Any]:
    lines = text.split("\n") or [""]
    end_line = max(1, len(lines))
    end_column = len(lines[-1]) + 1
    return {
        "kind": "text",
        "range": {"start": 0, "endExclusive": len(text)},
        "editorRange": {
            "startLineNumber": 1,
            "startColumn": 1,
            "endLineNumber": end_line,
            "endColumn": end_column,
        },
        "text": text,
    }


def _normalize_copilot_response(response: Any) -> list[Any]:
    if response is None:
        return []
    if isinstance(response, str):
        return [_copilot_response_part(response)] if response else []
    if isinstance(response, list):
        normalized = []
        for item in response:
            if isinstance(item, dict):
                normalized.append(_copilot_response_dict(item))
            elif isinstance(item, str) and item:
                normalized.append(_copilot_response_part(item))
        return normalized
    text = text_from_any(response)
    return [_copilot_response_part(text)] if text else []


def _copilot_response_dict(item: dict[str, Any]) -> dict[str, Any]:
    copied = dict(item)
    if "value" in copied and "kind" not in copied:
        copied.setdefault("supportThemeIcons", False)
        copied.setdefault("supportHtml", False)
        copied.setdefault("supportAlertSyntax", False)
    return copied


def _copilot_response_part(text: str) -> dict[str, Any]:
    return {
        "value": sanitize_embedded_images(str(text)),
        "supportThemeIcons": False,
        "supportHtml": False,
        "supportAlertSyntax": False,
    }


def _copilot_variable_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("variables"), list):
        return value
    return {"variables": []}


def _copilot_model_state(value: Any, timestamp_ms: int) -> dict[str, Any]:
    if isinstance(value, dict):
        state = dict(value)
        state.setdefault("value", 1)
        state.setdefault("completedAt", timestamp_ms)
        return state
    return {"value": 1, "completedAt": timestamp_ms}


def _copilot_payload_needs_native_shape(payload: dict[str, Any]) -> bool:
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return True
    for req in requests:
        if not isinstance(req, dict):
            return True
        message = req.get("message")
        if isinstance(message, str):
            continue
        if not isinstance(message, dict):
            return True
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            return True
        for part in parts:
            if not isinstance(part, dict):
                return True
            if part.get("kind") != "text" or not isinstance(part.get("range"), dict) or not isinstance(part.get("editorRange"), dict):
                return True
    return False


def _copilot_payload_needs_cleanup(payload: dict[str, Any]) -> bool:
    return _copilot_payload_needs_internal_cleanup(payload) or _copilot_payload_needs_image_cleanup(payload)


def _copilot_payload_needs_internal_cleanup(payload: dict[str, Any]) -> bool:
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return False
    return any(
        isinstance(req, dict) and _is_internal_copilot_import_request(_normalize_copilot_request(req))
        for req in requests
    )


def _copilot_payload_needs_image_cleanup(payload: dict[str, Any]) -> bool:
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return False
    return any(
        isinstance(req, dict) and _copilot_request_has_embedded_image_data(req)
        for req in requests
    )


def _copilot_request_has_embedded_image_data(req: dict[str, Any]) -> bool:
    return "data:image/" in json.dumps(req, ensure_ascii=False)


def _is_internal_copilot_import_request(req: dict[str, Any]) -> bool:
    user_text = _copilot_message_text(req.get("message")).strip()
    response_text = text_from_any(req.get("response")).strip()
    combined = f"{user_text}\n{response_text}".lower()
    internal_markers = (
        "<permissions instructions>",
        "<environment_context>",
        "<developer_instructions>",
        "filesystem sandboxing defines which files can be read or written",
        "current_date>",
    )
    if user_text.startswith("Imported context from ") and any(marker in combined for marker in internal_markers):
        return True
    return any(user_text.lower().startswith(marker) for marker in internal_markers)


def _upsert_copilot_chat_index(workspace: Path, sid: str, title: str, created_ms: int, updated_ms: int) -> None:
    db_path = workspace / "state.vscdb"
    index = _read_copilot_chat_index(workspace)
    entries = index.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        index["entries"] = entries
    entries[sid] = {
        "sessionId": sid,
        "title": title,
        "lastMessageDate": updated_ms,
        "timing": {
            "created": created_ms,
            "lastRequestStarted": updated_ms,
            "lastRequestEnded": updated_ms,
        },
        "initialLocation": "panel",
        "hasPendingEdits": False,
        "isEmpty": False,
        "isExternal": False,
        "lastResponseState": 1,
        "permissionLevel": "default",
        "isImported": True,
    }
    _write_vscode_json_key(workspace, COPILOT_CHAT_INDEX_KEY, index)


def _upsert_copilot_agent_session_cache(
    workspace: Path,
    sid: str,
    title: str,
    created_ms: int,
    updated_ms: int,
    project_path: str | None,
) -> None:
    resource = _vscode_local_chat_session_uri(sid)
    cache = _read_vscode_json_key(workspace, COPILOT_AGENT_CACHE_KEY, [])
    if not isinstance(cache, list):
        cache = []
    cache = [
        item
        for item in cache
        if not isinstance(item, dict) or item.get("resource") != resource
    ]
    entry: dict[str, Any] = {
        "providerType": "local",
        "providerLabel": "Local",
        "resource": resource,
        "icon": "vm",
        "label": title,
        "status": 1,
        "timing": {
            "created": created_ms,
            "lastRequestStarted": updated_ms,
            "lastRequestEnded": updated_ms,
        },
    }
    if project_path and not project_path.startswith(("vscode-remote://", "vscode://")):
        entry["metadata"] = {"workingDirectoryPath": project_path}
    cache.insert(0, entry)
    _write_vscode_json_key(workspace, COPILOT_AGENT_CACHE_KEY, cache)
    _mark_copilot_agent_session_unarchived(workspace, resource, updated_ms)


def _mark_copilot_agent_session_unarchived(workspace: Path, resource: str, updated_ms: int) -> None:
    states = _read_vscode_json_key(workspace, COPILOT_AGENT_STATE_KEY, [])
    if not isinstance(states, list):
        states = []
    next_states = []
    seen = False
    for item in states:
        if not isinstance(item, dict) or item.get("resource") != resource:
            next_states.append(item)
            continue
        repaired = dict(item)
        repaired["archived"] = False
        repaired.setdefault("read", updated_ms)
        next_states.append(repaired)
        seen = True
    if not seen:
        next_states.append({"resource": resource, "archived": False, "read": updated_ms})
    _write_vscode_json_key(workspace, COPILOT_AGENT_STATE_KEY, next_states)


def _read_copilot_chat_index(workspace: Path) -> dict[str, Any]:
    data = _read_vscode_json_key(workspace, COPILOT_CHAT_INDEX_KEY, {"version": 1, "entries": {}})
    if not isinstance(data, dict):
        return {"version": 1, "entries": {}}
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    data.setdefault("version", 1)
    return data


def _read_vscode_json_key(workspace: Path, key: str, default: Any) -> Any:
    db_path = workspace / "state.vscdb"
    if not db_path.exists():
        return default
    try:
        con = sqlite3.connect(db_path, timeout=10)
        row = con.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)).fetchone()
        con.close()
    except sqlite3.Error:
        return default
    if not row:
        return default
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _write_vscode_json_key(workspace: Path, key: str, value: Any) -> None:
    db_path = workspace / "state.vscdb"
    workspace.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
        con.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )
        con.commit()
    finally:
        con.close()


def _vscode_local_chat_session_uri(sid: str) -> str:
    encoded = base64.urlsafe_b64encode(sid.encode("utf-8")).decode("ascii").rstrip("=")
    return f"vscode-chat-session://local/{encoded}"


def _find_copilot_workspace(storage: Path, project_path: str) -> Path | None:
    from .parsers import _read_workspace_path

    if not storage.exists():
        return None
    for ws in storage.iterdir():
        if ws.is_dir() and _same_copilot_project(_read_workspace_path(ws), project_path):
            return ws
    return None


def _same_copilot_project(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", left) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", right):
        return False
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)
    except OSError:
        return Path(left).expanduser().absolute() == Path(right).expanduser().absolute()


def _read_copilot_workspace_path(workspace: Path) -> str | None:
    data = read_json(workspace / "workspace.json")
    if not isinstance(data, dict):
        return None
    raw = data.get("folder") or data.get("workspace") or data.get("uri")
    if not isinstance(raw, str):
        return None
    if raw.startswith("file://"):
        value = raw.removeprefix("file://")
        if value.startswith("/") and re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
        return unquote(value)
    return raw


def _find_or_create_copilot_workspace(storage: Path, project_path: str) -> Path:
    workspace = _find_copilot_workspace(storage, project_path)
    if workspace is not None:
        return workspace
    workspace = storage / _copilot_workspace_storage_hash(project_path)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_json = workspace / "workspace.json"
    if not workspace_json.exists():
        workspace_json.write_text(
            json.dumps({"folder": _project_to_file_uri(project_path)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return workspace


def _copilot_workspace_storage_hash(project_path: str) -> str:
    return hashlib.md5(_project_to_file_uri(project_path).encode("utf-8")).hexdigest()


def _project_to_file_uri(project_path: str) -> str:
    clean = project_path.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", clean):
        return clean
    if re.match(r"^[A-Za-z]:[\\/]", clean):
        drive = clean[0].upper()
        rest = clean[2:].replace("\\", "/").lstrip("/")
        return f"file:///{drive}:/{quote(rest)}"
    if clean.startswith("//"):
        return "file:" + quote(clean)
    return "file://" + quote(clean if clean.startswith("/") else f"/{clean}")
