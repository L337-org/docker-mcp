# library of mcp tools for `docker buildx`.
#
# Buildx is a CLI plugin layered on BuildKit; it covers multi-platform builds, modern
# cache export/import, attestations (SBOM/provenance), and manifest-list operations.
# These tools wrap the CLI via tools/_cli.py for cross-platform safety.

import json

from server import mcp
from tools._cli import CliResult, require_plugin, run_docker

# Per-operation timeout ceilings (seconds). Builds and pulls against slow registries or
# large contexts routinely run for many minutes, so they get longer ceilings than queries.
_TIMEOUT_QUERY = 60.0
_TIMEOUT_BUILD = 1800.0
_TIMEOUT_BAKE = 1800.0
_TIMEOUT_IMAGETOOLS_CREATE = 600.0
_TIMEOUT_PRUNE = 600.0


def _run_buildx(args: list[str], *, cwd: str | None = None, timeout: float) -> CliResult:
    require_plugin("buildx")
    return run_docker(["buildx", *args], cwd=cwd, timeout=timeout)


def _raise_on_failure(result: CliResult, action: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"`docker buildx {action}` failed with exit code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip() or '<no output>'}"
        )


def _parse_json_lines(text: str) -> list[dict]:
    """Parse one JSON object per non-blank line of `text`, tolerating a trailing blank line."""
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


