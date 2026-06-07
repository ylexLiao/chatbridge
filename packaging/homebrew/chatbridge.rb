# Homebrew formula template.
# Replace tag version and sha256 after publishing a GitHub release.
class Chatbridge < Formula
  desc "Local TUI/CLI bridge for Copilot, Codex, and Claude Code chat histories"
  homepage "https://github.com/ylexLiao/chatbridge"
  url "https://github.com/ylexLiao/chatbridge/archive/refs/tags/v1.0.1.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_SHA256"
  license "MIT"
  head "https://github.com/ylexLiao/chatbridge.git", branch: "main"

  depends_on "python@3.12"
  depends_on "rust" => :build

  def install
    system "cargo", "build", "--release", "--manifest-path", "rust/chatbridge-tui/Cargo.toml"
    libexec.install Dir["*"]
    (libexec/"bin").install "rust/chatbridge-tui/target/release/chatbridge-tui"
    wrapper = buildpath/"chatbridge"
    wrapper.write <<~EOS
      #!/usr/bin/env bash
      set -euo pipefail
      export PYTHONPATH="#{libexec}${PYTHONPATH:+:$PYTHONPATH}"
      exec "#{Formula["python@3.12"].opt_bin}/python3" -c 'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("chatbridge", run_name="__main__", alter_sys=True)' "#{libexec}" "$@"
    EOS
    bin.install wrapper
  end

  test do
    assert_match "ChatBridge TUI", shell_output({"CHATBRIDGE_TUI_SMOKE" => "1"}, "#{bin}/chatbridge")
    assert_match "Rust ratatui TUI", shell_output({"CHATBRIDGE_TUI_SMOKE" => "1"}, "#{bin}/chatbridge")
  end
end
