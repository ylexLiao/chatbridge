from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import toolcalls
from .models import Artifact, Message, Session
from .paths import resolve_paths
from .toolcalls import ToolCall
from .util import file_uri_to_path, iter_jsonl, project_to_claude_slug, read_json, text_from_any, timestamp_sort_key


SUPPORTED_SOURCES = {"copilot", "codex", "claude"}

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def load_sessions(source: str, home: Path, metadata_only: bool = False, limit: int | None = None) -> list[Session]:
    if source == "copilot":
        return load_copilot_sessions(home, metadata_only=metadata_only, limit=limit)
    if source == "codex":
        return load_codex_sessions(home, metadata_only=metadata_only, limit=limit)
    if source == "claude":
        return load_claude_sessions(home, metadata_only=metadata_only, limit=limit)
    raise ValueError(f"unsupported source: {source}")


def count_sessions(source: str, home: Path, project: str | None = None) -> int:
    if source == "copilot":
        return count_copilot_sessions(home, project=project)
    if source == "codex":
        return count_codex_sessions(home, project=project)
    if source == "claude":
        return count_claude_sessions(home, project=project)
    return 0


def find_session(source: str, home: Path, session_id: str | None = None, project: str | None = None) -> Session:
    if source == "copilot" and session_id:
        direct = load_copilot_session(home, session_id)
        if direct and _session_matches_project(direct, project):
            return direct
    if source == "codex" and session_id:
        direct = load_codex_session(home, session_id)
        if direct and _session_matches_project(direct, project):
            return direct
    if source == "claude" and session_id:
        direct = load_claude_session(home, session_id)
        if direct and _session_matches_project(direct, project):
            return direct
    sessions = load_sessions(source, home)
    if project:
        sessions = [session for session in sessions if _session_matches_project(session, project)]
    if session_id:
        for session in sessions:
            if session.session_id == session_id:
                return session
        raise SystemExit(_missing_session_message(source, home, session_id))
    if not sessions:
        raise SystemExit(f"No {source} sessions found")
    return sorted(
        sessions,
        key=lambda s: timestamp_sort_key(s.updated_at if s.updated_at not in (None, "") else s.created_at),
        reverse=True,
    )[0]


def _session_matches_project(session: Session, project: str | None) -> bool:
    if not project:
        return True
    return session.project_path == project or bool(session.project_path and Path(session.project_path) == Path(project))


def _missing_session_message(source: str, home: Path, session_id: str) -> str:
    message = f"No {source} session found for id {session_id}."
    if source == "codex":
        codex_home = resolve_paths(home).codex_home
        return (
            f"{message}\n"
            f"Tip: Codex imports need a readable rollout file or thread row under {codex_home}. "
            "If this id is from another Codex desktop/worktree, open that source in ChatBridge or export from its own home directory."
        )
    if source == "claude":
        claude_home = resolve_paths(home).claude_home
        return (
            f"{message}\n"
            f"Tip: Claude imports read {claude_home}/history.jsonl and {claude_home}/projects. "
            "Use the session picker so the id matches the active Claude home."
        )
    return message



def load_copilot_session(home: Path, session_id: str) -> Session | None:
    root = resolve_paths(home).copilot_workspace_storage
    if not root.exists():
        return None
    for ws in root.iterdir():
        if not ws.is_dir():
            continue
        project_path = _read_workspace_path(ws)
        chat_dir = ws / "chatSessions"
        if chat_dir.exists():
            for suffix in (".json", ".jsonl"):
                path = chat_dir / f"{session_id}{suffix}"
                if not path.exists():
                    continue
                session = _copilot_from_json(path, project_path, ws.name) if suffix == ".json" else _copilot_from_jsonl(path, project_path, ws.name)
                if session:
                    _attach_copilot_extras(ws, session)
                    return session
        transcript = ws / "GitHub.copilot-chat" / "transcripts" / f"{session_id}.jsonl"
        if transcript.exists():
            session = Session(source="copilot", session_id=session_id, title=session_id, project_path=project_path, metadata={"workspace_hash": ws.name}, raw_path=transcript)
            _attach_copilot_extras(ws, session)
            return session
    return None


def _attach_copilot_extras(ws: Path, session: Session) -> None:
    transcript = ws / "GitHub.copilot-chat" / "transcripts" / f"{session.session_id}.jsonl"
    if transcript.exists():
        _append_copilot_transcript(session, transcript)
    resources = ws / "GitHub.copilot-chat" / "chat-session-resources" / session.session_id
    if resources.exists():
        for content in sorted(resources.rglob("content.txt")):
            text = content.read_text(encoding="utf-8", errors="replace")
            session.artifacts.append(Artifact(kind="resource", text=text, path=str(content)))


def _append_copilot_transcript(session: Session, transcript: Path) -> None:
    seen = {(message.role, message.text) for message in session.messages}
    for row in iter_jsonl(transcript):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or row.get("type") or "assistant")
        text = text_from_any(row.get("content") or row.get("message") or row.get("text"))
        if text and (role, text) not in seen:
            seen.add((role, text))
            session.messages.append(Message(role=role, text=text, timestamp=row.get("timestamp")))

