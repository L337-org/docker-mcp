"""Top-level pytest configuration.

`docker_mcp.server` reads `DOCKER_MCP_READONLY` / `DOCKER_MCP_NO_DESTRUCTIVE` / `DOCKER_MCP_DISABLE`
once at import time to decide which tools to register. If a developer runs the suite with any of them
set in their shell, the first import of `docker_mcp` during collection would register a reduced tool
set and break the default-mode assertions in `tests/test_server.py`. Clear them here — this top-level
conftest is imported before any test module (and thus before `docker_mcp`), keeping the unit suite
hermetic.

The subprocess-based tests in test_server.py set these vars explicitly for the *child* process via a
freshly built environment, so clearing them in this (parent) process does not affect those cases.
"""

import os

import pytest

os.environ.pop("DOCKER_MCP_READONLY", None)
os.environ.pop("DOCKER_MCP_NO_DESTRUCTIVE", None)
os.environ.pop("DOCKER_MCP_DISABLE", None)
# Registry credential fallbacks are read at call time (not import time), but clear them too so a
# developer's shell credentials can't leak basic-auth headers into the registry tests' mock flows.
os.environ.pop("DOCKER_MCP_REGISTRY_USERNAME", None)
os.environ.pop("DOCKER_MCP_REGISTRY_PASSWORD", None)
# The container guards key off DOCKER_MCP_IN_CONTAINER / DOCKER_MCP_ALLOW_SELF_TERMINATE; clear any
# shell values so they can't perturb the default-mode tests.
os.environ.pop("DOCKER_MCP_IN_CONTAINER", None)
os.environ.pop("DOCKER_MCP_ALLOW_SELF_TERMINATE", None)


@pytest.fixture(autouse=True)
def _force_host_install(monkeypatch):
    """
    Default every test to the host (non-container) code path.

    The filesystem guard reads `_utils.in_container()`; pinning it False keeps the suite hermetic
    even when run inside a devcontainer (where `/.dockerenv` exists), so existing `stream_to_file`
    tests don't trip the mount check. Tests that exercise the in-container behaviour re-patch it.
    """
    monkeypatch.setattr("docker_mcp.tools._utils.in_container", lambda: False)
