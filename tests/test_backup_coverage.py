"""Backup coverage: every destructive --apply flow must populate ~/.chatbridge/backups.

These tests reuse the fixtures from test_chatbridge (make_home, run_cli) and pass an
explicit --project so they are independent of the current working directory.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

try:
    import test_chatbridge as helpers
except ImportError:  # pragma: no cover - direct invocation outside unittest discover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_chatbridge as helpers


def backup_files(home: Path) -> list[Path]:
    root = home / ".chatbridge" / "backups"
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


class BackupCoverageTests(unittest.TestCase):
    def test_codex_to_claude_apply_populates_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = helpers.make_home(Path(temp_dir))
            self.assertEqual(backup_files(home), [])

            result = helpers.run_cli(
                home,
                "native-import",
                "--from",
                "codex",
                "--to",
                "claude",
                "--session",
                "codex-1",
                "--project",
                "/repo/app",
                "--apply",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Backup: ", result.stdout)
            files = backup_files(home)
            self.assertTrue(files, "expected files under ~/.chatbridge/backups after codex->claude apply")
            # The pre-existing Claude history must have been backed up before the write.
            self.assertTrue(any(path.name == "history.jsonl" for path in files), [str(path) for path in files])

    def test_claude_to_copilot_apply_populates_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = helpers.make_home(Path(temp_dir))
            self.assertEqual(backup_files(home), [])

            # /repo/app maps to the fixture workspace ws1 (folder file:///repo/app).
            result = helpers.run_cli(
                home,
                "native-import",
                "--from",
                "claude",
                "--to",
                "copilot",
                "--session",
                "claude-1",
                "--project",
                "/repo/app",
                "--apply",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Backup: ", result.stdout)
            files = backup_files(home)
            self.assertTrue(files, "expected files under ~/.chatbridge/backups after claude->copilot apply")
            # The existing Copilot chatSessions files must have been backed up.
            self.assertTrue(
                any(path.suffix in {".json", ".jsonl"} and "copilot" in path.name for path in files),
                [str(path) for path in files],
            )

    def test_repair_copilot_imports_apply_populates_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = helpers.make_home(Path(temp_dir))
            workspace = home / ".config/Code/User/workspaceStorage/ws1"
            sid = "imported-codex-old"
            helpers.write_json(
                workspace / f"chatSessions/{sid}.json",
                {
                    "version": 3,
                    "sessionId": sid,
                    "customTitle": "[Imported from Codex] Old import",
                    "creationDate": 1700000100000,
                    "lastMessageDate": 1700000200000,
                    "initialLocation": "panel",
                    "isImported": True,
                    "requests": [
                        {
                            "requestId": "r1",
                            "timestamp": 1700000100000,
                            "message": {"text": "Old prompt", "parts": [{"text": "Old prompt"}]},
                            "response": [{"value": "Old answer"}],
                        }
                    ],
                },
            )
            self.assertEqual(backup_files(home), [])

            result = helpers.run_cli(home, "repair-copilot-imports", "--apply")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Backup: ", result.stdout)
            files = backup_files(home)
            self.assertTrue(files, "expected files under ~/.chatbridge/backups after repair-copilot-imports --apply")
            # The repaired session payload must have been backed up before rewriting.
            self.assertTrue(any(path.name == f"{sid}.json" for path in files), [str(path) for path in files])


if __name__ == "__main__":
    unittest.main()