def load_copilot_sessions(home: Path, metadata_only: bool = False, limit: int | None = None) -> list[Session]:
    root = resolve_paths(home).copilot_workspace_storage
    sessions: dict[str, Session] = {}
    if not root.exists():
        return []
    workspaces = [ws for ws in root.iterdir() if ws.is_dir()]
    if metadata_only and limit:
        candidates: list[tuple[float, Path, Path]] = []
        workspace_paths: dict[Path, str | None] = {}
        for ws in workspaces:
            chat_dir = ws / "chatSessions"
            if not chat_dir.exists():
                continue
            for path in chat_dir.iterdir():
                if path.suffix in {".json", ".jsonl"}:
                    try:
                        candidates.append((path.stat().st_mtime, ws, path))
                    except OSError:
                        continue
        for _, ws, path in sorted(candidates, reverse=True)[: limit * 3]:
            project_path = workspace_paths.setdefault(ws, _read_workspace_path(ws))
            session = _copilot_metadata_fast(path, project_path, ws.name)
            if session:
                sessions[session.session_id] = session
                if len(sessions) >= limit:
                    return list(sessions.values())
        return list(sessions.values())
    for ws in sorted(workspaces):
        project_path = _read_workspace_path(ws)
        chat_dir = ws / "chatSessions"
        if chat_dir.exists():
            for path in sorted(chat_dir.iterdir()):
                if path.suffix == ".json":
                    session = _copilot_from_json(path, project_path, ws.name, metadata_only=metadata_only)
                elif path.suffix == ".jsonl":
                    session = _copilot_from_jsonl(path, project_path, ws.name, metadata_only=metadata_only)
                else:
                    continue
                if session:
                    sessions[session.session_id] = session
        transcript_dir = ws / "GitHub.copilot-chat" / "transcripts"
        if transcript_dir.exists() and not metadata_only:
            for path in sorted(transcript_dir.glob("*.jsonl")):
                sid = path.stem
                session = sessions.get(sid) or Session(
                    source="copilot",
                    session_id=sid,
                    title=sid,
                    project_path=project_path,
                    metadata={"workspace_hash": ws.name},
                    raw_path=path,
                )
                _append_copilot_transcript(session, path)
                sessions[sid] = session
        resources_root = ws / "GitHub.copilot-chat" / "chat-session-resources"
        if resources_root.exists() and not metadata_only:
            for session_dir in sorted(resources_root.iterdir()):
                if not session_dir.is_dir():
                    continue
                session = sessions.get(session_dir.name)
                if not session:
                    continue
                for content in sorted(session_dir.rglob("content.txt")):
                    text = content.read_text(encoding="utf-8", errors="replace")
                    session.artifacts.append(Artifact(kind="resource", text=text, path=str(content)))
    return list(sessions.values())


def count_copilot_sessions(home: Path, project: str | None = None) -> int:
    root = resolve_paths(home).copilot_workspace_storage
    if not root.exists():
        return 0
    session_ids: set[str] = set()
    for ws in root.iterdir():
        if not ws.is_dir():
            continue
        if project and _read_workspace_path(ws) != project:
            continue
        chat_dir = ws / "chatSessions"
        if not chat_dir.exists():
            continue
        for path in chat_dir.iterdir():
            if path.suffix in {".json", ".jsonl"}:
                session_ids.add(path.stem)
    return len(session_ids)


def count_codex_sessions(home: Path, project: str | None = None) -> int:
    root = resolve_paths(home).codex_home
    if project:
        return len([session for session in load_codex_sessions(home, metadata_only=True) if session.project_path == project])
    ids: set[str] = set()
    db_path = root / "state_5.sqlite"
    if db_path.exists():
        try:
            con = sqlite3.connect(db_path, timeout=10)
            table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
            if table:
                ids.update(str(row[0]) for row in con.execute("SELECT id FROM threads"))
            con.close()
        except sqlite3.Error:
            pass
    index = _codex_session_index(root)
    ids.update(index)
    ids.update(str(row["session_id"]) for row in iter_jsonl(root / "history.jsonl") if isinstance(row, dict) and row.get("session_id"))
    sessions_root = root / "sessions"
    if sessions_root.exists():
        ids.update(_codex_id_from_path(path, index) for path in sessions_root.rglob("*.jsonl"))
    return len(ids)


def count_claude_sessions(home: Path, project: str | None = None) -> int:
    root = resolve_paths(home).claude_home
    history = _claude_history_index(root)
    session_ids = {
        sid
        for sid, meta in history.items()
        if meta.prompts and (not project or meta.project == project)
    }
    projects = root / "projects"
    if project:
        project_dir = projects / project_to_claude_slug(project)
        if project_dir.exists():
            session_ids.update(path.stem for path in project_dir.glob("*.jsonl"))
        return len(session_ids)
    if projects.exists():
        session_ids.update(path.stem for path in projects.rglob("*.jsonl") if "subagents" not in path.parts)
    return len(session_ids)


def _copilot_metadata_fast(path: Path, project_path: str | None, workspace_hash: str) -> Session | None:
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            chunk = handle.read(65536).decode("utf-8", errors="ignore")
    except OSError:
        return None
    session_id = _regex_value(chunk, "sessionId") or path.stem
    title = _regex_value(chunk, "customTitle") or _regex_value(chunk, "title") or session_id
    created = _regex_number(chunk, "creationDate")
    updated = _regex_number(chunk, "lastMessageDate") or int(stat.st_mtime * 1000)
    version = _regex_number(chunk, "version")
    return Session(
        source="copilot",
        session_id=session_id,
        title=title,
        project_path=project_path,
        created_at=created,
        updated_at=updated,
        metadata={"workspace_hash": workspace_hash, "version": version},
        raw_path=path,
    )


