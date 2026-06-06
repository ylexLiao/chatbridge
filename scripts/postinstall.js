"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const suffix = process.platform === "win32" ? ".exe" : "";
const bundledBinary = path.join(root, "bin", `chatbridge-tui${suffix}`);
const sourceBinary = path.join(root, "rust", "chatbridge-tui", "target", "release", `chatbridge-tui${suffix}`);
const manifest = path.join(root, "rust", "chatbridge-tui", "Cargo.toml");

function exists(filePath) {
  try {
    return fs.existsSync(filePath);
  } catch (_error) {
    return false;
  }
}

function canRun(command, args) {
  const result = childProcess.spawnSync(command, args, { stdio: "ignore" });
  return result.status === 0;
}

if (process.env.CHATBRIDGE_SKIP_TUI_BUILD === "1") {
  console.log("chatbridge: skipping Rust TUI build because CHATBRIDGE_SKIP_TUI_BUILD=1");
  process.exit(0);
}

if (exists(bundledBinary) || exists(sourceBinary)) {
  process.exit(0);
}

if (!exists(manifest)) {
  console.warn("chatbridge: Rust TUI source was not found; install a release package with a bundled binary.");
  process.exit(0);
}

if (!canRun("cargo", ["--version"])) {
  console.error("chatbridge: cargo is required to build the Rust TUI from this source package.");
  console.error("Install Rust from https://rustup.rs/ or set CHATBRIDGE_SKIP_TUI_BUILD=1 to install only the Python CLI.");
  process.exit(1);
}

console.log("chatbridge: building Rust TUI...");
const result = childProcess.spawnSync(
  "cargo",
  ["build", "--manifest-path", manifest, "--release"],
  { cwd: root, stdio: "inherit" }
);

process.exit(result.status ?? 1);
