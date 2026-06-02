"""MCP server for managing Docker resources and searching Docker documentation"""

from server import mcp
import tools  # noqa: F401  -- imported for side effects (registers @mcp.tool() decorators)


def main():
    """
    Main function to run the MCP server.
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