def _regex_value(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _regex_number(text: str, key: str) -> int | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', text)
    return int(match.group(1)) if match else None

def _read_workspace_path(ws: Path) -> str | None:
    data = read_json(ws / "workspace.json")
    if not isinstance(data, dict):
        return None
    folder = data.get("folder")
    if isinstance(folder, str):
        return file_uri_to_path(folder)
    folders = data.get("folders")
    if isinstance(folders, list) and folders:
        first = folders[0]
        if isinstance(first, dict):
            return file_uri_to_path(first.get("uri") or first.get("path"))
    return None


def _copilot_from_json(path: Path, project_path: str | None, workspace_hash: str, metadata_only: bool = False) -> Session | None:
    data = read_json(path)
    if not isinstance(data, dict):
        return None
    session = Session(
        source="copilot",
        session_id=str(data.get("sessionId") or path.stem),
        title=str(data.get("customTitle") or data.get("title") or path.stem),
        project_path=project_path,
        created_at=data.get("creationDate"),
        updated_at=data.get("lastMessageDate"),
        metadata={"workspace_hash": workspace_hash, "version": data.get("version")},
        raw_path=path,
    )
    if not metadata_only:
        _add_copilot_requests(session, data.get("requests") or [])
    return session


def _copilot_from_jsonl(path: Path, project_path: str | None, workspace_hash: str, metadata_only: bool = False) -> Session | None:
    state: dict[str, Any] = {}
    for row in iter_jsonl(path):
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        if kind == 0 and isinstance(row.get("v"), dict):
            state = copy.deepcopy(row["v"])
            if metadata_only:
                break
        elif kind in {1, 2} and not metadata_only:
            keys = row.get("k")
            if isinstance(keys, list):
                if kind == 2:
                    _apply_copilot_array_delta(state, keys, row.get("v"))
                else:
                    _set_nested(state, keys, row.get("v"))
    if not state:
        return None
    session = Session(
        source="copilot",
        session_id=str(state.get("sessionId") or path.stem),
        title=str(state.get("customTitle") or state.get("title") or path.stem),
        project_path=project_path,
        created_at=state.get("creationDate"),
        updated_at=state.get("lastMessageDate"),
        metadata={"workspace_hash": workspace_hash, "version": state.get("version")},
        raw_path=path,
    )
    if not metadata_only:
        _add_copilot_requests(session, state.get("requests") or [])
    return session


def _set_nested(target: dict[str, Any], keys: list[Any], value: Any) -> None:
    if not keys:
        return
    current: Any = target
    for key in keys[:-1]:
        if isinstance(current, dict):
            current = current.setdefault(str(key), {})
        elif isinstance(current, list) and isinstance(key, int) and 0 <= key < len(current):
            current = current[key]
        else:
            return
    last = keys[-1]
    if isinstance(current, dict):
        current[str(last)] = value
    elif isinstance(current, list) and isinstance(last, int):
        while len(current) <= last:
            current.append(None)
        current[last] = value


def _apply_copilot_array_delta(target: dict[str, Any], keys: list[Any], value: Any) -> None:
    if keys == ["requests"] and isinstance(value, list):
        existing = target.get("requests")
        if isinstance(existing, list):
            existing.extend(copy.deepcopy(value))
        else:
            target["requests"] = copy.deepcopy(value)
        return
    _set_nested(target, keys, value)


def _add_copilot_requests(session: Session, requests: list[Any]) -> None:
    seen_messages: set[tuple[str, str]] = set()
    for req in requests:
        if not isinstance(req, dict):
            continue
        assistant_text = _copilot_assistant_text(req)
        artifacts = _copilot_request_artifacts(req)
        user_text = _clean_copilot_text(text_from_any(req.get("message")))
        if user_text and (assistant_text or artifacts or not _copilot_is_noise_request(req)):
            _append_unique_message(session, seen_messages, "user", user_text, req.get("timestamp"))
        if assistant_text:
            _append_unique_message(session, seen_messages, "assistant", assistant_text, req.get("timestamp"))
        for artifact in artifacts:
            if artifact.text:
                session.artifacts.append(artifact)


def _append_unique_message(session: Session, seen: set[tuple[str, str]], role: str, text: str, timestamp: Any) -> None:
    key = (role, text)
    if key in seen:
        return
    seen.add(key)
    session.messages.append(Message(role=role, text=text, timestamp=timestamp))


def _copilot_assistant_text(req: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.extend(_copilot_response_parts(req.get("response")))
    result = req.get("result")
    if isinstance(result, dict):
        explicit = result.get("response") or result.get("text") or result.get("message")
        parts.extend(_copilot_response_parts(explicit))
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            for round_data in metadata.get("toolCallRounds") or []:
                if isinstance(round_data, dict):
                    parts.extend(_copilot_response_parts(round_data.get("response")))
            for code_block in metadata.get("codeBlocks") or []:
                text = _clean_copilot_text(text_from_any(code_block))
                if text:
                    parts.append(text)
    else:
        parts.extend(_copilot_response_parts(result))
    return _join_unique_text(parts)


def _copilot_response_parts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_copilot_response_parts(item))
        return parts
    if isinstance(value, dict):
        kind = str(value.get("kind") or "")
        if kind in {"thinking", "mcpServersStarting", "mcpServersStarted"}:
            return []
        if kind == "toolInvocationSerialized":
            text = _copilot_serialized_tool_text(value.get("value"))
            return [text] if text else []
        parts = []
        for key in ("value", "text", "content", "message", "invocationMessage", "pastTenseMessage"):
            if key in value:
                text = _clean_copilot_text(text_from_any(value[key]))
                if text and not _copilot_is_noise_text(text):
                    parts.append(text)
        if parts:
            return parts
        text = _clean_copilot_text(text_from_any(value))
        return [text] if text and not _copilot_is_noise_text(text) else []
    text = _clean_copilot_text(text_from_any(value))
    return [text] if text and not _copilot_is_noise_text(text) else []


def _copilot_serialized_tool_text(value: Any) -> str:
    text = _clean_copilot_text(text_from_any(value))
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return _clean_copilot_text(text_from_any(parsed))


def _copilot_request_artifacts(req: dict[str, Any]) -> list[Artifact]:
    artifacts: list[Artifact] = []
    result = req.get("result")
    metadata = result.get("metadata") if isinstance(result, dict) else None
    if not isinstance(metadata, dict):
        return artifacts

    rendered = _clean_copilot_text(text_from_any(metadata.get("renderedUserMessage")))
    if rendered and not rendered.lstrip().startswith("<context>") and not _copilot_is_noise_text(rendered):
        artifacts.append(Artifact(kind="rendered-user-context", text=rendered))

    tool_results = metadata.get("toolCallResults")
    if isinstance(tool_results, dict):
        iterable = tool_results.values()
    elif isinstance(tool_results, list):
        iterable = tool_results
    else:
        iterable = []
    for result_item in iterable:
        text = _clean_copilot_text(text_from_any(result_item))
        if text and not _copilot_is_noise_text(text):
            artifacts.append(Artifact(kind="tool-result", text=text, path=_extract_copilot_path(text)))
    return artifacts


def _extract_copilot_path(text: str) -> str | None:
    match = re.search(r"(?:file://)?([A-Za-z]:[\\/][^\s`'\")]+|/[^\s`'\")]+)", text)
    return match.group(1) if match else None


def _join_unique_text(parts: list[str]) -> str:
    seen: set[str] = set()
    clean_parts: list[str] = []
    for part in parts:
        text = _clean_copilot_text(part)
        if not text or text in seen or _copilot_is_noise_text(text):
            continue
        seen.add(text)
        clean_parts.append(text)
    return "\n\n".join(clean_parts)


def _clean_copilot_text(text: str) -> str:
    return text.strip()


def _copilot_is_noise_request(req: dict[str, Any]) -> bool:
    return bool(req.get("response")) and not _copilot_response_parts(req.get("response")) and not req.get("result")


def _copilot_is_noise_text(text: str) -> bool:
    clean = " ".join(text.split())
    return clean in {"mcpServersStarting", "mcpServersStarted"} or clean.startswith("mcpServersStarting ")


def load_codex_sessions(home: Path, metadata_only: bool = False, limit: int | None = None) -> list[Session]:
    root = resolve_paths(home).codex_home
    index = _codex_session_index(root)
    if metadata_only and limit:
        return _load_codex_metadata_limited(root, index, limit)

    sessions: dict[str, Session] = {}
    for path in sorted((root / "sessions").rglob("*.jsonl")) if (root / "sessions").exists() else []:
        sid = _codex_id_from_path(path, index)
        session = _codex_session_from_rollout(path, sid, index.get(sid, {}), metadata_only=metadata_only)
        sessions[sid] = session
    seen_texts: dict[str, set[str]] = {}
    for row in iter_jsonl(root / "history.jsonl"):
        if not isinstance(row, dict) or not row.get("session_id"):
            continue
        sid = str(row["session_id"])
        session = sessions.setdefault(
            sid,
            Session(source="codex", session_id=sid, title=str(index.get(sid, {}).get("thread_name") or sid), updated_at=index.get(sid, {}).get("updated_at")),
        )
        if metadata_only:
            continue
        seen = seen_texts.get(sid)
        if seen is None:
            seen = {message.text for message in session.messages}
            seen_texts[sid] = seen
        text = text_from_any(row.get("text"))
        if text and text not in seen:
            seen.add(text)
            session.messages.append(Message(role="user", text=text, timestamp=row.get("ts")))
    return list(sessions.values())


def load_codex_session(home: Path, session_id: str) -> Session | None:
    root = resolve_paths(home).codex_home
    index = _codex_session_index(root)
    state = _codex_thread_from_state_db(root, session_id)
    meta = {**index.get(session_id, {}), **state}
    for path in _codex_rollout_candidates(root, session_id, meta):
        if path.exists():
            return _codex_session_from_rollout(path, session_id, meta, metadata_only=False)
    if not meta:
        history_rows = [row for row in iter_jsonl(root / "history.jsonl") if isinstance(row, dict) and str(row.get("session_id") or "") == session_id]
        if not history_rows:
            return None
        meta = {"thread_name": text_from_any(history_rows[-1].get("text")) or session_id, "updated_at": history_rows[-1].get("ts")}
    session = Session(
        source="codex",
        session_id=session_id,
        title=_codex_title(meta, session_id),
        project_path=meta.get("cwd"),
        created_at=meta.get("created_at_ms") or meta.get("created_at"),
        updated_at=meta.get("updated_at_ms") or meta.get("updated_at"),
        metadata={"transcript": "missing"},
    )
    seen_texts = {message.text for message in session.messages}
    for key in ("first_user_message", "preview"):
        text = text_from_any(meta.get(key))
        if text and text not in seen_texts:
            seen_texts.add(text)
            session.messages.append(Message(role="user", text=text, timestamp=session.updated_at or session.created_at))
    for row in iter_jsonl(root / "history.jsonl"):
        if isinstance(row, dict) and str(row.get("session_id") or "") == session_id:
            text = text_from_any(row.get("text"))
            if text and text not in seen_texts:
                seen_texts.add(text)
                session.messages.append(Message(role="user", text=text, timestamp=row.get("ts")))
    return session


def _codex_session_from_rollout(path: Path, sid: str, meta: dict[str, Any], metadata_only: bool = False) -> Session:
    session = Session(
        source="codex",
        session_id=sid,
        title=_codex_title(meta, sid),
        project_path=meta.get("cwd"),
        created_at=meta.get("created_at_ms") or meta.get("created_at"),
        updated_at=meta.get("updated_at_ms") or meta.get("updated_at"),
        raw_path=path,
    )
    if metadata_only:
        return session
    rows: list[dict[str, Any]] = []
    for row in iter_jsonl(path):
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if row.get("type") == "session_meta" and isinstance(payload, dict):
            session.project_path = str(payload.get("cwd") or session.project_path or "") or None
            session.created_at = payload.get("timestamp") or session.created_at
        elif row.get("type") == "turn_context" and isinstance(payload, dict) and not session.project_path:
            session.project_path = str(payload.get("cwd") or "") or None
        rows.append(row)
    session.messages.extend(codex_rollout_messages(rows))
    return session


# Prefixes of text Codex injects into user turns (not typed by the user).
CODEX_CONTEXT_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<permissions instructions>",
    "<skills_instructions>",
    "<turn_aborted>",
    "<collaboration_mode>",
    "<multi_agent",
    "# AGENTS.md instructions",
)
CODEX_IDE_CONTEXT_PREFIX = "# Context from my IDE setup:"
CODEX_IDE_REQUEST_MARKER = "## My request for Codex:"
CODEX_SHELL_NAMES = {"shell", "exec_command", "container.exec", "local_shell", "unified_exec"}


