"""Top-level pytest configuration.

`docker_mcp.server` reads `DOCKER_MCP_READONLY` / `DOCKER_MCP_NO_DESTRUCTIVE` once at import time to
decide which tools to register. If a developer runs the suite with either set in their shell, the
first import of `docker_mcp` during collection would register a reduced tool set and break the
default-mode assertions in `tests/test_server.py`. Clear them here — this top-level conftest is
imported before any test module (and thus before `docker_mcp`), keeping the unit suite hermetic.

The subprocess-based tests in test_server.py set these vars explicitly for the *child* process via a
freshly built environment, so clearing them in this (parent) process does not affect those cases.
"""

import os

os.environ.pop("DOCKER_MCP_READONLY", None)
os.environ.pop("DOCKER_MCP_NO_DESTRUCTIVE", None)
# Registry credential fallbacks are read at call time (not import time), but clear them too so a
# developer's shell credentials can't leak basic-auth headers into the registry tests' mock flows.
os.environ.pop("DOCKER_MCP_REGISTRY_USERNAME", None)
os.environ.pop("DOCKER_MCP_REGISTRY_PASSWORD", None)
