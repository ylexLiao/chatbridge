from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import Artifact, Message, Session
from .paths import resolve_paths
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
    db_path = root / "state_5.sqlite"
    if db_path.exists():
        try:
            con = sqlite3.connect(db_path, timeout=10)
            table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='threads'").fetchone()
            if table:
                columns = {str(row[1]) for row in con.execute("PRAGMA table_info(threads)").fetchall()}
                if project and "cwd" in columns:
                    count = con.execute("SELECT COUNT(*) FROM threads WHERE cwd = ?", (project,)).fetchone()[0]
                else:
                    count = con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
                con.close()
                return int(count)
            con.close()
        except sqlite3.Error:
            pass
    index_ids = {str(row["id"]) for row in iter_jsonl(root / "session_index.jsonl") if isinstance(row, dict) and row.get("id")}
    if index_ids and not project:
        return len(index_ids)
    sessions_root = root / "sessions"
    if not sessions_root.exists():
        return len(index_ids)
    ids = set(index_ids)
    index = _codex_session_index(root)
    for path in sessions_root.rglob("*.jsonl"):
        ids.add(_codex_id_from_path(path, index))
    if project:
        return len([session for session in load_codex_sessions(home, metadata_only=True) if session.project_path == project])
    return len(ids)


def count_claude_sessions(home: Path, project: str | None = None) -> int:
    root = resolve_paths(home).claude_home
    history_ids: set[str] = set()
    for row in iter_jsonl(root / "history.jsonl"):
        if not isinstance(row, dict) or not row.get("sessionId"):
            continue
        if project and row.get("project") != project:
            continue
        history_ids.add(str(row["sessionId"]))
    if project:
        project_dir = root / "projects" / project_to_claude_slug(project)
        if project_dir.exists():
            for path in project_dir.glob("*.jsonl"):
                history_ids.add(path.stem)
        return len(history_ids)
    projects = root / "projects"
    if projects.exists():
        for path in projects.rglob("*.jsonl"):
            if "subagents" not in path.parts:
                history_ids.add(path.stem)
    return len(history_ids)


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
        title=str(meta.get("thread_name") or meta.get("title") or meta.get("preview") or session_id),
        project_path=meta.get("cwd"),
        created_at=meta.get("created_at_ms") or meta.get("created_at"),
        updated_at=meta.get("updated_at_ms") or meta.get("updated_at"),
        raw_path=Path(str(meta["rollout_path"])) if meta.get("rollout_path") else None,
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
        title=str(meta.get("thread_name") or meta.get("title") or meta.get("preview") or sid),
        project_path=meta.get("cwd"),
        created_at=meta.get("created_at_ms") or meta.get("created_at"),
        updated_at=meta.get("updated_at_ms") or meta.get("updated_at"),
        raw_path=path,
    )
    if metadata_only:
        return session
    for row in iter_jsonl(path):
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "session_meta":
            session.project_path = str(payload.get("cwd") or session.project_path or "") or None
            session.created_at = payload.get("timestamp") or session.created_at
        elif row.get("type") == "turn_context" and not session.project_path:
            session.project_path = str(payload.get("cwd") or "") or None
        role = payload.get("role") or payload.get("type")
        text = text_from_any(payload.get("content") or payload.get("text") or payload.get("message"))
        if role in {"message", "agent_message"}:
            role = payload.get("role") or "assistant"
        if text:
            session.messages.append(Message(role=str(role or "assistant"), text=text, timestamp=row.get("ts") or row.get("timestamp")))
    return session


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
        wanted = ["id", "title", "cwd", "created_at", "updated_at", "created_at_ms", "updated_at_ms", "rollout_path", "preview"]
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
                title=str(item.get("title") or item.get("preview") or sid),
                project_path=item.get("cwd"),
                created_at=item.get("created_at_ms") or item.get("created_at"),
                updated_at=item.get("updated_at_ms") or item.get("updated_at"),
                raw_path=Path(str(item["rollout_path"])) if item.get("rollout_path") else None,
            )
        )
    return sessions


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
    history: dict[str, dict[str, Any]] = {}
    for row in iter_jsonl(root / "history.jsonl"):
        if isinstance(row, dict) and row.get("sessionId"):
            history[str(row["sessionId"])] = row
    if metadata_only and limit:
        return _load_claude_metadata_limited(root, history, limit)

    sessions: dict[str, Session] = {}
    projects = root / "projects"
    if projects.exists():
        for path in sorted(projects.rglob("*.jsonl")):
            if "subagents" in path.parts:
                continue
            sid = path.stem
            meta = history.get(sid, {})
            project = meta.get("project")
            session = Session(
                source="claude",
                session_id=sid,
                title=str(meta.get("display") or sid),
                project_path=project,
                updated_at=meta.get("timestamp"),
                raw_path=path,
            )
            if not metadata_only:
                for row in iter_jsonl(path):
                    if not isinstance(row, dict):
                        continue
                    if not session.project_path and row.get("cwd"):
                        session.project_path = str(row["cwd"])
                    role = row.get("type") or row.get("role") or "assistant"
                    text = text_from_any(row.get("message") or row.get("content") or row.get("text"))
                    if text:
                        session.messages.append(Message(role=str(role), text=text, timestamp=row.get("timestamp")))
            sessions[sid] = session
    seen_texts: dict[str, set[str]] = {}
    for sid, row in history.items():
        session = sessions.setdefault(
            sid,
            Session(source="claude", session_id=sid, title=str(row.get("display") or sid), project_path=row.get("project"), updated_at=row.get("timestamp")),
        )
        text = text_from_any(row.get("display"))
        if not text:
            continue
        seen = seen_texts.get(sid)
        if seen is None:
            seen = {message.text for message in session.messages}
            seen_texts[sid] = seen
        if text not in seen:
            seen.add(text)
            session.messages.append(Message(role="user", text=text, timestamp=row.get("timestamp")))
    return list(sessions.values())


