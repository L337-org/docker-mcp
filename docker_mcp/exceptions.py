"""
Project exception types.

Lives at the package root (not under `tools/`) so `docker_mcp.server` can import it for the host
guard without importing `docker_mcp.tools` - a circular import at tool-registration time - mirroring
`_env.py` and `_hosts.py`.

The split that matters is anticipated versus not. The MCP SDK decides what a failure tells the
client purely by exception type: a `ToolError`/`ResourceError` keeps its message, and anything else
is reported as `Error executing tool <name>` with the original text withheld and a traceback logged
at ERROR. `server.py`'s registrars translate the types here into the SDK's, so a failure the caller
can act on arrives with its wording intact, and a bug in this server stays a crash.

Raise one of these for a refusal, an invalid argument, or a diagnostic naming what to do about it.
Leave a bare `RuntimeError`/`ValueError` where the failure means this server is broken or in a state
it did not expect: that text is for the log, not the model.
"""

from __future__ import annotations


class DockerMcpError(Exception):
    """Base for every failure this server raises deliberately.

    Catching this catches an anticipated failure and nothing else, which is what makes the
    translation in `server.py` safe to apply at one chokepoint.
    """


class ToolInputError(DockerMcpError):
    """An argument a caller can correct: unknown, malformed, out of range, or a required
    combination not supplied.

    Never for a value the Docker daemon rejected - that is the daemon's answer to a legal call, and
    it carries its own error.
    """


class HostGuardError(DockerMcpError):
    """A call refused by the host guard: an unknown host label, a write to a host marked `(ro)`,
    a destructive call to one marked `(nd)`, or a write that named no host in multi-host mode.

    Distinct from `ToolInputError` because it is a policy refusal rather than a malformed argument:
    the call was well-formed and this server declined it. The wording reaches the model, which is
    the point - a refusal it cannot read is one it will retry.
    """


class HostConfigError(DockerMcpError):
    """A malformed `DOCKER_MCP_SERVER_HOSTS` value. `_hosts.load()` turns this into a stderr line
    and `exit(1)`.

    Raised before any tool is registered, so it never reaches a client; it subclasses the base for
    one exception hierarchy rather than because it is translated.
    """
