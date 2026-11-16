"""Top-level pytest configuration.

`docker_mcp.server` reads `DOCKER_MCP_SERVER_READONLY` / `DOCKER_MCP_SERVER_NO_DESTRUCTIVE` /
`DOCKER_MCP_SERVER_DISABLE` once at import time to decide which tools to register. If a developer runs
the suite with any of them set in their shell, the first import of `docker_mcp` during collection
would register a reduced tool set and break the default-mode assertions in `tests/test_server.py`.
Clear them here — this top-level conftest is imported before any test module (and thus before
`docker_mcp`), keeping the unit suite hermetic.

The subprocess-based tests in test_server.py set these vars explicitly for the *child* process via a
freshly built environment, so clearing them in this (parent) process does not affect those cases.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

for _tunable in (
    "DOCKER_MCP_SERVER_READONLY",
    "DOCKER_MCP_SERVER_NO_DESTRUCTIVE",
    "DOCKER_MCP_SERVER_DISABLE",
    # Registry credential fallbacks are read at call time (not import time), but clear them too so a
    # developer's shell credentials can't leak basic-auth headers into the registry tests' mock flows.
    "DOCKER_MCP_SERVER_REGISTRY_USERNAME",
    "DOCKER_MCP_SERVER_REGISTRY_PASSWORD",
    # The container guards key off these; clear shell values so they can't perturb default-mode tests.
    "DOCKER_MCP_SERVER_IN_CONTAINER",
    "DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE",
    # Provenance stamping is on by default and the label tests assert it; a shell opt-out would
    # break them.
    "DOCKER_MCP_SERVER_NO_LABELS",
    # Read at import by `redaction.settings.load()`: a shell value would pin redaction on for every
    # test, and a malformed names value would abort collection with SystemExit.
    "DOCKER_MCP_SERVER_REDACT",
    "DOCKER_MCP_SERVER_REDACT_NAMES",
):
    os.environ.pop(_tunable, None)

# Net-new (canonical only, no alias). `docker_mcp/__init__.py` calls `_hosts.load()` on first import,
# which parses this var and fail-fasts (SystemExit) on a malformed value — so a developer's shell value
# could otherwise abort collection or skew the default single-host assumption the unit suite makes.
os.environ.pop("DOCKER_MCP_SERVER_HOSTS", None)


@pytest.fixture(autouse=True)
def _force_host_install(monkeypatch):
    """
    Default every test to the host (non-container) code path.

    The filesystem guard reads `_utils.in_container()`; pinning it False keeps the suite hermetic
    even when run inside a devcontainer (where `/.dockerenv` exists), so existing `stream_to_file`
    tests don't trip the mount check. Tests that exercise the in-container behaviour re-patch it.
    """
    monkeypatch.setattr("docker_mcp.tools._utils.in_container", lambda: False)


@pytest.fixture(autouse=True)
def _pin_registry_to_local_daemon(monkeypatch):
    """
    Pin the host registry to one deterministic local daemon.

    With DOCKER_MCP_SERVER_HOSTS cleared above, the registry's single host is resolved from the
    developer's own DOCKER_HOST or active docker context, which may perfectly well be `ssh://`. That
    is not inert: the CLI-backed tools ask `should_remote_exec` whether the remote-exec fallback
    applies, and an ssh:// default would send unit tests down that path — probing plugins, and
    connecting, for real. Tests needing a specific registry set `_registry` themselves, which (running
    after this fixture) wins.
    """
    from docker_mcp import _hosts

    monkeypatch.setattr(_hosts, "_registry", {"default": _hosts.Host("default", "unix:///var/run/docker.sock")})


@pytest.fixture
def on_the_wire():
    """Call a registered tool through `call_tool`, the path a client uses.

    Calling the function directly - what nearly every test here does, and rightly for a unit check -
    cannot see what a *client* is told, because the SDK decides that from the exception's type and
    only `ToolError`/`ResourceError` keep their message. So a message assertion on a direct call
    proves nothing about the wire. Use this for the refusals whose wording a caller acts on.

    Whatever `call_tool` raises comes straight back out, so assert on the type as well as the text:
    a plain `ToolError` is a deliberate failure logged at INFO with its message kept, while an
    `UnexpectedToolError` is a crash logged at ERROR with the message withheld.
    """

    def call(name: str, arguments: dict):
        import anyio

        from docker_mcp.server import mcp

        return anyio.run(mcp.call_tool, name, arguments)

    return call


_REPO_ROOT = Path(__file__).resolve().parent.parent


def tracked_plugin_files() -> list[str]:
    """The paths git tracks under `claude-plugin/`, relative to the repository root.

    The release builds from a clean checkout, so a tracked file is all that can ship.  A git-ignored
    file a developer's tools leave in the folder - an IDE's virtualenv for the launch project, or the
    `uv.lock` that creating it writes - is not part of the plugin, so checks on the repository's copy
    read this list rather than the folder on disk.  The index is listed, so a staged file counts.

    Returns:
        list[str]: POSIX paths starting `claude-plugin/`; fails the test if git cannot list them
    """
    git = shutil.which("git")
    assert git, "git is needed to list the plugin folder's tracked files"
    listed = subprocess.run(  # noqa: S603 - fixed argv, resolved binary, no shell
        [git, "ls-files", "-z", "--", "claude-plugin"], cwd=_REPO_ROOT, capture_output=True, timeout=30, check=False
    )
    stderr = listed.stderr.decode("utf-8", errors="replace").strip()
    assert listed.returncode == 0, f"git ls-files claude-plugin failed with exit {listed.returncode}: {stderr!r}"
    paths = [path for path in listed.stdout.decode("utf-8", errors="replace").split("\0") if path]
    assert paths, f"git tracks nothing under claude-plugin/ in {str(_REPO_ROOT)!r}"
    return paths


def copy_tracked_plugin(target: Path) -> Path:
    """Copy the tracked plugin files into `target`, as a clean checkout holds them.

    Symlinks are copied as symlinks, so a committed one still reaches the checks that refuse it.

    Args:
        target: the directory to create, standing in for `claude-plugin/`

    Returns:
        Path: `target`
    """
    for path in tracked_plugin_files():
        destination = target / Path(path).relative_to("claude-plugin")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / path, destination, follow_symlinks=False)
    return target