def codex_rollout_messages(rows: list[dict[str, Any]]) -> list[Message]:
    """Turn rollout rows into user/assistant/tool messages, in order.

    Codex >= 0.153 ("paginated" history) records every visible item as an
    ``event_msg``/``item_completed`` row, which is the cleanest source. Older
    ("legacy") rollouts only carry ``response_item`` rows (plus duplicate
    ``event_msg`` user/agent messages, which are skipped).
    """
    if any(_codex_row_payload(row).get("type") == "item_completed" for row in rows if row.get("type") == "event_msg"):
        return _codex_messages_from_items(rows)
    return _codex_messages_from_response_items(rows)


def _codex_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else row


def _codex_messages_from_items(rows: list[dict[str, Any]]) -> list[Message]:
    entries: list[Message | ToolCall] = []
    for row in rows:
        payload = _codex_row_payload(row)
        if row.get("type") != "event_msg" or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        timestamp = row.get("timestamp")
        kind = str(item.get("type") or "")
        if kind == "UserMessage":
            text = _codex_user_text(_codex_item_text(item.get("content")))
            if isinstance(text, ToolCall):
                text.timestamp = timestamp
                entries.append(text)
            elif text:
                entries.append(Message(role="user", text=text, timestamp=timestamp))
        elif kind == "AgentMessage":
            text = _codex_item_text(item.get("content")) or str(item.get("text") or "")
            if text.strip():
                entries.append(Message(role="assistant", text=text.strip(), timestamp=timestamp))
        elif kind == "CommandExecution":
            call = toolcalls.shell_call(item.get("command") or "", name="shell", cwd=item.get("cwd"))
            call.add_output(item.get("aggregated_output") or item.get("formatted_output") or "\n".join(
                part for part in (item.get("stdout"), item.get("stderr")) if part
            ))
            call.exit_code = item.get("exit_code") if isinstance(item.get("exit_code"), int) else None
            call.timestamp = timestamp
            entries.append(call)
        elif kind == "FileChange":
            call = toolcalls.file_change_call(item.get("changes"), name="apply_patch")
            if str(item.get("status") or "").lower() not in {"", "completed"}:
                call.add_output(str(item.get("stderr") or item.get("status")))
                call.is_error = True
            call.timestamp = timestamp
            entries.append(call)
        elif kind in {"McpToolCall", "DynamicToolCall"}:
            server = item.get("server") or item.get("namespace")
            name = ".".join(str(part) for part in (server, item.get("tool")) if part) or kind
            call = toolcalls.generic_call(name, item.get("arguments"))
            result = item.get("result") if item.get("result") is not None else item.get("content_items")
            call.add_output(toolcalls.output_text(result if result is not None else item.get("error")))
            call.is_error = bool(item.get("error"))
            call.timestamp = timestamp
            entries.append(call)
        elif kind == "WebSearch":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            query = item.get("query") or action.get("query") or action.get("url") or ""
            entries.append(toolcalls.ToolCall(name="web_search", title=str(query), timestamp=timestamp))
        elif kind == "ImageView":
            path = file_uri_to_path(str(item.get("path") or "")) or ""
            entries.append(toolcalls.ToolCall(name="view_image", title=path, timestamp=timestamp))
        elif kind == "ImageGeneration":
            title = str(item.get("revised_prompt") or item.get("saved_path") or "image")
            entries.append(toolcalls.ToolCall(name="image_generation", title=title, timestamp=timestamp))
        elif kind == "Plan":
            entries.append(toolcalls.ToolCall(name="plan", title="plan", body=str(item.get("text") or ""), timestamp=timestamp))
        elif kind in {"CollabAgentToolCall", "SubAgentActivity"}:
            label = item.get("tool") or item.get("kind") or "agent"
            prompt = str(item.get("prompt") or item.get("agent_path") or "")
            entries.append(toolcalls.ToolCall(name="subagent", title=str(label), body=prompt, body_limit=2000, timestamp=timestamp))
        elif kind in {"Reasoning", "ContextCompaction", "HookPrompt", "Sleep", "EnteredReviewMode", "ExitedReviewMode", "FunctionCallOutput"}:
            continue
        elif kind:
            call = toolcalls.generic_call(kind, {key: value for key, value in item.items() if key not in {"type", "id"}})
            call.timestamp = timestamp
            entries.append(call)
    return _finalize_entries(entries)


