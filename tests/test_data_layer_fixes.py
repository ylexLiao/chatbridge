"""Focused tests for the data-layer fixes (parsers/util/summary)."""

import json
import os
import tempfile
import unittest
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


class DataLayerFixesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env: dict[str, str] = {}
        for key in (
            "CHATBRIDGE_COPILOT_WORKSPACE_STORAGE",
            "CHATBRIDGE_CODEX_HOME",
            "CHATBRIDGE_CLAUDE_HOME",
            "CODEX_HOME",
            "CLAUDE_HOME",
        ):
            if key in os.environ:
                self._saved_env[key] = os.environ.pop(key)

    def tearDown(self) -> None:
        os.environ.update(self._saved_env)

    def _copilot_home(self, tmp_path: Path) -> tuple[Path, Path]:
        home = tmp_path / "home"
        ws = home / ".config/Code/User/workspaceStorage/ws1"
        write_json(ws / "workspace.json", {"folder": "file:///repo/app"})
        write_json(
            ws / "chatSessions/copilot-dedup.json",
            {
                "sessionId": "copilot-dedup",
                "customTitle": "Dedup session",
                "creationDate": 1700000000000,
                "lastMessageDate": 1700000005000,
                "requests": [
                    {
                        "timestamp": 1700000001000,
                        "message": {"text": "Fix bug"},
                        "response": [{"value": "Patched"}],
                    }
                ],
            },
        )
        write_jsonl(
            ws / "GitHub.copilot-chat/transcripts/copilot-dedup.jsonl",
            [
                {"role": "user", "content": "Fix bug"},
                {"role": "assistant", "content": "Patched"},
                {"role": "assistant", "content": "transcript-only answer"},
            ],
        )
        return home, ws

    def test_copilot_transcript_rows_contribute_exactly_once(self) -> None:
        from chatbridge.parsers import load_copilot_session, load_copilot_sessions

        with tempfile.TemporaryDirectory() as temp_dir:
            home, _ = self._copilot_home(Path(temp_dir))

            direct = load_copilot_session(home, "copilot-dedup")
            self.assertIsNotNone(direct)
            texts = [(m.role, m.text) for m in direct.messages]
            self.assertEqual(len(texts), len(set(texts)), f"duplicate messages: {texts}")
            # Requests come first, transcript-only extras follow.
            self.assertEqual(
                texts,
                [
                    ("user", "Fix bug"),
                    ("assistant", "Patched"),
                    ("assistant", "transcript-only answer"),
                ],
            )

            listed = {s.session_id: s for s in load_copilot_sessions(home)}
            listed_texts = [(m.role, m.text) for m in listed["copilot-dedup"].messages]
            self.assertEqual(listed_texts, texts)

    def test_copilot_transcript_only_session_has_no_duplicates(self) -> None:
        from chatbridge.parsers import load_copilot_session

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            ws = home / ".config/Code/User/workspaceStorage/ws1"
            write_json(ws / "workspace.json", {"folder": "file:///repo/app"})
            write_jsonl(
                ws / "GitHub.copilot-chat/transcripts/only-transcript.jsonl",
                [
                    {"role": "user", "content": "transcript request"},
                    {"role": "assistant", "content": "transcript answer"},
                ],
            )

            session = load_copilot_session(home, "only-transcript")
            self.assertIsNotNone(session)
            texts = [(m.role, m.text) for m in session.messages]
            self.assertEqual(
                texts,
                [("user", "transcript request"), ("assistant", "transcript answer")],
            )

    def test_codex_id_from_path_keeps_full_uuid(self) -> None:
        from chatbridge.parsers import _codex_id_from_path

        uuid_name = "rollout-2026-01-02T03-04-05-018f6c2e-1234-4abc-8def-0123456789ab.jsonl"
        self.assertEqual(
            _codex_id_from_path(Path(uuid_name), {}),
            "018f6c2e-1234-4abc-8def-0123456789ab",
        )
        # Non-UUID names: strip the rollout-<timestamp>- prefix only.
        self.assertEqual(
            _codex_id_from_path(Path("rollout-2026-01-02T03-04-05-mysession.jsonl"), {}),
            "mysession",
        )
        # Plain names pass through untouched (no last-dash-token truncation).
        self.assertEqual(_codex_id_from_path(Path("random-file.jsonl"), {}), "random-file")
        # Index ids still win when embedded in the name.
        self.assertEqual(
            _codex_id_from_path(Path(uuid_name), {"018f6c2e-1234-4abc-8def-0123456789ab": {}}),
            "018f6c2e-1234-4abc-8def-0123456789ab",
        )

    def test_timestamp_sort_key_orders_mixed_formats(self) -> None:
        from chatbridge.util import parse_timestamp, timestamp_sort_key

        values = [
            1735689600000,  # 2025-01-01T00:00:00Z (epoch ms)
            None,
            "2024-01-01T00:00:00Z",  # ISO string
            1700000000,  # 2023-11-14T22:13:20Z (epoch seconds)
            "",
        ]
        ordered = sorted(values, key=timestamp_sort_key)
        self.assertEqual(
            ordered,
            [None, "", 1700000000, "2024-01-01T00:00:00Z", 1735689600000],
        )
        # Digit strings parse like their numeric counterparts.
        self.assertEqual(parse_timestamp("1735689600000"), parse_timestamp(1735689600000))
        self.assertEqual(timestamp_sort_key(None), float("-inf"))
        self.assertEqual(timestamp_sort_key("not a timestamp"), float("-inf"))

    def test_image_data_url_placeholder_preserves_following_prose(self) -> None:
        from chatbridge.util import sanitize_embedded_images

        text = "before data:image/png;base64,AAAABBBB after words"
        result = sanitize_embedded_images(text)
        self.assertIn(
            "[Image attachment not imported: embedded PNG data URL, approx 6 B]",
            result,
        )
        self.assertTrue(result.startswith("before "))
        self.assertTrue(result.endswith(" after words"), result)
        self.assertNotIn("AAAABBBB", result)

        # Newline-wrapped payloads are still one image; size ignores the wraps.
        wrapped = sanitize_embedded_images("data:image/png;base64,AAAA\r\nBBBB")
        self.assertEqual(
            wrapped,
            "[Image attachment not imported: embedded PNG data URL, approx 6 B]",
        )

    def test_redact_leaves_words_merely_ending_in_pass_alone(self) -> None:
        from chatbridge.util import redact

        text = redact("set compass=north and multipass: ticket for the trip")
        self.assertIn("compass=north", text)
        self.assertIn("multipass: ticket", text)
        self.assertNotIn("[REDACTED]", text)

        # Real keys still redact.
        redacted = redact("pass=secret password: hunter2 api_key: abc123")
        self.assertNotIn("secret", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertIn("pass=[REDACTED]", redacted)
        self.assertIn("password: [REDACTED]", redacted)
        self.assertIn("api_key: [REDACTED]", redacted)

    def test_claude_subagents_excluded_via_path_parts(self) -> None:
        from chatbridge.parsers import (
            _find_claude_project_file,
            count_claude_sessions,
            load_claude_sessions,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            claude_home = home / ".claude"
            write_jsonl(
                claude_home / "projects/-repo-app/main-1.jsonl",
                [{"type": "user", "message": {"content": "main session"}, "cwd": "/repo/app"}],
            )
            write_jsonl(
                claude_home / "projects/-repo-app/subagents/sub-1.jsonl",
                [{"type": "user", "message": {"content": "subagent noise"}, "cwd": "/repo/app"}],
            )

            self.assertEqual(count_claude_sessions(home), 1)
            session_ids = {s.session_id for s in load_claude_sessions(home)}
            self.assertEqual(session_ids, {"main-1"})
            self.assertIsNotNone(_find_claude_project_file(claude_home, "main-1"))
            self.assertIsNone(_find_claude_project_file(claude_home, "sub-1"))

    def test_handoff_keeps_multiline_message_structure(self) -> None:
        from chatbridge.models import Message, Session
        from chatbridge.summary import build_handoff

        session = Session(
            source="copilot",
            session_id="multiline-1",
            title="Multi-line",
            messages=[Message(role="user", text="first line\nsecond line\nthird line")],
        )

        text = build_handoff(session, "codex", level="full")
        self.assertIn("[Handoff: Copilot -> Codex]", text)
        self.assertIn("- user: first line\n  second line\n  third line", text)


if __name__ == "__main__":
    unittest.main()
