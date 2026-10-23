from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import Artifact, Message, Session
from .util import approx_tokens, redact, sanitize_embedded_images

BUNDLE_FORMAT = "chatbridge-bundle"
BUNDLE_VERSION = 1
BUNDLE_GLOB = "chatbridge-export-*.json"
# Bytes read from the start of a bundle to list it without parsing the whole file.
HEADER_BYTES = 65536


def default_bundle_name(session: Session) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(session.session_id)).strip("-") or "session"
    return f"chatbridge-export-{session.source}-{safe_id}.json"


def default_export_dir(home: Path) -> Path:
    """Where the TUI writes bundles (and the first place it looks for them)."""
    return home / ".chatbridge" / "exports"


def session_stats(session: Session) -> dict[str, Any]:
    roles = Counter(message.role for message in session.messages)
    tools = Counter(message.tool or "tool" for message in session.messages if message.role == "tool")
    return {
        "messages": len(session.messages),
        "user": roles.get("user", 0),
        "assistant": roles.get("assistant", 0),
        "toolCalls": roles.get("tool", 0),
        "tools": dict(tools.most_common()),
        "artifacts": len(session.artifacts),
        "approxTokens": sum(approx_tokens(message.text) for message in session.messages),
    }


def build_bundle(session: Session) -> dict[str, Any]:
    # "stats" sits before "session" so listing tools can read it from the first few KB.
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "chatbridgeVersion": __version__,
        "stats": session_stats(session),
        "session": {
            "source": session.source,
            "sessionId": session.session_id,
            "title": session.title,
            "projectPath": session.project_path,
            "createdAt": session.created_at,
            "updatedAt": session.updated_at,
            "messages": [_message_row(message) for message in session.messages],
            "artifacts": [
                {"kind": artifact.kind, "text": _clean_text(artifact.text), "path": artifact.path}
                for artifact in session.artifacts
            ],
            "metadata": session.metadata if isinstance(session.metadata, dict) else {},
        },
    }


def _message_row(message: Message) -> dict[str, Any]:
    row: dict[str, Any] = {"role": message.role, "text": _clean_text(message.text), "timestamp": message.timestamp}
    if message.tool:
        row["tool"] = message.tool
    return row


def write_bundle(session: Session, out: str | Path | None = None, cwd_default: Path | None = None) -> Path:
    if out is not None:
        raw = str(out)
        target = Path(raw).expanduser()
        # A trailing separator means "this directory", even if it does not exist yet.
        if target.is_dir() or raw.endswith(("/", "\\")):
            target = target / default_bundle_name(session)
    else:
        target = (cwd_default or Path.cwd()) / default_bundle_name(session)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_bundle(session), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_bundle(path: Path) -> Session:
    bundle_path = Path(path).expanduser()
    try:
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Bundle file not found: {bundle_path}") from None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit(f"Could not read bundle {bundle_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
        raise SystemExit(f"Not a ChatBridge bundle (expected format={BUNDLE_FORMAT!r}): {bundle_path}")
    version = data.get("version")
    if not isinstance(version, int) or version > BUNDLE_VERSION:
        raise SystemExit(f"Unsupported bundle version {version!r} (this ChatBridge supports up to {BUNDLE_VERSION}). Update ChatBridge and retry.")
    payload = data.get("session")
    if not isinstance(payload, dict):
        raise SystemExit(f"Bundle has no session payload: {bundle_path}")
    messages = [
        Message(
            role=str(row.get("role") or ""),
            text=str(row.get("text") or ""),
            timestamp=row.get("timestamp"),
            tool=str(row["tool"]) if row.get("tool") else None,
        )
        for row in payload.get("messages") or []
        if isinstance(row, dict)
    ]
    artifacts = [
        Artifact(kind=str(row.get("kind") or "artifact"), text=str(row.get("text") or ""), path=row.get("path"))
        for row in payload.get("artifacts") or []
        if isinstance(row, dict)
    ]
    metadata = payload.get("metadata")
    return Session(
        source=str(payload.get("source") or "bundle"),
        session_id=str(payload.get("sessionId") or bundle_path.stem),
        title=str(payload.get("title") or "Untitled"),
        project_path=payload.get("projectPath"),
        created_at=payload.get("createdAt"),
        updated_at=payload.get("updatedAt"),
        messages=messages,
        artifacts=artifacts,
        metadata=metadata if isinstance(metadata, dict) else {},
        raw_path=bundle_path,
    )


def bundle_search_dirs(home: Path, cwd: Path | None = None) -> list[Path]:
    """Places a bundle usually lands: the TUI export dir, where you are, Downloads, home."""
    candidates = [default_export_dir(home), cwd or Path.cwd(), home / "Downloads", home]
    seen: set[str] = set()
    dirs: list[Path] = []
    for candidate in candidates:
        try:
            key = str(candidate.expanduser().resolve())
        except OSError:
            key = str(candidate.expanduser().absolute())
        if key not in seen:
            seen.add(key)
            dirs.append(candidate.expanduser())
    return dirs


def list_bundles(dirs: list[Path], limit: int = 100) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for directory in dirs:
        try:
            paths = list(directory.glob(BUNDLE_GLOB)) if directory.is_dir() else []
        except OSError:
            continue
        for path in paths:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path.absolute())
            if key in found:
                continue
            header = read_bundle_header(path)
            if header is not None:
                found[key] = header
    rows = sorted(found.values(), key=lambda row: row.get("modified") or 0, reverse=True)
    return rows[:limit]


def read_bundle_header(path: Path) -> dict[str, Any] | None:
    """Summary of a bundle from its first bytes (None if it is not a ChatBridge bundle)."""
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            chunk = handle.read(HEADER_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None
    if f'"{BUNDLE_FORMAT}"' not in chunk:
        return None
    stats = _header_object(chunk, "stats")
    return {
        "path": str(path),
        "name": path.name,
        "size": stat.st_size,
        "modified": int(stat.st_mtime * 1000),
        "exportedAt": _header_string(chunk, "exportedAt"),
        "chatbridgeVersion": _header_string(chunk, "chatbridgeVersion"),
        "source": _header_string(chunk, "source"),
        "sessionId": _header_string(chunk, "sessionId"),
        "title": _header_string(chunk, "title") or path.stem,
        "projectPath": _header_string(chunk, "projectPath"),
        "stats": stats if isinstance(stats, dict) else None,
    }


def _header_string(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _header_object(text: str, key: str) -> Any:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\{{', text)
    if not match:
        return None
    depth = 0
    start = match.end() - 1
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _clean_text(text: Any) -> str:
    return sanitize_embedded_images(redact(str(text or "")))
