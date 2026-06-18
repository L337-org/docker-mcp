"""MCP server for managing Docker resources and searching Docker documentation"""

from docker_mcp.server import mcp, finalize_instructions
from docker_mcp import tools  # noqa: F401  -- imported for side effects (registers @mcp.tool() decorators)

# Build the server `instructions` router now that every tool has registered, so it reflects exactly the
# domains the active env switches (DOCKER_MCP_SERVER_DISABLE / _READONLY / _NO_DESTRUCTIVE) left registered.
finalize_instructions()


def main():
    """
    Main function to run the MCP server.
    """
    from docker_mcp.tools.client import startup_preflight

    startup_preflight()
    mcp.run(transport="stdio")
