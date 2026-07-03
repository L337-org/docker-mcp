# library of mcp tools for `docker buildx`.
#
# Buildx is a CLI plugin layered on BuildKit; it covers multi-platform builds, modern
# cache export/import, attestations (SBOM/provenance), and manifest-list operations.
# These tools wrap the CLI via tools/_cli.py for cross-platform safety.

from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    parse_json_or_ndjson,
    parse_ndjson,
    raise_on_cli_failure,
    require_plugin,
    run_docker,
    safe_positional,
)

# Per-operation timeout ceilings (seconds). Builds and pulls against slow registries or
# large contexts routinely run for many minutes, so they get longer ceilings than queries.
_TIMEOUT_QUERY = 60.0
_TIMEOUT_BUILD = 1800.0
_TIMEOUT_BAKE = 1800.0
_TIMEOUT_IMAGETOOLS_CREATE = 600.0
_TIMEOUT_PRUNE = 600.0


def _run_buildx(args: list[str], *, cwd: str | None = None, timeout: float, host: str | None = None) -> CliResult:
    require_plugin("buildx")
    return run_docker(["buildx", *args], cwd=cwd, timeout=timeout, host=host)


@tool()
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
    host: str | None = None,
) -> dict:
    """
    Build an image with BuildKit via `docker buildx build`.

    Replaces the legacy `image_build` tool when you need any of: multi-platform output
    (`platforms`), modern cache export (`cache_from`/`cache_to`), SBOM or provenance
    attestations, build secrets, or multi-stage builds with `target`. Always runs with
    `--progress=plain` so output is captured rather than redrawn on a TTY.

    args:
        context - Build context: a filesystem path or Git/HTTP URL (verbatim; no `~`/glob expansion).
                       The `-` stdin-tarball form is NOT supported (stdin isn't forwarded — it'd block
                       on the server's own stdin); serve a pre-packed tarball over HTTP instead.
        tags - Image references to apply (`-t`, repeatable)
        platforms - Target platforms, e.g. ["linux/amd64", "linux/arm64"]
        file - Dockerfile path (relative to context unless absolute)
        build_args - Build-time variables (each becomes `--build-arg KEY=VALUE`)
        build_contexts - Additional named build contexts (e.g. {"deps": "./vendor"})
        labels - Labels to set on the resulting image (each becomes `--label KEY=VALUE`)
        annotations - OCI manifest annotations (passed verbatim, repeatable)
        target - Target build stage to stop at
        push - Push the result to the registry (mutually exclusive with `load`)
        load - Load the result into the local image store (single-platform builds only)
        output - Custom `--output` specs (e.g. ["type=tar,dest=out.tar"])
        no_cache - Do not use cache when building
        no_cache_filter - Stage names to exclude from caching
        pull - Always attempt to pull a newer version of each base image
        cache_from - Cache import specs, e.g. ["type=registry,ref=user/img:cache"]
        cache_to - Cache export specs
        builder - Override the active builder
        sbom - Shorthand for `--attest=type=sbom`; pass "true" or a config string
        provenance - Shorthand for `--attest=type=provenance`; pass "true", "false", or a config string
        attest - Custom attestation specs (repeatable)
        secret - Secret specs (e.g. ["id=npmrc,src=/home/user/.npmrc"] or ["id=npmrc,env=NPM_TOKEN"]).
                            `~` in `src=` is NOT expanded (by this tool or the CLI) — use an absolute path.
        ssh - SSH agent socket/key specs (e.g. ["default"], using $SSH_AUTH_SOCK)
        timeout_seconds - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if context == "-":
        raise ValueError(
            "buildx_build: context='-' (read a tarball from stdin) is not supported by this "
            "tool because we don't forward stdin to the buildx subprocess — `-` would block "
            "on the MCP server's own stdin. Use a filesystem path or an HTTP/Git URL instead, "
            "or pre-stage the context on disk."
        )
    if push and load:
        raise ValueError(
            "buildx_build: `push` and `load` are mutually exclusive; --load only works for "
            "single-platform builds loaded into the local image store, --push uploads to a "
            "registry. Pick one (or use `output=` for a custom output spec)."
        )
    args: list[str] = ["build", "--progress=plain"]
    for tag in tags or []:
        args.extend(["--tag", tag])
    # buildx documents `--platform` as a comma-separated list (e.g. `linux/amd64,linux/arm64`).
    # The underlying flag is a stringArray, so repeating it would also work, but the comma
    # form is the canonical invocation shown in all upstream docs.
    if platforms:
        args.extend(["--platform", ",".join(platforms)])
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
    args.append(safe_positional(context, "build context"))
    return _run_buildx(args, timeout=timeout_seconds, host=host).to_dict()


@tool()
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
    host: str | None = None,
) -> dict:
    """
    Build multiple targets defined in a bake file (HCL, JSON, or compose).

    args:
        targets - Bake targets to build (default: the `default` group)
        files - Bake file paths (`-f`, repeatable)
        set_overrides - Per-target overrides, e.g. ["app.platform=linux/amd64"]
        push - Push results to the registry
        load - Load results into the local image store
        no_cache - Do not use cache when building
        pull - Always pull a newer base image
        builder - Override the active builder
        cwd - Working directory containing the bake file (defaults to the server's cwd)
        timeout_seconds - Subprocess timeout (default 1800s)
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
    return _run_buildx(args, cwd=cwd, timeout=timeout_seconds, host=host).to_dict()


@tool()
def buildx_imagetools_inspect(
    image: str,
    raw: bool = False,
    format: str | None = None,
    builder: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Inspect a manifest in a registry without pulling.

    Replaces `docker manifest inspect`. The standalone `docker manifest` command is in
    maintenance mode and lacks support for OCI image indexes, attestations, and
    annotations — `buildx imagetools inspect` is the path forward and handles both
    single-platform manifests and multi-platform manifest lists / OCI indexes. Uses the docker
    CLI's credential store; `registry_manifest` answers the same question over direct HTTPS
    with no daemon or plugin.

    args:
        image - Image reference, e.g. "alpine:3.19" or "ghcr.io/org/repo@sha256:..."
        raw - Return the raw manifest bytes (a JSON document) instead of the
                    human-rendered tree
        format - Go template format string (mutually exclusive with `raw`)
        builder - Override the active builder
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}.
                    When `raw=True` or `format="{{json .}}"`, `stdout` is a JSON document
                    the caller can parse.
    """
    if raw and format is not None:
        raise ValueError(
            "buildx_imagetools_inspect: `raw` and `format` are mutually exclusive — `raw` "
            "always emits the unmodified manifest JSON, while `format` runs a Go template "
            "against a rendered view. Pick one."
        )
    args: list[str] = ["imagetools", "inspect"]
    if raw:
        args.append("--raw")
    if format is not None:
        args.extend(["--format", format])
    if builder is not None:
        args.extend(["--builder", builder])
    args.append(safe_positional(image, "image"))
    return _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def buildx_imagetools_create(
    target: str,
    sources: list[str],
    append: bool = False,
    dry_run: bool = False,
    annotations: list[str] | None = None,
    platforms: list[str] | None = None,
    descriptor_files: list[str] | None = None,
    builder: str | None = None,
    timeout_seconds: float = _TIMEOUT_IMAGETOOLS_CREATE,
    host: str | None = None,
) -> dict:
    """
    Create a manifest list / OCI image index from existing per-platform tags.

    Replaces `docker manifest create` + `docker manifest push` — builds the index and pushes it in
    one operation. Source tags must already be pushed; this only stitches them together.

    args:
        target - Tag for the new manifest list (`-t`)
        sources - Source image references to combine
        append - Append to the existing manifest at `target` rather than replacing
        dry_run - Print the resulting manifest without pushing
        annotations - OCI annotations (repeatable; passed verbatim)
        platforms - Filter source platforms when combining
        descriptor_files - Files to read source descriptors from, instead of refs
        builder - Override the active builder
        timeout_seconds - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not sources and not descriptor_files:
        raise ValueError("buildx_imagetools_create requires at least one source ref or file")
    args: list[str] = ["imagetools", "create", "--tag", target]
    if append:
        args.append("--append")
    if dry_run:
        args.append("--dry-run")
    for annotation in annotations or []:
        args.extend(["--annotation", annotation])
    if platforms:
        args.extend(["--platform", ",".join(platforms)])
    for f in descriptor_files or []:
        args.extend(["--file", f])
    if builder is not None:
        args.extend(["--builder", builder])
    args.extend(safe_positional(s, "source") for s in sources)
    return _run_buildx(args, timeout=timeout_seconds, host=host).to_dict()


@tool()
def buildx_list(host: str | None = None) -> list:
    """
    List builder instances.

    returns: list - One dict per builder (parsed from `--format '{{json .}}'`).
                    If the captured stdout was truncated by MAX_CLI_OUTPUT_BYTES the
                    last (likely partial) record is dropped before parsing.
    """
    result = _run_buildx(["ls", "--format", "{{json .}}"], timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "buildx ls")
    return parse_ndjson(result.stdout, truncated=result.truncated, what="buildx ls output")


@tool()
def buildx_history_list(builder: str | None = None, host: str | None = None) -> list:
    """
    List recent build records (BuildKit build history), parsed from `--format '{{json .}}'`.

    Each record is a past build with its ref, name, status, step counts, and timestamps — useful for
    finding a build to drill into with `buildx_history_inspect`. Requires buildx >= v0.13 (older
    versions have no `history` subcommand and this raises with the CLI's "unknown command" error).

    args:
        builder - Builder instance to read history from (defaults to the active builder)
    returns: list - One dict per build record (ref, name, status, total/completed/cached steps, times)
    """
    args = ["history", "ls", "--format", "{{json .}}"]
    if builder is not None:
        args.extend(["--builder", safe_positional(builder, "builder name")])
    result = _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "buildx history ls")
    return parse_ndjson(result.stdout, truncated=result.truncated, what="buildx history ls output")


