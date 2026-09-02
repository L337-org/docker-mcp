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

    Includes a daemon `NotFound`, which `_LIBRARY_FAILURES` maps here: the caller named a Docker
    object the daemon does not have - a container, image, network, volume, plugin, service or any
    other type docker-py reports this way - and the remedy is a different argument. This docstring used to say
    the opposite - that a daemon rejection "carries its own error" and so was never this type - which
    stopped being true the moment the SDK began withholding the text of anything it did not
    recognise as deliberate. Other daemon failures are `RemoteFailureError`.
    """


class ToolRefusalError(DockerMcpError):
    """A well-formed call this server declined on policy grounds.

    Distinct from `ToolInputError` because nothing about the arguments was wrong: changing them is
    not the remedy, and a caller that reads this should report it rather than retry. The wording
    reaches the model deliberately - a refusal it cannot read is one it will retry.
    """


class HostGuardError(ToolRefusalError):
    """A call refused by the host guard: an unknown host label, a write to a host marked `(ro)`,
    a destructive call to one marked `(nd)`, or a write that named no host in multi-host mode.
    """


class RemoteFailureError(DockerMcpError):
    """The far end failed and said why: a non-zero `docker` exit, an SSH connection that would not
    open, a registry rate limit, a daemon that cannot be reached.

    Separate from `CapabilityError` because this one may be worth retrying - a 429 and an
    unreachable daemon both often clear - and because the far end's own text is the useful part, so
    it is carried through rather than paraphrased.
    """


class CapabilityError(DockerMcpError):
    """This host or installation cannot do it at all: a missing CLI plugin, no POSIX shell on the
    remote host, a docker-py without the API the call needs, a feature area switched off.

    Never worth retrying, which is what separates it from `RemoteFailureError`. The message names
    what is missing so the caller can tell the user rather than route around it.
    """


class HostConfigError(DockerMcpError):
    """A malformed `DOCKER_MCP_SERVER_HOSTS` value. `_hosts.load()` turns this into a stderr line
    and `exit(1)`.

    Raised before any tool is registered, so it never reaches a client; it subclasses the base for
    one exception hierarchy rather than because it is translated.
    """