@mcp.tool()
def buildx_build(
    context: str,
    tags: list[str] | None = None,
    platforms: list[str] | None = None,
    file: str | None = None,
    build_args: dict | None = None,
    build_contexts: dict | None = None,
    labels: dict | None = None,
    annotations: list[str] | None = None,
    target: str | None = None,
    push: bool = False,
    load: bool = False,
    output: list[str] | None = None,
    no_cache: bool = False,
    no_cache_filter: list[str] | None = None,
    pull: bool = False,
    cache_from: list[str] | None = None,
    cache_to: list[str] | None = None,
    builder: str | None = None,
    sbom: str | None = None,
    provenance: str | None = None,
    attest: list[str] | None = None,
    secret: list[str] | None = None,
    ssh: list[str] | None = None,
    timeout_seconds: float = _TIMEOUT_BUILD,
) -> dict:
    """
    Build an image with BuildKit via `docker buildx build`.

    Replaces the legacy `build_image` tool when you need any of: multi-platform output
    (`platforms`), modern cache export (`cache_from`/`cache_to`), SBOM or provenance
    attestations, build secrets, or multi-stage builds with `target`. Always runs with
    `--progress=plain` so output is captured rather than redrawn on a TTY.

    args:
        context: str - Build context (path, URL, or `-` to use a tarball on stdin).
                       Passed verbatim to docker; no shell expansion of `~` or globs.
        tags: list[str] - Image references to apply (`-t`, repeatable)
        platforms: list[str] - Target platforms, e.g. ["linux/amd64", "linux/arm64"]
        file: str - Dockerfile path (relative to context unless absolute)
        build_args: dict - Build-time variables (each becomes `--build-arg KEY=VALUE`)
        build_contexts: dict - Additional named build contexts (e.g. {"deps": "./vendor"})
        labels: dict - Image labels (each becomes `--label KEY=VALUE`)
        annotations: list[str] - OCI manifest annotations (passed verbatim, repeatable)
        target: str - Target build stage to stop at
        push: bool - Push the result to the registry (mutually exclusive with `load`)
        load: bool - Load the result into the local image store (single-platform builds only)
        output: list[str] - Custom `--output` specs (e.g. ["type=tar,dest=out.tar"])
        no_cache: bool - Do not use cache when building
        no_cache_filter: list[str] - Stage names to exclude from caching
        pull: bool - Always attempt to pull a newer version of each base image
        cache_from: list[str] - Cache import specs, e.g. ["type=registry,ref=user/img:cache"]
        cache_to: list[str] - Cache export specs
        builder: str - Override the active builder
        sbom: str - Shorthand for `--attest=type=sbom`; pass "true" or a config string
        provenance: str - Shorthand for `--attest=type=provenance`; pass "true", "false", or a config string
        attest: list[str] - Custom attestation specs (repeatable)
        secret: list[str] - Secret specs (e.g. ["id=npmrc,src=~/.npmrc"])
        ssh: list[str] - SSH agent socket / key specs (e.g. ["default"])
        timeout_seconds: float - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args: list[str] = ["build", "--progress=plain"]
    for tag in tags or []:
        args.extend(["--tag", tag])
    for platform in platforms or []:
        args.extend(["--platform", platform])
    if file is not None:
        args.extend(["--file", file])
    for key, value in (build_args or {}).items():
        args.extend(["--build-arg", f"{key}={value}"])
    for key, value in (build_contexts or {}).items():
        args.extend(["--build-context", f"{key}={value}"])
    for key, value in (labels or {}).items():
        args.extend(["--label", f"{key}={value}"])
    for annotation in annotations or []:
        args.extend(["--annotation", annotation])
    if target is not None:
        args.extend(["--target", target])
    if push:
        args.append("--push")
    if load:
        args.append("--load")
    for spec in output or []:
        args.extend(["--output", spec])
    if no_cache:
        args.append("--no-cache")
    for stage in no_cache_filter or []:
        args.extend(["--no-cache-filter", stage])
    if pull:
        args.append("--pull")
    for spec in cache_from or []:
        args.extend(["--cache-from", spec])
    for spec in cache_to or []:
        args.extend(["--cache-to", spec])
    if builder is not None:
        args.extend(["--builder", builder])
    if sbom is not None:
        args.extend(["--sbom", sbom])
    if provenance is not None:
        args.extend(["--provenance", provenance])
    for spec in attest or []:
        args.extend(["--attest", spec])
    for spec in secret or []:
        args.extend(["--secret", spec])
    for spec in ssh or []:
        args.extend(["--ssh", spec])
    args.append(context)
    return _run_buildx(args, timeout=timeout_seconds).to_dict()


@mcp.tool()
def buildx_bake(
    targets: list[str] | None = None,
    files: list[str] | None = None,
    set_overrides: list[str] | None = None,
    push: bool = False,
    load: bool = False,
    no_cache: bool = False,
    pull: bool = False,
    builder: str | None = None,
    cwd: str | None = None,
    timeout_seconds: float = _TIMEOUT_BAKE,
) -> dict:
    """
    Build multiple targets defined in a bake file (HCL, JSON, or compose).

    args:
        targets: list[str] - Bake targets to build (default: the `default` group)
        files: list[str] - Bake file paths (`-f`, repeatable)
        set_overrides: list[str] - Per-target overrides, e.g. ["app.platform=linux/amd64"]
        push: bool - Push results to the registry
        load: bool - Load results into the local image store
        no_cache: bool - Do not use cache when building
        pull: bool - Always pull a newer base image
        builder: str - Override the active builder
        cwd: str - Working directory containing the bake file (defaults to the server's cwd)
        timeout_seconds: float - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args: list[str] = ["bake", "--progress=plain"]
    for f in files or []:
        args.extend(["-f", f])
    for override in set_overrides or []:
        args.extend(["--set", override])
    if push:
        args.append("--push")
    if load:
        args.append("--load")
    if no_cache:
        args.append("--no-cache")
    if pull:
        args.append("--pull")
    if builder is not None:
        args.extend(["--builder", builder])
    if targets:
        args.extend(targets)
    return _run_buildx(args, cwd=cwd, timeout=timeout_seconds).to_dict()


@mcp.tool()
def buildx_imagetools_inspect(
    image: str,
    raw: bool = False,
    format: str | None = None,
    builder: str | None = None,
) -> dict:
    """
    Inspect a manifest in a registry without pulling.

    Replaces `docker manifest inspect`. The standalone `docker manifest` command is in
    maintenance mode and lacks support for OCI image indexes, attestations, and
    annotations — `buildx imagetools inspect` is the path forward and handles both
    single-platform manifests and multi-platform manifest lists / OCI indexes.

    args:
        image: str - Image reference, e.g. "alpine:3.19" or "ghcr.io/org/repo@sha256:..."
        raw: bool - Return the raw manifest bytes (a JSON document) instead of the
                    human-rendered tree
        format: str - Go template format string (mutually exclusive with `raw`)
        builder: str - Override the active builder
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}.
                    When `raw=True` or `format="{{json .}}"`, `stdout` is a JSON document
                    the caller can parse.
    """
    args: list[str] = ["imagetools", "inspect"]
    if raw:
        args.append("--raw")
    if format is not None:
        args.extend(["--format", format])
    if builder is not None:
        args.extend(["--builder", builder])
    args.append(image)
    return _run_buildx(args, timeout=_TIMEOUT_QUERY).to_dict()


@mcp.tool()
def buildx_imagetools_create(
    target: str,
    sources: list[str],
    append: bool = False,
    dry_run: bool = False,
    annotations: list[str] | None = None,
    platforms: list[str] | None = None,
    files: list[str] | None = None,
    builder: str | None = None,
    timeout_seconds: float = _TIMEOUT_IMAGETOOLS_CREATE,
) -> dict:
    """
    Create a manifest list / OCI image index from existing per-platform tags.

    Replaces `docker manifest create` + `docker manifest push` — `imagetools create`
    builds the index and pushes it in one operation. The source tags must already be
    pushed to the registry; this command only stitches them together.

    args:
        target: str - Tag for the new manifest list (`-t`)
        sources: list[str] - Source image references to combine
        append: bool - Append to the existing manifest at `target` rather than replacing
        dry_run: bool - Print the resulting manifest without pushing
        annotations: list[str] - OCI annotations (repeatable; passed verbatim)
        platforms: list[str] - Filter source platforms when combining
        files: list[str] - Read source descriptors from files instead of refs
        builder: str - Override the active builder
        timeout_seconds: float - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not sources and not files:
        raise ValueError("buildx_imagetools_create requires at least one source ref or file")
    args: list[str] = ["imagetools", "create", "--tag", target]
    if append:
        args.append("--append")
    if dry_run:
        args.append("--dry-run")
    for annotation in annotations or []:
        args.extend(["--annotation", annotation])
    for platform in platforms or []:
        args.extend(["--platform", platform])
    for f in files or []:
        args.extend(["--file", f])
    if builder is not None:
        args.extend(["--builder", builder])
    args.extend(sources)
    return _run_buildx(args, timeout=timeout_seconds).to_dict()


@mcp.tool()
def buildx_ls() -> list:
    """
    List builder instances.

    returns: list - One dict per builder (parsed from `--format '{{json .}}'`)
    """
    result = _run_buildx(["ls", "--format", "{{json .}}"], timeout=_TIMEOUT_QUERY)
    _raise_on_failure(result, "ls")
    return _parse_json_lines(result.stdout)


@mcp.tool()
def buildx_inspect(name: str | None = None, bootstrap: bool = False) -> dict:
    """
    Inspect a builder instance.

    args:
        name: str - Builder name (defaults to the active builder)
        bootstrap: bool - Boot the builder if it isn't already running
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}.
                    stdout is human-readable; parse with the agent or call buildx_ls for JSON.
    """
    args: list[str] = ["inspect"]
    if bootstrap:
        args.append("--bootstrap")
    if name is not None:
        args.append(name)
    return _run_buildx(args, timeout=_TIMEOUT_QUERY).to_dict()


@mcp.tool()
def buildx_du(builder: str | None = None) -> list:
    """
    Report BuildKit cache disk usage as a list of records.

    args: builder: str - Override the active builder
    returns: list - One dict per cache record (parsed from `--format '{{json .}}'`)
    """
    args: list[str] = ["du", "--format", "{{json .}}"]
    if builder is not None:
        args.extend(["--builder", builder])
    result = _run_buildx(args, timeout=_TIMEOUT_QUERY)
    _raise_on_failure(result, "du")
    return _parse_json_lines(result.stdout)


@mcp.tool()
def buildx_prune(
    all: bool = False,
    filter: dict | None = None,
    keep_storage: str | None = None,
    reserved_space: str | None = None,
    max_used_space: str | None = None,
    min_free_space: str | None = None,
    builder: str | None = None,
    timeout_seconds: float = _TIMEOUT_PRUNE,
) -> dict:
    """
    Remove BuildKit cache entries.

    Destructive: this tool always passes `--force` because no interactive prompt is
    available under MCP. Pair with `buildx_du` first to inventory what would be removed.

    args:
        all: bool - Include internal/frontend images
        filter: dict - Filter values (e.g. {"until": "24h", "type": "exec.cachemount"})
        keep_storage: str - DEPRECATED; older buildx flag. Use `reserved_space` instead.
        reserved_space: str - Amount of disk to always keep (e.g. "10GB")
        max_used_space: str - Maximum disk space the cache may use (e.g. "20GB")
        min_free_space: str - Target amount of free disk after pruning (e.g. "5GB")
        builder: str - Override the active builder
        timeout_seconds: float - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args: list[str] = ["prune", "--force"]
    if all:
        args.append("--all")
    for key, value in (filter or {}).items():
        args.extend(["--filter", f"{key}={value}"])
    if keep_storage is not None:
        args.extend(["--keep-storage", keep_storage])
    if reserved_space is not None:
        args.extend(["--reserved-space", reserved_space])
    if max_used_space is not None:
        args.extend(["--max-used-space", max_used_space])
    if min_free_space is not None:
        args.extend(["--min-free-space", min_free_space])
    if builder is not None:
        args.extend(["--builder", builder])
    return _run_buildx(args, timeout=timeout_seconds).to_dict()