@tool()
def buildx_history_inspect(ref: str = "", builder: str | None = None, host: str | None = None) -> dict:
    """
    Inspect a single build record by ref, parsed from `--format json`.

    Returns the full record for one build — duration, materials, attestations, error (if any) — for
    debugging a failed or slow build found via `buildx_history_list`. Requires buildx >= v0.13.

    args:
        ref - Build record ref. Pass the `ref` field from `buildx_history_list` directly — it
                   reports a qualified "<builder>/<node>/<id>", but `history inspect` only accepts the
                   bare id, so this reduces it to the id and (unless `builder` is given) targets the
                   builder named in the ref. Empty/omitted inspects the most recent build; the `^N`
                   syntax (e.g. "^0" = latest) is also valid.
        builder - Builder instance the build ran on (defaults to the one in `ref`, else active)
    returns: dict - The parsed build record (or {"raw": <stdout>} if the output isn't a JSON object)
    """
    # `buildx history ls` emits ref as "<builder>/<node>/<id>", but `history inspect` only finds the
    # record by its bare id; the qualified form errors with "no record found". Reduce a qualified ref
    # to its id, and derive the builder from it when the caller didn't pass one. `^N` refs and bare
    # ids have no "/" and pass through unchanged.
    effective_builder = builder
    bare_ref = ref
    if ref:
        parts = ref.split("/")
        if len(parts) >= 3:
            bare_ref = parts[-1]
            if effective_builder is None:
                effective_builder = parts[0]
    args = ["history", "inspect", "--format", "json"]
    if effective_builder is not None:
        args.extend(["--builder", safe_positional(effective_builder, "builder name")])
    if bare_ref:
        args.append(safe_positional(bare_ref, "build ref"))
    result = _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "buildx history inspect")
    parsed = parse_json_or_ndjson(result.stdout, truncated=result.truncated, what="buildx history inspect output")
    return parsed if isinstance(parsed, dict) else {"raw": result.stdout}