def _codex_messages_from_response_items(rows: list[dict[str, Any]]) -> list[Message]:
    entries: list[Message | ToolCall] = []
    calls: dict[str, ToolCall] = {}
    last_shell: ToolCall | None = None
    for row in rows:
        if row.get("type") in {"event_msg", "session_meta", "turn_context", "compacted", "world_state", "token_usage_record"}:
            continue
        payload = _codex_row_payload(row)
        timestamp = row.get("timestamp") or row.get("ts")
        kind = payload.get("type")
        if kind == "message" or (kind is None and payload.get("role")):
            role = str(payload.get("role") or "assistant")
            text = text_from_any(payload.get("content") or payload.get("text") or payload.get("message")).strip()
            if not text or role in {"developer", "system"}:
                continue
            if role == "user":
                converted = _codex_user_text(text)
                if isinstance(converted, ToolCall):
                    converted.timestamp = timestamp
                    entries.append(converted)
                elif converted:
                    entries.append(Message(role="user", text=converted, timestamp=timestamp))
            else:
                entries.append(Message(role="assistant", text=text, timestamp=timestamp))
            continue
        if kind in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name") or "tool")
            raw_args = payload.get("arguments") if kind == "function_call" else payload.get("input")
            args = _json_or_value(raw_args)
            if name == "write_stdin":
                chars = str(args.get("chars") or "") if isinstance(args, dict) else ""
                if not chars and last_shell is not None:
                    call = last_shell  # polling a running command: output belongs to it
                else:
                    call = toolcalls.ToolCall(name=name, title=f"send input {chars!r}")
                    entries.append(call)
            elif name in CODEX_SHELL_NAMES:
                command = args.get("cmd") or args.get("command") if isinstance(args, dict) else args
                cwd = args.get("workdir") if isinstance(args, dict) else None
                call = toolcalls.shell_call(command or "", name="shell", cwd=cwd)
                last_shell = call
                entries.append(call)
            elif name == "apply_patch":
                patch = args.get("input") or args.get("patch") if isinstance(args, dict) else raw_args
                call = toolcalls.patch_text_call(str(patch or ""))
                entries.append(call)
            elif name == "update_plan" and isinstance(args, dict):
                call = toolcalls.plan_call(args.get("plan"), args.get("explanation"))
                entries.append(call)
            else:
                call = toolcalls.generic_call(name, args)
                entries.append(call)
            call.timestamp = call.timestamp or timestamp
            call_id = payload.get("call_id")
            if call_id:
                calls[str(call_id)] = call
            continue
        if kind in {"function_call_output", "custom_tool_call_output"}:
            call = calls.get(str(payload.get("call_id") or ""))
            if call is None:
                continue
            raw = payload.get("output")
            exit_code = toolcalls.output_exit_code(raw)
            if exit_code is not None:
                call.exit_code = exit_code
            call.add_output(toolcalls.strip_exec_preamble(toolcalls.output_text(raw)))
            continue
        if kind == "local_shell_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            call = toolcalls.shell_call(action.get("command") or "", name="shell", cwd=action.get("working_directory"))
            call.timestamp = timestamp
            last_shell = call
            entries.append(call)
            if payload.get("call_id"):
                calls[str(payload["call_id"])] = call
            continue
        if kind == "web_search_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            query = action.get("query") or action.get("url") or ""
            entries.append(toolcalls.ToolCall(name="web_search", title=str(query), timestamp=timestamp))
    return _finalize_entries(entries)


