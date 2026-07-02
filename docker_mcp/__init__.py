"""MCP server for managing Docker resources and searching Docker documentation"""

from docker_mcp.server import mcp, finalize_instructions
from docker_mcp import _hosts

# Parse DOCKER_MCP_SERVER_HOSTS and pin the host registry before any tool registers — the @tool()
# decorator and resources read the registry at registration time. A malformed value fail-fasts here
# (one stderr line + non-zero exit).
_hosts.load()

from docker_mcp import tools  # noqa: F401, E402  -- side-effect import (registers @mcp.tool()); must follow _hosts.load()

# Build the server `instructions` router now that every tool has registered, so it reflects exactly the
# domains the active env switches (DOCKER_MCP_SERVER_DISABLE / _READONLY / _NO_DESTRUCTIVE) left registered.
finalize_instructions()


def main():
    """
    Main function to run the MCP server.
    """
    import sys

    # Exit-fast version report, so the published package can be smoke-tested (the weekly canary
    # workflow runs `uvx docker-mcp-server --version` on each platform) without starting a server
    # that never returns. Handled before startup_preflight: no daemon, no network. Printing to
    # stdout is safe here — the stdio-channel constraint only applies once the server is serving.
    if "--version" in sys.argv[1:]:
        from importlib.metadata import version

        print(version("docker-mcp-server"))
        return

    from docker_mcp.tools.client import startup_preflight

    startup_preflight()
    mcp.run(transport="stdio")
