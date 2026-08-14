# This file is maintained automatically by publish-homebrew.yaml in L337-org/docker-mcp.
# Do not edit by hand: changes will be overwritten on the next release.
class DockerMcpServer < Formula
  desc "MCP server for managing Docker via the Docker SDK for Python"
  homepage "https://github.com/L337-org/docker-mcp"
  version "${VERSION}"
  license "MIT"

  depends_on "uv" => :build
  depends_on "python@3.14"

  on_macos do
    on_arm do
      url "https://github.com/L337-org/docker-mcp/releases/download/${TAG}/docker-mcp-server-${VERSION}-wheelhouse-macos-arm64.tar.gz"
      sha256 "${SHA_ARM64}"
    end
    on_intel do
      url "https://github.com/L337-org/docker-mcp/releases/download/${TAG}/docker-mcp-server-${VERSION}-wheelhouse-macos-x86_64.tar.gz"
      sha256 "${SHA_X86_64}"
    end
  end

  # Keep Homebrew's post-install cleanup phase (stripping symbols, pruning .la files, fixing
  # permissions) out of the virtualenv, so it leaves the Python extension .so files alone.
  #
  # This is only a candidate workaround for the headerpad failure that paused this channel, and
  # an unverified one: skip_clean affects the cleanup phase only. It does not disable the separate
  # keg-relocation step that rewrites dylib IDs via install_name_tool, which is where that failure
  # actually occurs.
  #
  # This file is rendered into L337-org/homebrew-tap, so read it from there: this tap's own README
  # covers the pause, and the full record is in the "Homebrew tap" section of CLAUDE.md in
  # https://github.com/L337-org/docker-mcp.
  skip_clean "libexec"

  def install
    # Deliberately Formula[...].opt_bin rather than the formula_opt_bin helper that
    # Homebrew/FormulaPathMethods asks for: that helper only exists from Homebrew 6.0.3, so using
    # it would fail the formula on older Homebrew for a style-only gain. Revisit when 6.0.3 is a
    # safe floor.
    python3 = Formula["python@3.14"].opt_bin/"python3.14"
    system "uv", "venv", libexec.to_s, "--python", python3.to_s
    system "uv", "pip", "install",
      "--python", (libexec/"bin/python3").to_s,
      "--no-index",
      "--find-links=#{buildpath}",
      "docker-mcp-server==#{version}"
    bin.install_symlink libexec/"bin/docker-mcp-server"
    bin.install_symlink libexec/"bin/docker-mcp"
  end

  test do
    system libexec/"bin/python3", "-c", "import docker_mcp"
  end
end
