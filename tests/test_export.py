from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from test_chatbridge import make_home, run_cli, write_jsonl


class ExportBundleTests(unittest.TestCase):
    def test_export_writes_redacted_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            out = temp / "bundle.json"

            result = run_cli(home, "export", "--from", "copilot", "--session", "copilot-json", "--out", str(out))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Exported Copilot session copilot-json", result.stdout)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["format"], "chatbridge-bundle")
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["session"]["sessionId"], "copilot-json")
            self.assertEqual(data["session"]["projectPath"], "/repo/app")
            text = json.dumps(data, ensure_ascii=False)
            self.assertNotIn("topsecret123", text)
            self.assertIn("[REDACTED]", text)

    def test_export_default_filename_lands_in_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            workdir = temp / "workdir"
            workdir.mkdir()

            result = run_cli(home, "export", "--from", "codex", "--session", "codex-1", cwd=workdir)

            self.assertEqual(result.returncode, 0, result.stderr)
            bundles = list(workdir.glob("chatbridge-export-codex-codex-1.json"))
            self.assertEqual(len(bundles), 1)

    def test_bundle_roundtrip_into_codex_targets_session_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            bundle = temp / "bundle.json"
            run_cli(home, "export", "--from", "copilot", "--session", "copilot-json", "--out", str(bundle))
            elsewhere = temp / "elsewhere"
            elsewhere.mkdir()

            result = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "codex", "--apply", cwd=elsewhere)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Target project: /repo/app", result.stdout)
            sid = next(
                line.split("Imported into Codex session ", 1)[1].strip()
                for line in result.stdout.splitlines()
                if line.startswith("Imported into Codex session ")
            )
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            row = con.execute("SELECT cwd, title FROM threads WHERE id = ?", (sid,)).fetchone()
            con.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "/repo/app")
            self.assertTrue(row[1].startswith("[Imported from Copilot]"))

    def test_bundle_roundtrip_into_claude_targets_session_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            bundle = temp / "bundle.json"
            run_cli(home, "export", "--from", "copilot", "--session", "copilot-json", "--out", str(bundle))
            elsewhere = temp / "elsewhere"
            elsewhere.mkdir()

            result = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "claude", "--apply", cwd=elsewhere)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Target project: /repo/app", result.stdout)
            session_file = next(
                Path(line.split("Session file: ", 1)[1])
                for line in result.stdout.splitlines()
                if line.startswith("Session file: ")
            )
            self.assertEqual(session_file.parent.name, "-repo-app")
            rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("cwd") == "/repo/app" for row in rows if isinstance(row, dict)))

    def test_vscode_remote_bundle_translates_to_local_path_for_claude(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            bundle = temp / "remote-bundle.json"
            bundle.write_text(
                json.dumps(
                    {
                        "format": "chatbridge-bundle",
                        "version": 1,
                        "exportedAt": "2026-06-10T00:00:00Z",
                        "chatbridgeVersion": "1.0.1",
                        "session": {
                            "source": "copilot",
                            "sessionId": "remote-1",
                            "title": "Remote task",
                            "projectPath": "vscode-remote://ssh-remote%2Bvps/home/ubuntu/proj",
                            "createdAt": 1700000000000,
                            "updatedAt": 1700000005000,
                            "messages": [
                                {"role": "user", "text": "Deploy the app", "timestamp": 1700000001000},
                                {"role": "assistant", "text": "Deployed", "timestamp": 1700000002000},
                            ],
                            "artifacts": [],
                            "metadata": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "claude", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Target project: /home/ubuntu/proj", result.stdout)
            session_file = next(
                Path(line.split("Session file: ", 1)[1])
                for line in result.stdout.splitlines()
                if line.startswith("Session file: ")
            )
            self.assertEqual(session_file.parent.name, "-home-ubuntu-proj")
            rows = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("cwd") == "/home/ubuntu/proj" for row in rows if isinstance(row, dict)))
            self.assertIn("Deploy the app", session_file.read_text(encoding="utf-8"))

    def test_vscode_remote_project_kept_verbatim_for_copilot_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            remote_uri = "vscode-remote://ssh-remote%2Bvps/home/ubuntu/proj"
            bundle = temp / "remote-bundle.json"
            bundle.write_text(
                json.dumps(
                    {
                        "format": "chatbridge-bundle",
                        "version": 1,
                        "exportedAt": "2026-06-10T00:00:00Z",
                        "chatbridgeVersion": "1.0.1",
                        "session": {
                            "source": "codex",
                            "sessionId": "remote-2",
                            "title": "Remote task",
                            "projectPath": remote_uri,
                            "createdAt": 1700000000000,
                            "updatedAt": 1700000005000,
                            "messages": [{"role": "user", "text": "Hello", "timestamp": None}],
                            "artifacts": [],
                            "metadata": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = run_cli(home, "native-import", "--bundle", str(bundle), "--to", "copilot", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Target project: {remote_uri}", result.stdout)


class TargetProjectSemanticsTests(unittest.TestCase):
    def test_native_import_targets_session_project_even_from_other_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            elsewhere = temp / "unrelated-cwd"
            elsewhere.mkdir()

            result = run_cli(home, "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply", cwd=elsewhere)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Target project: /repo/app", result.stdout)
            self.assertNotIn(str(elsewhere), result.stdout)
            sid = next(
                line.split("Imported into Codex session ", 1)[1].strip()
                for line in result.stdout.splitlines()
                if line.startswith("Imported into Codex session ")
            )
            con = sqlite3.connect(home / ".codex/state_5.sqlite")
            row = con.execute("SELECT cwd FROM threads WHERE id = ?", (sid,)).fetchone()
            con.close()
            self.assertEqual(row[0], "/repo/app")

    def test_dry_run_reports_target_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))

            result = run_cli(home, "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("DRY RUN"))
            self.assertIn("Target project: /repo/app", result.stdout)

    def test_api_duplicate_error_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = make_home(Path(temp_dir))
            first = run_cli(home, "api", "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(json.loads(first.stdout)["ok"], first.stdout)

            second = run_cli(home, "api", "native-import", "--from", "claude", "--to", "codex", "--session", "claude-1", "--apply")

            self.assertEqual(second.returncode, 0, second.stderr)
            payload = json.loads(second.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["kind"], "duplicate")
            self.assertIn("nextTitle", payload)
            self.assertIn("(1)", payload["nextTitle"])

    def test_api_export_returns_text_with_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            out = temp / "api-bundle.json"

            result = run_cli(home, "api", "export", "--from", "copilot", "--session", "copilot-json", "--out", str(out))

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], result.stdout)
            self.assertIn(str(out), payload["data"]["text"])
            self.assertTrue(out.exists())


class RepairClaudeFidelityTests(unittest.TestCase):
    def test_repair_claude_imports_preserves_structured_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = make_home(temp)
            claude_home = home / ".claude"
            old_sid = "imported-legacy-1"
            title = "[Imported from Codex] Debug server"
            wrong_dir = claude_home / "projects" / "-wrong-place"
            base = {
                "isSidechain": False,
                "userType": "external",
                "cwd": "/wrong/place",
                "sessionId": old_sid,
                "version": "2.1.0",
            }
            write_jsonl(
                wrong_dir / f"{old_sid}.jsonl",
                [
                    {**base, "type": "user", "uuid": "u-1", "parentUuid": None, "message": {"role": "user", "content": "Original question one"}},
                    {**base, "type": "assistant", "uuid": "a-1", "parentUuid": "u-1", "message": {"role": "assistant", "content": [{"type": "text", "text": "Original answer one"}]}},
                    {**base, "type": "user", "uuid": "u-2", "parentUuid": "a-1", "message": {"role": "user", "content": "[Handoff: Codex -> Claude Code] context"}},
                    {"type": "last-prompt", "lastPrompt": title, "leafUuid": "u-2", "sessionId": old_sid},
                ],
            )
            history_path = claude_home / "history.jsonl"
            existing = history_path.read_text(encoding="utf-8")
            history_path.write_text(
                existing
                + json.dumps({"display": title, "timestamp": 1700000050000, "project": "/repo/app", "sessionId": old_sid})
                + "\n",
                encoding="utf-8",
            )

            dry = run_cli(home, "repair-claude-imports", "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn(old_sid, dry.stdout)

            result = run_cli(home, "repair-claude-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            target_dir = claude_home / "projects" / "-repo-app"
            repaired = [
                path
                for path in target_dir.glob("*.jsonl")
                if path.stem != "claude-1" and title in path.read_text(encoding="utf-8")
            ]
            self.assertEqual(len(repaired), 1)
            repaired_text = repaired[0].read_text(encoding="utf-8")
            # The full structured transcript survives — not just the handoff fallback.
            self.assertIn("Original question one", repaired_text)
            self.assertIn("Original answer one", repaired_text)
            rows = [json.loads(line) for line in repaired_text.splitlines()]
            message_rows = [row for row in rows if row.get("type") in {"user", "assistant"}]
            self.assertGreaterEqual(len(message_rows), 3)
            new_sid = str(uuid.UUID(repaired[0].stem))
            self.assertTrue(all(row.get("sessionId") == new_sid for row in message_rows))
            self.assertTrue(all(row.get("cwd") == "/repo/app" for row in message_rows))
            self.assertFalse((wrong_dir / f"{old_sid}.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
