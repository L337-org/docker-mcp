"""Shared pytest configuration for integration tests.

Auto-marks every test in this directory with `@pytest.mark.integration` so
new files don't need to declare the marker, and provides a module-scoped
autouse `skip_if_no_daemon` fixture that skips the suite when the Docker
daemon is unreachable. `skip_if_no_swarm` is a separate, non-autouse
fixture that swarm-dependent test files opt into explicitly.

`fail_unless_environmental` is the guard against the failure mode that let three
broken scout tools ship: a test that skipped on *any* non-zero exit, so a
deterministic product defect was indistinguishable from being offline. Skipping
is only legitimate for a cause we can name and recognise.
"""

from pathlib import Path

import pytest
from docker.errors import DockerException

from docker_mcp.tools.system import system_info, system_ping

_INTEGRATION_DIR = Path(__file__).parent

# Substrings identifying a genuinely environmental failure: the host is offline, a registry is
# unreachable or throttling, or the call needs credentials this machine does not have. Matched
# case-insensitively against both streams, because the docker CLI and its plugins are inconsistent
# about which one an error lands on -- `docker scout quickview --format json` writes "unknown flag"
# to stdout and leaves stderr empty, which is precisely why the old "stderr looks empty, must be
# the network" reasoning was wrong.
_ENVIRONMENTAL_SIGNALS = (
    "connection refused",
    "no such host",
    "timed out",
    "i/o timeout",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
    "tls handshake",
    "certificate",
    "unauthorized",
    "authentication required",
    "denied: requested access",
    "not logged in",
    "toomanyrequests",
    "rate limit",
    "429 too many requests",
)


def fail_unless_environmental(*, returncode: int, stderr: str = "", stdout: str = "", what: str) -> None:
    """Skip when a failed CLI call names a recognised environmental cause; otherwise fail.

    A non-zero exit with no recognised cause is treated as a product defect, because that is the
    likelier explanation and because the alternative -- skipping -- hides it indefinitely.

    args:
        returncode - The CLI exit status; zero returns immediately
        stderr - Captured stderr, searched for an environmental signal
        stdout - Captured stdout, searched too (some plugins report errors here)
        what - Short description of the call, used in the skip or failure message
    returns: None - raises via `pytest.skip` or `pytest.fail` when `returncode` is non-zero
    """
    if returncode == 0:
        return
    haystack = f"{stderr}\n{stdout}".lower()
    for signal in _ENVIRONMENTAL_SIGNALS:
        if signal in haystack:
            pytest.skip(f"{what}: recognised environmental failure ({signal!r}); skipping")
    pytest.fail(
        f"{what} exited {returncode} with no recognised environmental cause, so this is a product "
        f"defect until shown otherwise. If the cause really is environmental, add its signature to "
        f"_ENVIRONMENTAL_SIGNALS rather than widening the skip.\n"
        f"stderr: {stderr[:500]!r}\nstdout: {stdout[:500]!r}"
    )


def fail_unless_environmental_error(exc: BaseException, *, what: str) -> None:
    """Exception-shaped counterpart to `fail_unless_environmental`, for SDK-backed setup calls.

    A fixture that skips on any exception hides a product defect just as effectively as one that
    skips on any non-zero exit, so the same rule applies: skip only for a cause we can name.

    args:
        exc - The caught exception, whose string form is searched for an environmental signal
        what - Short description of the call, used in the skip or failure message
    returns: None - always raises, via `pytest.skip` or `pytest.fail`
    """
    text = str(exc).lower()
    for signal in _ENVIRONMENTAL_SIGNALS:
        if signal in text:
            pytest.skip(f"{what}: recognised environmental failure ({signal!r}); skipping")
    pytest.fail(
        f"{what} raised {type(exc).__name__} with no recognised environmental cause, so this is a "
        f"product defect until shown otherwise. If the cause really is environmental, add its "
        f"signature to _ENVIRONMENTAL_SIGNALS rather than widening the skip.\n{exc}"
    )


def pytest_collection_modifyitems(items):
    for item in items:
        if _INTEGRATION_DIR in Path(item.path).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True, scope="module")
def skip_if_no_daemon():
    try:
        system_ping()
    except (DockerException, RuntimeError) as exc:
        pytest.skip(f"Docker daemon not reachable: {exc}")


@pytest.fixture(scope="module")
def skip_if_no_swarm():
    """Skip a test module's tests if the daemon isn't a swarm manager (`docker swarm init` first)."""
    info = system_info()
    if (info.get("Swarm") or {}).get("LocalNodeState") != "active":
        pytest.skip("Docker daemon is not a swarm manager (run `docker swarm init` first)")
