# This file is maintained automatically by publish-homebrew.yaml in GavinLucas/docker-mcp.
# Do not edit by hand — changes will be overwritten on the next release.
class DockerMcpServer < Formula
  desc "MCP server for managing Docker via the Docker SDK for Python"
  homepage "https://github.com/GavinLucas/docker-mcp"
  version "${VERSION}"
  license "MIT"

  depends_on "python@3.14"
  depends_on "uv" => :build

  # Prevent Homebrew's post-install linkage fixer from rewriting @rpath IDs
  # in Python extension .so files inside the virtualenv. Those binaries don't
  # have headerpad room for longer absolute paths and don't need relinking —
  # Python loads them directly by path, not via the dylib ID.
  skip_clean "libexec"

  on_macos do
    on_arm do
      url "https://github.com/GavinLucas/docker-mcp/releases/download/${TAG}/docker-mcp-server-${VERSION}-wheelhouse-macos-arm64.tar.gz"
      sha256 "${SHA_ARM64}"
    end
    on_intel do
      url "https://github.com/GavinLucas/docker-mcp/releases/download/${TAG}/docker-mcp-server-${VERSION}-wheelhouse-macos-x86_64.tar.gz"
      sha256 "${SHA_X86_64}"
    end
  end

  def install
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
