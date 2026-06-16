# internal helper: provenance labels stamped on the Docker objects this server creates
#
# Every resource docker-mcp-server creates on the agent's behalf (containers, networks, volumes,
# services, configs, secrets) is stamped with a small set of namespaced labels identifying it as
# MCP-created, so a human or a cleanup job can later enumerate exactly that footprint with a single
# `--filter label=docker-mcp-server.managed=true`.
#
# The stamping is benign by construction: the keys live in a distinctive, near-unique namespace, we
# only *add* keys (a caller-supplied label always wins on collision), and the affected resources
# carry labels as pure metadata with no effect on content or digests. (Image builds are the one
# place a label changes the digest, so they are deliberately NOT in this set.) On by default; set
# DOCKER_MCP_NO_LABELS=1 to suppress it entirely.

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from docker_mcp.tools._utils import env_flag

# Prefix for the provenance labels. A single constant so the namespace is trivially rebrandable.
# Deliberately *not* reverse-DNS: the project name is distinctive enough to make a collision
# negligible, it reads better in `docker ps` / `--filter label=...`, and it implies no project↔domain
# link. Clear of Docker's reserved namespaces (com.docker.*, io.docker.*, org.dockerproject.*).
LABEL_PREFIX = "docker-mcp-server"

# Opt-out switch (stamping is on by default).
DISABLE_ENV = "DOCKER_MCP_NO_LABELS"

# The key callers filter on; the rest are forensic.
MANAGED_LABEL = f"{LABEL_PREFIX}.managed"
MANAGED_FILTER = f"{MANAGED_LABEL}=true"


def _server_version() -> str:
    """The installed package version, or 'unknown' from a source checkout without dist metadata."""
    try:
        return _pkg_version("docker-mcp-server")
    except PackageNotFoundError:
        return "unknown"


def provenance_labels(created_by: str) -> dict[str, str]:
    """
    The MCP-provenance label set for a resource this server creates, or {} when stamping is disabled.

    `created_by` is the @tool name (e.g. "run_container") recorded in the `.tool` label.
    """
    if env_flag(DISABLE_ENV):
        return {}
    return {
        MANAGED_LABEL: "true",
        f"{LABEL_PREFIX}.version": _server_version(),
        f"{LABEL_PREFIX}.tool": created_by,
        f"{LABEL_PREFIX}.created": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def with_provenance(labels: dict | list | None, created_by: str) -> dict[str, str] | None:
    """
    Merge caller-supplied labels with the provenance set.

    Accepts the shapes the Docker SDK accepts for `labels` — a dict, a list of bare names (treated
    as empty values), or None. A caller dict wins on any key collision (the caller's value
    overrides ours). A caller *list* only contributes names that aren't already provenance keys —
    a bare name carries no value, so it can't meaningfully override a provenance label and the
    provenance value is kept. Returns a merged dict, or None when there is nothing to apply
    (stamping disabled *and* no caller labels) so the call site can `drop_none` it back out and
    preserve the SDK's default.
    """
    merged: dict[str, str] = dict(provenance_labels(created_by))
    if isinstance(labels, dict):
        merged.update(labels)  # caller wins on any key collision
    elif isinstance(labels, list):
        # docker accepts a list of bare names (set with empty values); normalize so we can merge.
        for name in labels:
            merged.setdefault(name, "")
    return merged or None


def managed_filter(filters: dict | None) -> dict:
    """
    Return a copy of `filters` with the managed-by-us label filter added (for `managed_only=True`).

    Preserves any label filter the caller already set by combining into a list rather than clobbering.
    """
    result = dict(filters or {})
    existing = result.get("label")
    if existing is None:
        result["label"] = MANAGED_FILTER
    elif isinstance(existing, list):
        result["label"] = [*existing, MANAGED_FILTER]
    else:
        result["label"] = [existing, MANAGED_FILTER]
    return result
