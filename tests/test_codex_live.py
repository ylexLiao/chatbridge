"""Opt-in check against a real Codex binary (no model calls, never touches ~/.codex).

    CHATBRIDGE_CODEX_BIN="$(which codex)" npm test
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("CHATBRIDGE_CODEX_BIN"), "set CHATBRIDGE_CODEX_BIN to a codex executable to run")
class CodexLiveCompatTests(unittest.TestCase):
    def test_import_is_listed_resumable_and_shows_tool_activity(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex_compat_check.py"), "--codex-bin", os.environ["CHATBRIDGE_CODEX_BIN"]],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