def _codex_item_text(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and str(part.get("type") or "").lower() in {"text", "input_text", "output_text"}:
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, dict) and str(part.get("type") or "").lower() in {"image", "local_image", "input_image"}:
                parts.append(text_from_any({"type": "image", **part}))
        return "\n".join(part for part in parts if part).strip()
    return text_from_any(content).strip()


def _codex_user_text(text: str) -> str | ToolCall:
    """Strip Codex-injected context from a user turn; '' means the turn was pure context."""
    clean = text.strip()
    if not clean or clean.startswith(CODEX_CONTEXT_PREFIXES):
        return ""
    if clean.startswith(CODEX_IDE_CONTEXT_PREFIX):
        _, marker, request = clean.partition(CODEX_IDE_REQUEST_MARKER)
        return request.strip() if marker else ""
    if clean.startswith("<user_shell_command>"):
        command = _between(clean, "<command>", "</command>")
        result = _between(clean, "<result>", "</result>")
        call = toolcalls.shell_call(command, name="user_shell")
        call.exit_code = toolcalls.output_exit_code(result.replace("Exit code:", "Process exited with code", 1))
        call.add_output(toolcalls.strip_exec_preamble(result.strip()))
        return call
    return clean


def _between(text: str, start: str, end: str) -> str:
    _, found, rest = text.partition(start)
    if not found:
        return ""
    return rest.partition(end)[0].strip("\n")


def _json_or_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip()[:1] == "{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _finalize_entries(entries: list[Message | ToolCall]) -> list[Message]:
    """Render tool calls and drop adjacent exact duplicates."""
    messages: list[Message] = []
    for entry in entries:
        if isinstance(entry, ToolCall):
            message = Message(role="tool", text=entry.render(), timestamp=entry.timestamp, tool=entry.name)
        else:
            message = entry
        if messages and messages[-1].role == message.role and messages[-1].text == message.text:
            continue
        messages.append(message)
    return messages


