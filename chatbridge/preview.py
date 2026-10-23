"""What is inside a session or bundle, before exporting or importing it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .export import session_stats
from .models import Session
from .summary import tool_summary, touched_files
from .writers import LEVEL_TOKEN_BUDGETS, _vscode_remote_to_local_path, project_transcript


def session_preview(session: Session, level: str = "full") -> dict[str, Any]:
    project = session.project_path or None
    local_project = _vscode_remote_to_local_path(project) if project else None
    check_path = local_project or project
    users = [message for message in session.messages if message.role == "user"]
    replies = [message for message in session.messages if message.role == "assistant"]
    tools = [message for message in session.messages if message.role == "tool"]
    budget = LEVEL_TOKEN_BUDGETS.get(level, LEVEL_TOKEN_BUDGETS["full"])
    projection = project_transcript(session, budget)
    return {
        "source": session.source,
        "sourceLabel": session.source_label,
        "sessionId": session.session_id,
        "title": session.title,
        "projectPath": project,
        "localProject": local_project,
        "projectExists": bool(check_path) and _is_dir(check_path),
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "rawPath": str(session.raw_path) if session.raw_path else None,
        "transcriptMissing": (session.metadata or {}).get("transcript") == "missing",
        "stats": session_stats(session),
        "filesTouched": touched_files(session)[:30],
        "firstPrompt": _snippet(users[0].text) if users else None,
        "lastPrompt": _snippet(users[-1].text) if len(users) > 1 else None,
        "lastReply": _snippet(replies[-1].text, 600) if replies else None,
        "recentTools": [tool_summary(message.text)[:160] for message in tools[-6:]],
        "importEstimate": {
            "level": level,
            "budget": budget,
            "messages": len(projection.messages),
            "approxTokens": projection.approx_tokens,
            "trimmed": bool(projection.notes),
            "notes": projection.notes,
        },
    }


def render_preview_text(preview: dict[str, Any]) -> str:
    stats = preview.get("stats") or {}
    tools = stats.get("tools") or {}
    lines = []
    if preview.get("transcriptMissing"):
        lines.append(f"! {TRANSCRIPT_MISSING_NOTE}")
    lines += [
        f"Title:    {preview.get('title')}",
        f"Source:   {preview.get('sourceLabel')}  ({preview.get('sessionId')})",
        f"Project:  {preview.get('projectPath') or '-'}" + ("" if preview.get("projectExists") else "   [not found on this machine]"),
        f"Messages: {stats.get('user', 0)} user, {stats.get('assistant', 0)} assistant, {stats.get('toolCalls', 0)} tool calls"
        + (f" ({', '.join(f'{name} {count}' for name, count in list(tools.items())[:6])})" if tools else ""),
        f"Size:     ~{stats.get('approxTokens', 0):,} tokens",
    ]
    estimate = preview.get("importEstimate") or {}
    if estimate:
        lines.append(
            f"Import:   ~{estimate.get('approxTokens', 0):,} tokens in {estimate.get('messages', 0)} messages at level {estimate.get('level')}"
            + (" (trimmed: " + "; ".join(estimate.get("notes") or []) + ")" if estimate.get("trimmed") else "")
        )
    files = preview.get("filesTouched") or []
    if files:
        lines.append("")
        lines.append(f"Files touched ({len(files)}):")
        lines.extend(f"  - {path}" for path in files[:12])
    for label, key in (("First prompt", "firstPrompt"), ("Last prompt", "lastPrompt"), ("Last reply", "lastReply")):
        if preview.get(key):
            lines.extend(["", f"{label}:", f"  {preview[key]}"])
    recent = preview.get("recentTools") or []
    if recent:
        lines.extend(["", "Recent tool calls:"])
        lines.extend(f"  - {item}" for item in recent)
    return "\n".join(lines) + "\n"


TRANSCRIPT_MISSING_NOTE = (
    "No transcript file was found for this session, only the prompts recorded in history.jsonl. "
    "Exports and imports will contain just those prompts."
)


def _snippet(text: str, limit: int = 300) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _is_dir(path: str) -> bool:
    try:
        return Path(path).expanduser().is_dir()
    except (OSError, ValueError):
        return False