@mcp.tool()
def buildx_create(
    name: str | None = None,
    driver: str | None = None,
    driver_opts: dict | None = None,
    use: bool = False,
    bootstrap: bool = False,
    platforms: list[str] | None = None,
    config: str | None = None,
    node_name: str | None = None,
    append: bool = False,
) -> dict:
    """
    Create a new builder instance.

    args:
        name: str - Name for the new builder (defaults to a generated name)
        driver: str - BuildKit driver (e.g. "docker-container", "kubernetes", "remote")
        driver_opts: dict - Driver-specific options (each becomes `--driver-opt KEY=VALUE`)
        use: bool - Set the new builder as the current one
        bootstrap: bool - Boot the builder immediately
        platforms: list[str] - Platforms the builder advertises
        config: str - Path to a buildkitd config file
        node_name: str - Node name within the builder (for multi-node builders)
        append: bool - Append a node to an existing builder named `name`
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args: list[str] = ["create"]
    if driver is not None:
        args.extend(["--driver", driver])
    for key, value in (driver_opts or {}).items():
        args.extend(["--driver-opt", f"{key}={value}"])
    if use:
        args.append("--use")
    if bootstrap:
        args.append("--bootstrap")
    for platform in platforms or []:
        args.extend(["--platform", platform])
    if config is not None:
        args.extend(["--config", config])
    if node_name is not None:
        args.extend(["--node", node_name])
    if append:
        args.append("--append")
    if name is not None:
        args.extend(["--name", name])
    return _run_buildx(args, timeout=_TIMEOUT_QUERY).to_dict()


@mcp.tool()
def buildx_use(name: str, default: bool = False, global_default: bool = False) -> dict:
    """
    Switch the current builder.

    args:
        name: str - Builder name to activate
        default: bool - Set as default for this context
        global_default: bool - Set as default across all contexts
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args: list[str] = ["use"]
    if default:
        args.append("--default")
    if global_default:
        args.append("--global")
    args.append(name)
    return _run_buildx(args, timeout=_TIMEOUT_QUERY).to_dict()


@mcp.tool()
def buildx_rm(
    name: str | None = None,
    all_inactive: bool = False,
    keep_state: bool = False,
    keep_daemon: bool = False,
    force: bool = False,
) -> dict:
    """
    Remove a builder instance.

    args:
        name: str - Builder name to remove (mutually exclusive with `all_inactive`)
        all_inactive: bool - Remove every inactive builder
        keep_state: bool - Keep the BuildKit state volume
        keep_daemon: bool - Keep the BuildKit daemon process running
        force: bool - Force removal even if the builder is in use
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not name and not all_inactive:
        raise ValueError("buildx_rm requires either `name` or `all_inactive=True`")
    args: list[str] = ["rm"]
    if all_inactive:
        args.append("--all-inactive")
    if keep_state:
        args.append("--keep-state")
    if keep_daemon:
        args.append("--keep-daemon")
    if force:
        args.append("--force")
    if name is not None:
        args.append(name)
    return _run_buildx(args, timeout=_TIMEOUT_QUERY).to_dict()