@tool()
def buildx_inspect(name: str | None = None, bootstrap: bool = False, host: str | None = None) -> dict:
    """
    Inspect a builder instance.

    args:
        name - Builder name (defaults to the active builder)
        bootstrap - Boot the builder if it isn't already running
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}.
                    stdout is human-readable; parse with the agent or call buildx_list for JSON.
    """
    args: list[str] = ["inspect"]
    if bootstrap:
        args.append("--bootstrap")
    if name is not None:
        args.append(safe_positional(name, "builder name"))
    return _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def buildx_du(builder: str | None = None, host: str | None = None) -> list:
    """
    Report BuildKit cache disk usage as a list of records.

    A large cache can easily generate more output than MAX_CLI_OUTPUT_BYTES; if that
    happens the captured stdout is truncated and this tool drops the final (partial)
    record before parsing. For an exhaustive accounting on a busy builder, run
    `docker buildx du --format '{{json .}}'` on the host directly.

    args: builder - Override the active builder
    returns: list - One dict per cache record (parsed from `--format '{{json .}}'`)
    """
    args: list[str] = ["du", "--format", "{{json .}}"]
    if builder is not None:
        args.extend(["--builder", builder])
    result = _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host)
    raise_on_cli_failure(result, "buildx du")
    return parse_ndjson(result.stdout, truncated=result.truncated, what="buildx du output")