def load_claude_session(home: Path, session_id: str) -> Session | None:
    root = resolve_paths(home).claude_home
    history = {
        str(row["sessionId"]): row
        for row in iter_jsonl(root / "history.jsonl")
        if isinstance(row, dict) and row.get("sessionId")
    }
    meta = history.get(session_id, {})
    path = _find_claude_project_file(root, session_id)
    if not path and not meta:
        return None
    session = Session(
        source="claude",
        session_id=session_id,
        title=str(meta.get("display") or session_id),
        project_path=meta.get("project"),
        updated_at=meta.get("timestamp"),
        raw_path=path,
    )
    if path:
        for row in iter_jsonl(path):
            if not isinstance(row, dict):
                continue
            if not session.project_path and row.get("cwd"):
                session.project_path = str(row["cwd"])
            role = row.get("type") or row.get("role") or "assistant"
            text = text_from_any(row.get("message") or row.get("content") or row.get("text"))
            if text:
                session.messages.append(Message(role=str(role), text=text, timestamp=row.get("timestamp")))
    text = text_from_any(meta.get("display"))
    if text and not any(message.text == text for message in session.messages):
        session.messages.append(Message(role="user", text=text, timestamp=meta.get("timestamp")))
    return session


def _find_claude_project_file(root: Path, session_id: str) -> Path | None:
    projects = root / "projects"
    if not projects.exists():
        return None
    for path in projects.rglob(f"{session_id}.jsonl"):
        if "subagents" not in path.parts:
            return path
    return None


def _load_claude_metadata_limited(root: Path, history: dict[str, dict[str, Any]], limit: int) -> list[Session]:
    sessions: dict[str, Session] = {}
    for sid, row in sorted(history.items(), key=lambda item: timestamp_sort_key(item[1].get("timestamp")), reverse=True):
        sessions[sid] = Session(
            source="claude",
            session_id=sid,
            title=str(row.get("display") or sid),
            project_path=row.get("project"),
            updated_at=row.get("timestamp"),
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
        meta = history.get(sid, {})
        sessions[sid] = Session(
            source="claude",
            session_id=sid,
            title=str(meta.get("display") or sid),
            project_path=meta.get("project"),
            updated_at=meta.get("timestamp") or int(mtime * 1000),
            raw_path=path,
        )
        if len(sessions) >= limit:
            break
    return list(sessions.values())
