from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import Artifact, Message, Session
from .util import redact, sanitize_embedded_images

BUNDLE_FORMAT = "chatbridge-bundle"
BUNDLE_VERSION = 1


def default_bundle_name(session: Session) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(session.session_id)).strip("-") or "session"
    return f"chatbridge-export-{session.source}-{safe_id}.json"


def build_bundle(session: Session) -> dict[str, Any]:
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "chatbridgeVersion": __version__,
        "session": {
            "source": session.source,
            "sessionId": session.session_id,
            "title": session.title,
            "projectPath": session.project_path,
            "createdAt": session.created_at,
            "updatedAt": session.updated_at,
            "messages": [
                {"role": message.role, "text": _clean_text(message.text), "timestamp": message.timestamp}
                for message in session.messages
            ],
            "artifacts": [
                {"kind": artifact.kind, "text": _clean_text(artifact.text), "path": artifact.path}
                for artifact in session.artifacts
            ],
            "metadata": session.metadata if isinstance(session.metadata, dict) else {},
        },
    }


def write_bundle(session: Session, out: Path | None = None, cwd_default: Path | None = None) -> Path:
    if out is not None:
        target = Path(out).expanduser()
        if target.is_dir():
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
    except (OSError, json.JSONDecodeError) as exc:
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
        Message(role=str(row.get("role") or ""), text=str(row.get("text") or ""), timestamp=row.get("timestamp"))
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


def _clean_text(text: Any) -> str:
    return sanitize_embedded_images(redact(str(text or "")))