@tool()
def buildx_prune(
    all: bool = False,
    filter: dict | None = None,
    keep_storage: str | None = None,
    reserved_space: str | None = None,
    max_used_space: str | None = None,
    min_free_space: str | None = None,
    builder: str | None = None,
    timeout_seconds: float = _TIMEOUT_PRUNE,
    host: str | None = None,
) -> dict:
    """
    Remove BuildKit cache entries.

    Destructive: this tool always passes `--force` because no interactive prompt is
    available under MCP. Pair with `buildx_du` first to inventory what would be removed.

    args:
        all - Include internal/frontend images
        filter - Filter values (e.g. {"until": "24h", "type": "exec.cachemount"})
        keep_storage - DEPRECATED; older buildx flag. Use `reserved_space` instead.
        reserved_space - Amount of disk to always keep (e.g. "10GB")
        max_used_space - Maximum disk space the cache may use (e.g. "20GB")
        min_free_space - Target amount of free disk after pruning (e.g. "5GB")
        builder - Override the active builder
        timeout_seconds - Subprocess timeout (default 600s)
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
    return _run_buildx(args, timeout=timeout_seconds, host=host).to_dict()


@tool()
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
    host: str | None = None,
) -> dict:
    """
    Create a new builder instance.

    args:
        name - Name for the new builder (defaults to a generated name)
        driver - BuildKit driver (e.g. "docker-container", "kubernetes", "remote")
        driver_opts - Driver-specific options (each becomes `--driver-opt KEY=VALUE`)
        use - Set the new builder as the current one
        bootstrap - Boot the builder immediately
        platforms - Platforms the builder advertises
        config - Path to a buildkitd config file
        node_name - Node name within the builder (for multi-node builders)
        append - Append a node to an existing builder named `name`
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
    if platforms:
        args.extend(["--platform", ",".join(platforms)])
    if config is not None:
        args.extend(["--config", config])
    if node_name is not None:
        args.extend(["--node", node_name])
    if append:
        args.append("--append")
    if name is not None:
        args.extend(["--name", name])
    return _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def buildx_use(name: str, default: bool = False, global_default: bool = False, host: str | None = None) -> dict:
    """
    Select the active builder for subsequent buildx operations.

    Without `default` or `global_default` the switch applies only to the current CLI
    session. `default` persists the choice for the current Docker context; `global_default`
    persists across all Docker contexts. Use `buildx_list` to see available builders and their
    current status. To avoid switching the global default, pass a specific builder name
    directly via `buildx_build`'s `builder` parameter instead.

    args:
        name - Builder name to activate (from `buildx_list`)
        default - Persist as default builder for the current Docker context
        global_default - Persist as default builder across all Docker contexts
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    args: list[str] = ["use"]
    if default:
        args.append("--default")
    if global_default:
        args.append("--global")
    args.append(safe_positional(name, "builder name"))
    return _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host).to_dict()


@tool()
def buildx_remove(
    name: str | None = None,
    all_inactive: bool = False,
    keep_state: bool = False,
    keep_daemon: bool = False,
    force: bool = False,
    host: str | None = None,
) -> dict:
    """
    Remove a builder instance.

    args:
        name - Builder name to remove (mutually exclusive with `all_inactive`)
        all_inactive - Remove every inactive builder
        keep_state - Keep the BuildKit state volume
        keep_daemon - Keep the BuildKit daemon process running
        force - Force removal even if the builder is in use
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not name and not all_inactive:
        raise ValueError("buildx_remove requires either `name` or `all_inactive=True`")
    if name and all_inactive:
        raise ValueError(
            "buildx_remove: `name` and `all_inactive=True` are mutually exclusive — pass `name` to "
            "remove a specific builder, or `all_inactive=True` to sweep every inactive one."
        )
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
        args.append(safe_positional(name, "builder name"))
    return _run_buildx(args, timeout=_TIMEOUT_QUERY, host=host).to_dict()