def _codex_title(meta: dict[str, Any], fallback: str) -> str:
    """Codex's own thread name (auto or renamed) beats the first-message title."""
    for key in ("name", "thread_name", "title", "preview"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _codex_rollout_candidates(root: Path, session_id: str, meta: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    for value in (meta.get("rollout_path"), meta.get("path")):
        if value:
            candidates.append(Path(str(value)).expanduser())
    sessions_root = root / "sessions"
    if sessions_root.exists():
        candidates.extend(sorted(sessions_root.rglob(f"*{session_id}*.jsonl")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _codex_thread_from_state_db(root: Path, session_id: str) -> dict[str, Any]:
    db_path = root / "state_5.sqlite"
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
        if not table:
            con.close()
            return {}
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(threads)").fetchall()}
        wanted = [
            "id",
            "name",
            "title",
            "cwd",
            "created_at",
            "updated_at",
            "created_at_ms",
            "updated_at_ms",
            "rollout_path",
            "preview",
            "first_user_message",
        ]
        selected = [column for column in wanted if column in columns]
        if "id" not in selected:
            con.close()
            return {}
        row = con.execute(
            f"SELECT {', '.join(selected)} FROM threads WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return dict(row)


def _codex_session_index(root: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(root / "session_index.jsonl"):
        if isinstance(row, dict) and row.get("id"):
            index[str(row["id"])] = row
    return index


def _load_codex_metadata_limited(root: Path, index: dict[str, dict[str, Any]], limit: int) -> list[Session]:
    sessions: dict[str, Session] = {}
    for session in _codex_metadata_from_state_db(root, limit):
        sessions[session.session_id] = session
        if len(sessions) >= limit:
            return list(sessions.values())

    for sid, meta in sorted(index.items(), key=lambda item: timestamp_sort_key(item[1].get("updated_at")), reverse=True):
        if sid in sessions:
            continue
        sessions[sid] = Session(
            source="codex",
            session_id=sid,
            title=str(meta.get("thread_name") or sid),
            updated_at=meta.get("updated_at"),
        )
        if len(sessions) >= limit:
            return list(sessions.values())

    rollout_ids = _codex_rollout_ids(root, index)
    for row in iter_jsonl(root / "history.jsonl"):
        if not isinstance(row, dict) or not row.get("session_id"):
            continue
        sid = str(row["session_id"])
        if sid in sessions:
            continue
        sessions[sid] = Session(
            source="codex",
            session_id=sid,
            title=str(index.get(sid, {}).get("thread_name") or text_from_any(row.get("text")) or sid),
            updated_at=index.get(sid, {}).get("updated_at") or row.get("ts"),
            metadata={} if sid in rollout_ids else {"transcript": "missing"},
        )
        if len(sessions) >= limit:
            return list(sessions.values())

    candidates: list[tuple[float, Path]] = []
    sessions_root = root / "sessions"
    if sessions_root.exists():
        for path in sessions_root.rglob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for mtime, path in sorted(candidates, reverse=True):
        sid = _codex_id_from_path(path, index)
        if sid in sessions:
            continue
        meta = index.get(sid, {})
        sessions[sid] = Session(
            source="codex",
            session_id=sid,
            title=str(meta.get("thread_name") or sid),
            updated_at=meta.get("updated_at") or int(mtime * 1000),
            raw_path=path,
        )
        if len(sessions) >= limit:
            break
    return list(sessions.values())


def _codex_metadata_from_state_db(root: Path, limit: int) -> list[Session]:
    db_path = root / "state_5.sqlite"
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(db_path, timeout=10)
        con.row_factory = sqlite3.Row
        table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
        if not table:
            con.close()
            return []
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(threads)").fetchall()}
        wanted = ["id", "name", "title", "cwd", "created_at", "updated_at", "created_at_ms", "updated_at_ms", "rollout_path", "preview"]
        selected = [column for column in wanted if column in columns]
        if "id" not in selected:
            con.close()
            return []
        order = "updated_at_ms" if "updated_at_ms" in columns else "updated_at" if "updated_at" in columns else "rowid"
        rows = con.execute(
            f"SELECT {', '.join(selected)} FROM threads ORDER BY {order} DESC LIMIT ?",
            (limit,),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    sessions: list[Session] = []
    for row in rows:
        item = dict(row)
        sid = str(item.get("id") or "")
        if not sid:
            continue
        sessions.append(
            Session(
                source="codex",
                session_id=sid,
                title=str(item.get("name") or item.get("title") or item.get("preview") or sid),
                project_path=item.get("cwd"),
                created_at=item.get("created_at_ms") or item.get("created_at"),
                updated_at=item.get("updated_at_ms") or item.get("updated_at"),
                raw_path=Path(str(item["rollout_path"])) if item.get("rollout_path") else None,
            )
        )
    return sessions


def _codex_rollout_ids(root: Path, index: dict[str, dict[str, Any]]) -> set[str]:
    sessions_root = root / "sessions"
    ids: set[str] = set()
    for folder in (sessions_root, root / "archived_sessions"):
        if folder.exists():
            ids.update(_codex_id_from_path(path, index) for path in folder.rglob("*.jsonl"))
    return ids


def _codex_id_from_path(path: Path, index: dict[str, dict[str, Any]]) -> str:
    name = path.stem
    for sid in index:
        if sid in name:
            return sid
    match = _UUID_RE.search(name)
    if match:
        return match.group(0)
    stripped = re.sub(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-", "", name)
    if stripped:
        return stripped
    return name


def load_claude_sessions(home: Path, metadata_only: bool = False, limit: int | None = None) -> list[Session]:
    root = resolve_paths(home).claude_home
    history = _claude_history_index(root)
    if metadata_only and limit:
        return _load_claude_metadata_limited(root, history, limit)

    sessions: dict[str, Session] = {}
    projects = root / "projects"
    if projects.exists():
        for path in sorted(projects.rglob("*.jsonl")):
            if "subagents" in path.parts:
                continue
            sid = path.stem
            meta = history.get(sid)
            session = Session(
                source="claude",
                session_id=sid,
                title=(meta.title if meta else "") or sid,
                project_path=meta.project if meta else None,
                created_at=meta.created_at if meta else None,
                updated_at=meta.updated_at if meta else None,
                raw_path=path,
            )
            if metadata_only:
                titles = _claude_tail_titles(path)
                session.title = titles.get("custom") or titles.get("ai") or session.title
            else:
                _fill_claude_session(session, path, meta)
            sessions[sid] = session
    for sid, meta in history.items():
        if sid in sessions or not meta.prompts:
            continue
        sessions[sid] = Session(
            source="claude",
            session_id=sid,
            title=meta.title or sid,
            project_path=meta.project,
            updated_at=meta.updated_at,
            messages=[] if metadata_only else [Message(role="user", text=prompt, timestamp=ts) for prompt, ts in meta.prompts],
            metadata={"transcript": "missing"},
        )
    return list(sessions.values())


def load_claude_session(home: Path, session_id: str) -> Session | None:
    root = resolve_paths(home).claude_home
    meta = _claude_history_index(root).get(session_id)
    path = _find_claude_project_file(root, session_id)
    if not path and not meta:
        return None
    session = Session(
        source="claude",
        session_id=session_id,
        title=(meta.title if meta else "") or session_id,
        project_path=meta.project if meta else None,
        created_at=meta.created_at if meta else None,
        updated_at=meta.updated_at if meta else None,
        raw_path=path,
    )
    if path:
        _fill_claude_session(session, path, meta)
    elif meta:
        session.messages = [Message(role="user", text=prompt, timestamp=ts) for prompt, ts in meta.prompts]
        session.metadata = {"transcript": "missing"}
    return session


@dataclass
class _ClaudeHistory:
    project: str | None = None
    created_at: Any = None
    updated_at: Any = None
    title: str = ""
    prompts: list[tuple[str, Any]] = field(default_factory=list)


def _claude_history_index(root: Path) -> dict[str, _ClaudeHistory]:
    """sessionId -> project, last activity and the first real prompt (never a /command)."""
    index: dict[str, _ClaudeHistory] = {}
    for row in iter_jsonl(root / "history.jsonl"):
        if not isinstance(row, dict) or not row.get("sessionId"):
            continue
        meta = index.setdefault(str(row["sessionId"]), _ClaudeHistory())
        meta.project = row.get("project") or meta.project
        meta.created_at = meta.created_at or row.get("timestamp")
        meta.updated_at = row.get("timestamp") or meta.updated_at
        prompt = text_from_any(row.get("display")).strip()
        if _is_meaningful_prompt(prompt):
            meta.prompts.append((prompt, row.get("timestamp")))
            if not meta.title:
                meta.title = _title_from_prompt(prompt)
    return index


def _is_meaningful_prompt(text: str) -> bool:
    return bool(text) and not _looks_like_slash_command(text) and not text.startswith(CLAUDE_NOISE_PREFIXES)


def _looks_like_slash_command(text: str) -> bool:
    """"/compact keep it short" is a command; "/home/me/app 这是什么" is a prompt about a path."""
    first = text.split(maxsplit=1)[0] if text.strip() else ""
    return first.startswith("/") and len(first) > 1 and "/" not in first[1:]


def _title_from_prompt(text: str, limit: int = 120) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _fill_claude_session(session: Session, path: Path, meta: _ClaudeHistory | None) -> None:
    messages, titles, cwd = claude_transcript_messages(iter_jsonl(path))
    session.messages = messages
    if not session.project_path and cwd:
        session.project_path = cwd
    first_prompt = next((m.text for m in messages if m.role == "user" and _is_meaningful_prompt(m.text)), "")
    session.title = (
        titles.get("custom")
        or titles.get("ai")
        or titles.get("summary")
        or (meta.title if meta else "")
        or (_title_from_prompt(first_prompt) if first_prompt else "")
        or session.session_id
    )
    if not session.created_at and messages:
        session.created_at = messages[0].timestamp


# User-turn text Claude Code generates itself (slash commands, local command output, notices).
CLAUDE_NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<task-notification>",
    "<system-reminder>",
    "Caveat: The messages below were generated by the user while running local commands",
)


def claude_transcript_messages(rows: Any) -> tuple[list[Message], dict[str, str], str | None]:
    """Parse Claude Code transcript rows into messages, title candidates and cwd."""
    entries: list[Message | ToolCall] = []
    pending: dict[str, ToolCall] = {}
    titles: dict[str, str] = {}
    cwd: str | None = None
    last_text_message_id: str | None = None
    last_user_shell: ToolCall | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "custom-title" and row.get("customTitle"):
            titles["custom"] = str(row["customTitle"])
        elif kind == "ai-title" and row.get("aiTitle"):
            titles["ai"] = str(row["aiTitle"])
        elif kind == "summary" and row.get("summary"):
            titles.setdefault("summary", str(row["summary"]))
        if kind not in {"user", "assistant"}:
            continue
        if row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary") or row.get("isApiErrorMessage"):
            continue
        if not cwd and row.get("cwd"):
            cwd = str(row["cwd"])
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else message if message is not None else row.get("content")
        timestamp = row.get("timestamp")
        if kind == "user":
            last_text_message_id = None
            if isinstance(content, list):
                texts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        texts.append(str(block.get("text") or ""))
                    elif block_type == "tool_result":
                        call = pending.get(str(block.get("tool_use_id") or ""))
                        if call is not None:
                            call.add_output(toolcalls.output_text(block.get("content")))
                            call.is_error = bool(block.get("is_error"))
                    elif block_type in {"image", "document"}:
                        texts.append(text_from_any({"type": "image", **block}) if block_type == "image" else "[Document attachment not imported]")
                text = "\n".join(part for part in texts if part.strip())
            else:
                text = text_from_any(content)
            text = text.strip()
            if text.startswith("<bash-input>"):
                last_user_shell = toolcalls.shell_call(_between(text, "<bash-input>", "</bash-input>"), name="user_shell")
                last_user_shell.timestamp = timestamp
                entries.append(last_user_shell)
            elif text.startswith("<bash-stdout>") or text.startswith("<bash-stderr>"):
                if last_user_shell is not None:
                    last_user_shell.add_output(_between(text, "<bash-stdout>", "</bash-stdout>"))
                    last_user_shell.add_output(_between(text, "<bash-stderr>", "</bash-stderr>"))
            elif _is_meaningful_prompt(text):
                entries.append(Message(role="user", text=text, timestamp=timestamp))
            continue

        message_id = str(message.get("id") or "") if isinstance(message, dict) else ""
        blocks = content if isinstance(content, list) else [{"type": "text", "text": text_from_any(content)}]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                previous = entries[-1] if entries else None
                if (
                    message_id
                    and message_id == last_text_message_id
                    and isinstance(previous, Message)
                    and previous.role == "assistant"
                ):
                    previous.text = f"{previous.text}\n\n{text}"
                else:
                    entries.append(Message(role="assistant", text=text, timestamp=timestamp))
                last_text_message_id = message_id
            elif block_type in {"tool_use", "server_tool_use"}:
                call = toolcalls.claude_tool_call(str(block.get("name") or "tool"), block.get("input"))
                call.timestamp = timestamp
                entries.append(call)
                if block.get("id"):
                    pending[str(block["id"])] = call
                last_text_message_id = None
    return _finalize_entries(entries), titles, cwd


def _claude_tail_titles(path: Path, max_bytes: int = 262144) -> dict[str, str]:
    """Latest custom/AI title rows near the end of a transcript, without reading it all."""
    titles: dict[str, str] = {}
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_bytes))
            chunk = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return titles
    lines = chunk.splitlines()
    if len(chunk) >= max_bytes and lines:
        lines = lines[1:]  # first line is probably cut in half
    for line in lines:
        if '"custom-title"' not in line and '"ai-title"' not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "custom-title" and row.get("customTitle"):
            titles["custom"] = str(row["customTitle"])
        elif row.get("type") == "ai-title" and row.get("aiTitle"):
            titles["ai"] = str(row["aiTitle"])
    return titles


def _find_claude_project_file(root: Path, session_id: str) -> Path | None:
    projects = root / "projects"
    if not projects.exists():
        return None
    for path in projects.rglob(f"{session_id}.jsonl"):
        if "subagents" not in path.parts:
            return path
    return None


def _claude_transcript_path(root: Path, session_id: str, project: str | None) -> Path | None:
    if project:
        candidate = root / "projects" / project_to_claude_slug(project) / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def _load_claude_metadata_limited(root: Path, history: dict[str, _ClaudeHistory], limit: int) -> list[Session]:
    sessions: dict[str, Session] = {}
    for sid, meta in sorted(history.items(), key=lambda item: timestamp_sort_key(item[1].updated_at), reverse=True):
        path = _claude_transcript_path(root, sid, meta.project)
        if path is None and not meta.prompts:
            continue  # e.g. a session where only "/resume" was typed: nothing to import
        titles = _claude_tail_titles(path) if path else {}
        sessions[sid] = Session(
            source="claude",
            session_id=sid,
            title=titles.get("custom") or titles.get("ai") or meta.title or sid,
            project_path=meta.project,
            created_at=meta.created_at,
            updated_at=meta.updated_at,
            raw_path=path,
            metadata={} if path else {"transcript": "missing"},
        )
        if len(sessions) >= limit:
            return list(sessions.values())

    candidates: list[tuple[float, Path]] = []
    projects = root / "projects"
    if projects.exists():
        for path in projects.rglob("*.jsonl"):
            if "subagents" in path.parts:
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for mtime, path in sorted(candidates, reverse=True):
        sid = path.stem
        if sid in sessions:
            continue
        meta = history.get(sid)
        titles = _claude_tail_titles(path)
        sessions[sid] = Session(
            source="claude",
            session_id=sid,
            title=titles.get("custom") or titles.get("ai") or (meta.title if meta else "") or sid,
            project_path=meta.project if meta else None,
            created_at=meta.created_at if meta else None,
            updated_at=(meta.updated_at if meta else None) or int(mtime * 1000),
            raw_path=path,
        )
        if len(sessions) >= limit:
            break
    return list(sessions.values())
