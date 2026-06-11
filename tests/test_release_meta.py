"""Release metadata consistency checks.

Ensures the version is single-sourced across package.json, the Python package,
and the Rust TUI crate, and that the release workflow publishes exactly the
asset names the installers download.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import chatbridge  # noqa: E402


class VersionSingleSourcingTests(unittest.TestCase):
    def test_versions_match_across_package_json_python_and_cargo(self) -> None:
        package_version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        cargo_match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            (ROOT / "rust/chatbridge-tui/Cargo.toml").read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(cargo_match, "no version line found in rust/chatbridge-tui/Cargo.toml")

        self.assertEqual(package_version, chatbridge.__version__, "package.json version differs from chatbridge.__version__")
        self.assertEqual(cargo_match.group(1), chatbridge.__version__, "Cargo.toml version differs from chatbridge.__version__")


class ReleaseAssetNameTests(unittest.TestCase):
    # (uname -s, uname -m) combinations install.sh supports for release installs.
    SUPPORTED_UNAMES = [
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
    ]

    def release_yaml_assets(self) -> set[str]:
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        assets: set[str] = set()
        # Literal full names (e.g. in shell snippets).
        assets.update(re.findall(r"chatbridge-[a-z0-9]+-[a-z0-9]+\.(?:tar\.gz|zip)", text))
        # Build-matrix entries pair an `asset:` name with an `archive:` extension.
        for block in re.split(r"\n\s*- os:", text):
            asset_match = re.search(r"asset:\s*(chatbridge-[a-z0-9-]+)", block)
            archive_match = re.search(r"archive:\s*(tar\.gz|zip)", block)
            if asset_match and archive_match:
                assets.add(f"{asset_match.group(1)}.{archive_match.group(1)}")
        # The macOS job builds tarballs via `build_asset <target> <asset>`.
        for name in re.findall(r"build_asset\s+\S+\s+(chatbridge-[a-z0-9-]+)", text):
            assets.add(f"{name}.tar.gz")
        return assets

    @unittest.skipUnless(shutil.which("bash"), "bash is required to evaluate install.sh detect_asset")
    def test_install_sh_detect_asset_names_exist_in_release_workflow(self) -> None:
        install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
        match = re.search(r"^detect_asset\(\) \{\n(?:.*\n)*?^\}", install_sh, flags=re.MULTILINE)
        self.assertIsNotNone(match, "detect_asset() not found in install.sh")
        function_text = match.group(0)

        release_assets = self.release_yaml_assets()
        for uname_s, uname_m in self.SUPPORTED_UNAMES:
            with self.subTest(uname_s=uname_s, uname_m=uname_m):
                script = "\n".join(
                    [
                        f'uname() {{ case "$1" in -s) echo "{uname_s}" ;; -m) echo "{uname_m}" ;; esac; }}',
                        "source_install_hint() { :; }",
                        function_text,
                        "detect_asset",
                    ]
                )
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                asset = result.stdout.strip()
                self.assertIn(
                    asset,
                    release_assets,
                    f"install.sh downloads {asset!r} for uname {uname_s}/{uname_m}, but release.yml does not publish it",
                )

    def test_install_ps1_asset_exists_in_release_workflow(self) -> None:
        install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
        match = re.search(r'"(chatbridge-windows-[a-z0-9]+\.zip)"', install_ps1)
        self.assertIsNotNone(match, "no windows asset name found in install.ps1")
        self.assertIn(match.group(1), self.release_yaml_assets())


if __name__ == "__main__":
    unittest.main()
