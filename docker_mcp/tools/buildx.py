"""Tools for BuildKit.

Building and baking images, inspecting and creating multi-platform manifests, and managing builder
instances and their history.
"""

# library of mcp tools for `docker buildx`.
#
# Buildx is a CLI plugin layered on BuildKit; it covers multi-platform builds, modern
# cache export/import, attestations (SBOM/provenance), and manifest-list operations.
# These tools wrap the CLI via tools/_cli.py for cross-platform safety.
#
# Remote-exec fallback (no local buildx plugin + an ssh:// target) splits this module three ways:
# the query/lifecycle tools name nothing local and just run there; `buildx_bake` stages a working
# directory like the compose tools; `buildx_create --config` and `buildx_imagetools_create --file`
# stage only the files they name. `buildx_build` is bespoke - its context needs `.dockerignore`-aware
# tarring and its `--build-context`/`--secret` values carry paths *inside* composite `key=value`
# tokens - and it refuses the flags whose effect would land on the wrong machine (see
# `_refuse_flags_that_resolve_on_the_wrong_host`).

from pathlib import Path

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    filter_args,
    parse_json_or_ndjson,
    parse_ndjson,
    RemoteStagingSession,
    raise_on_cli_failure,
    remote_cli_session,
    remote_exec_cli,
    remote_stage_and_exec,
    require_plugin,
    run_docker,
    run_in_session,
    safe_positional,
    should_remote_exec,
)

# Per-operation timeout ceilings (seconds). Builds and pulls against slow registries or
# large contexts routinely run for many minutes, so they get longer ceilings than queries.
_TIMEOUT_QUERY = 60.0
_TIMEOUT_BUILD = 1800.0
_TIMEOUT_BAKE = 1800.0
_TIMEOUT_IMAGETOOLS_CREATE = 600.0
_TIMEOUT_PRUNE = 600.0


def _run_buildx(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float,
    host: str | None = None,
    path_values: list[str] | None = None,
    stage_cwd: bool = False,
) -> CliResult:
    """Run `docker buildx <args...>`, locally or - with no local buildx plugin - on the ssh:// host.

    `stage_cwd` and `path_values` are passed explicitly by the two tools that read local files, rather
    than recovered by scanning the argv: `buildx_bake` appends caller-supplied target names, so a
    target could be the very flag a scan looks for. Explicit values need no such assumption.

    Args:
        args - the buildx argv, without the leading `buildx`
        cwd - working directory for the command, or None for the server's own
        timeout - seconds allowed for the command
        host - configured host label, or None for the default host
        path_values - values in `args` naming local paths to reconcile with the remote host
        stage_cwd - True when the subcommand reads files from `cwd` (bake), so it is copied over
    returns: CliResult - the same shape from either backend
    """
    if should_remote_exec(host, plugin="buildx"):
        if stage_cwd or path_values:
            return remote_stage_and_exec(
                host,
                ["buildx", *args],
                cwd=cwd,
                timeout=timeout,
                path_values=path_values or (),
                stage_cwd=stage_cwd,
            )
        return remote_exec_cli(host, ["buildx", *args], timeout=timeout)
    require_plugin("buildx")
    return run_docker(["buildx", *args], cwd=cwd, timeout=timeout, host=host)


# --- buildx_build's own staging -------------------------------------------------------------------
#
# `buildx_build` cannot use the generic backend: its context needs `.dockerignore`-aware tarring, and
# two of its flags hide paths inside composite `key=value` tokens. It drives a staging session itself.


def _spec_component(spec: str, key: str) -> str | None:
    """The value of `key=` within a comma-separated buildx spec, or None if absent.

    buildx parses `--output`/`--cache-to`/`--secret` values as comma-separated `key=value` pairs, so a
    path inside one cannot be found by looking at the whole token. A value containing a comma cannot be
    expressed in that syntax at all (buildx has the same limitation), so splitting on commas is exact
    rather than approximate.

    Args:
        spec - one spec value, e.g. "type=local,dest=out" or "id=npmrc,src=/home/u/.npmrc"
        key - the component to read, e.g. "dest"
    returns: str | None - the component's value, or None when the spec does not carry it
    """
    for component in spec.split(","):
        name, separator, value = component.partition("=")
        if separator and name.strip() == key:
            return value
    return None


def _replace_spec_component(spec: str, key: str, new_value: str) -> str:
    """Rewrite one `key=` component of a buildx spec, leaving the rest of it untouched.

    Args:
        spec - the spec value to rewrite
        key - the component to replace, e.g. "src"
        new_value - the replacement value
    returns: str - the rewritten spec
    """
    parts = []
    for component in spec.split(","):
        name, separator, _ = component.partition("=")
        parts.append(f"{key}={new_value}" if separator and name.strip() == key else component)
    return ",".join(parts)


def _refuse_flags_that_resolve_on_the_wrong_host(
    *, output: list[str] | None, cache_to: list[str] | None, cache_from: list[str] | None, ssh: list[str] | None
) -> None:
    """Refuse build flags whose effect would land on the remote host instead of this one.

    Reached only on the remote-exec path, and deliberately a refusal rather than a best effort. A
    `dest=` output written into the staging directory would be deleted with it the moment the build
    finished; a local cache export would accumulate on the wrong disk; a local `cache_from` that is not
    there is *non-fatal* to BuildKit, so the build would silently run uncached; and `ssh=default` reads
    the executing host's `$SSH_AUTH_SOCK`, which is the remote user's agent, not the caller's (agent
    forwarding is not requested). Each of those either loses work or changes the build silently, which
    is worse than not running.

    `dest=-` is exempt for `output` **only**: that is stdout, which the result captures identically on
    both paths, and buildx rejects it for the exporters where it makes no sense (verified:
    `--output type=local,dest=-` fails with "dest cannot be stdout for local exporter"), so no
    exporter allow-list is needed here. A cache has no stdout form, so any `dest=`/`src=` on the cache
    flags is refused whatever its value.

    Args:
        output - `--output` specs
        cache_to - `--cache-to` specs
        cache_from - `--cache-from` specs
        ssh - `--ssh` specs
    raises: ToolInputError - any of the above is present
    """
    checks = (
        (
            "output",
            output,
            "dest",
            True,
            "the image or archive would be written into a temporary directory on that host that is deleted "
            "when the call returns. Push to a registry (`push=True`, or a `type=registry` output) instead",
        ),
        (
            "cache_to",
            cache_to,
            "dest",
            False,
            "the cache would be written to that host's disk rather than yours, where nothing later reads it. "
            "Export to a registry (`type=registry,ref=...`) instead",
        ),
        (
            "cache_from",
            cache_from,
            "src",
            False,
            "the cache would be read from that host, and a cache import that isn't there is *non-fatal* to "
            "BuildKit - so the build would silently run uncached rather than fail. Import from a registry "
            "(`type=registry,ref=...`) instead",
        ),
    )
    for flag, specs, key, stdout_exempt, consequence in checks:
        for spec in specs or []:
            value = _spec_component(spec, key)
            if value is None or (stdout_exempt and value == "-"):
                continue
            raise ToolInputError(
                f"buildx_build cannot honour {flag}={spec!r} against this host: this server has no local "
                f"buildx plugin, so the build runs on the target host over SSH and {key}={value!r} would "
                f"resolve on *that* machine - {consequence}, or run the build on a host with a local "
                f"docker CLI."
            )
    if ssh:
        raise ToolInputError(
            f"buildx_build cannot honour ssh={ssh!r} against this host: this server has no local buildx plugin, "
            f"so the build runs on the target host over SSH, where `--ssh` reads that host's $SSH_AUTH_SOCK - "
            f"the remote user's agent, not yours (this server does not request agent forwarding). Bake the "
            f"credential in with `--secret` instead, or run the build on a host with a local docker CLI."
        )


def _local_dockerfile(file: str | None, *, context_is_local: bool) -> Path | None:
    """Where `--file` points on *this* machine, or None when buildx would not read it from here.

    Three behaviours, all established by running buildx rather than reasoning about it, because the
    remote path must resolve `--file` exactly as the local backend would or it stages the wrong file:

    - With a local-directory context, `--file` resolves against the **CLI's working directory**, not the
      context - the opposite of what this tool's docstring used to claim. `-f Dockerfile.x ./ctx`
      reports "failed to read dockerfile: open Dockerfile.x: no such file" when the file exists only
      inside `./ctx`, while `-f ctx/Dockerfile.x ./ctx` reads it.
    - With a **URL** context, an **absolute** `--file` is still read from this filesystem: buildx
      transfers it as a separate dockerfile context (observed as `transferring dockerfile: 46B` plus a
      parse error from the local file's own contents).
    - With a URL context, a **relative** `--file` is resolved inside the *fetched* context, not here, so
      it must be left alone - resolving it locally could stage a same-named file that happens to sit in
      this server's working directory, silently building something else.

    Args:
        file - the `file` parameter as given, or None
        context_is_local - whether `context` names an existing local directory
    returns: Path | None - the local path buildx would read, else None
    raises: ToolInputError - a relative `file` cannot be resolved (this server's cwd is unavailable)
    """
    if file is None:
        return None
    path = Path(file)
    if path.is_absolute():
        return path
    if not context_is_local:
        return None  # resolved inside the fetched context; not ours to touch
    try:
        return Path.cwd() / path
    except OSError as exc:
        raise ToolInputError(
            f"buildx_build cannot resolve file={file!r}: it is relative to this server's working directory, "
            f"which is unavailable ({exc}). Pass an absolute path."
        ) from exc


def _replace_flag_value(args: list[str], flag: str, new_value: str) -> None:
    """Rewrite the value token following the first occurrence of `flag`, in place.

    Anchored on the flag rather than matching the value as a whole token: the value is rewritten
    because of where it sits in the argv this module just built, so an unrelated argument that happens
    to equal it cannot be caught by accident.

    Args:
        args - the argv to modify in place
        flag - the flag whose value to replace, e.g. "--file"
        new_value - the replacement
    """
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            args[index + 1] = new_value
            return


def _stage_composite_paths(session: RemoteStagingSession, args: list[str], flag: str, key: str) -> None:
    """Stage the local path inside each `flag`'s composite spec and rewrite that component, in place.

    Used for `--build-context name=path` and `--secret id=x,src=path`. A component that does not name
    an existing local path (an image ref, a URL, `env=`) is left exactly as it was, so only real local
    inputs are copied.

    Args:
        session - the open staging session
        args - the argv to modify in place
        flag - the repeatable flag to walk, e.g. "--secret"
        key - the component holding a path; "" means the whole value after `name=`
    """
    for index, token in enumerate(args):
        if token != flag or index + 1 >= len(args):
            continue
        spec = args[index + 1]
        if key:
            local = _spec_component(spec, key)
        else:
            _, _, local = spec.partition("=")
        if not local:
            continue
        source = Path(local)
        if not source.exists():
            continue
        staged = session.stage_tree(source) if source.is_dir() else session.stage_file(source)
        if key:
            args[index + 1] = _replace_spec_component(spec, key, staged)
        else:
            name, _, _ = spec.partition("=")
            args[index + 1] = f"{name}={staged}"


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
    # Not an enum: accepts "true"/"false"/"min"/"max" *or* an arbitrary `key=value,...` attestation
    # config string, so the value space is open.
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
    With no local buildx plugin and an `ssh://` target, the build runs on that host: a local `context`
    directory is copied there honouring `.dockerignore`, as are `file`, `build_contexts` and `secret`
    paths. Raises ToolInputError in that case for `output`/`cache_to` with a filesystem `dest=`,
    `cache_from` with a local `src=`, or any `ssh=` - each would resolve on the remote machine, losing
    the output or silently changing the build.

    args:
        context - Build context: a filesystem path or Git/HTTP URL (verbatim; no `~`/glob expansion).
                       The `-` stdin-tarball form is NOT supported (stdin isn't forwarded - it'd block
                       on the server's own stdin); serve a pre-packed tarball over HTTP instead. Copied
                       to the target host when it names a local directory and there is no local plugin.
        tags - Image references to apply (`-t`, repeatable)
        platforms - Target platforms, e.g. ["linux/amd64", "linux/arm64"]
        file - Dockerfile path. A relative path resolves against this server's working directory
                      (buildx's own rule), NOT against `context` - pass e.g. "ctx/Dockerfile" for a
                      Dockerfile inside the context directory "ctx".
        build_args - Build-time variables (each becomes `--build-arg KEY=VALUE`)
        build_contexts - Additional named build contexts (e.g. {"deps": "./vendor"})
        labels - Labels to set on the resulting image (each becomes `--label KEY=VALUE`)
        annotations - OCI manifest annotations (passed verbatim, repeatable)
        target - Target build stage to stop at
        push - Push the result to the registry (mutually exclusive with `load`)
        load - Load the result into the local image store (single-platform builds only)
        output - Custom `--output` specs (e.g. ["type=tar,dest=out.tar"]). A filesystem `dest=` is
                      refused when the build has to run on a remote host; `dest=-` (stdout) is fine.
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
                            `~` in `src=` is NOT expanded (by this tool or the CLI) - use an absolute path.
        ssh - SSH agent socket/key specs (e.g. ["default"], using $SSH_AUTH_SOCK). Refused when
                            the build has to run on a remote host: the socket read would be that host's.
        timeout_seconds - Subprocess timeout (default 1800s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if context == "-":
        raise ToolInputError(
            "buildx_build: context='-' (read a tarball from stdin) is not supported by this "
            "tool because we don't forward stdin to the buildx subprocess - `-` would block "
            "on the MCP server's own stdin. Use a filesystem path or an HTTP/Git URL instead, "
            "or pre-stage the context on disk."
        )
    if push and load:
        raise ToolInputError(
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
    if should_remote_exec(host, plugin="buildx"):
        return _run_buildx_build_remotely(
            args,
            context=context,
            file=file,
            output=output,
            cache_to=cache_to,
            cache_from=cache_from,
            ssh=ssh,
            timeout=timeout_seconds,
            host=host,
        ).to_dict()
    require_plugin("buildx")
    return run_docker(["buildx", *args], timeout=timeout_seconds, host=host).to_dict()


def _run_buildx_build_remotely(
    args: list[str],
    *,
    context: str,
    file: str | None,
    output: list[str] | None,
    cache_to: list[str] | None,
    cache_from: list[str] | None,
    ssh: list[str] | None,
    timeout: float,
    host: str | None,
) -> CliResult:
    """Stage a build's local inputs on the ssh:// host and run the build there.

    The context is staged only when it names an existing local directory - the inverse test to guessing
    which strings are URLs, which cannot be got right from syntax alone. A Git/HTTP context (or a path
    that does not exist here) is therefore passed through untouched, and the remote CLI fetches or
    reports it exactly as the local one would.

    The remote command gets **no working directory**, and every path rewritten here is absolute. Running
    it inside the staged context instead would let a relative `--file` resolve *there* - and buildx
    resolves `--file` against the CLI's own working directory (see `_local_dockerfile`), so a path the
    local CLI could not find would be found remotely inside the copied context. An in-context Dockerfile
    therefore becomes an absolute path under the staged copy; one outside the context, or beside a URL
    context, is copied on its own and pointed at the same way.

    Args:
        args - the fully-built buildx argv, ending with the context positional
        context - the context as the caller gave it
        file - the `file` parameter as the caller gave it
        output/cache_to/cache_from/ssh - checked for effects that would land on the wrong machine
        timeout - seconds allowed for the build
        host - configured host label, or None for the default host
    returns: CliResult - the build's outcome, in `run_docker`'s shape
    raises:
        ToolInputError - a refused flag (see `_refuse_flags_that_resolve_on_the_wrong_host`), or a
                     `file` that cannot be resolved against this server's working directory
    """
    # Before connecting: a refusal should not cost an SSH handshake and a context upload.
    _refuse_flags_that_resolve_on_the_wrong_host(output=output, cache_to=cache_to, cache_from=cache_from, ssh=ssh)
    staged_args = list(args)
    local_context = Path(context)
    context_is_local = local_context.is_dir()
    dockerfile = _local_dockerfile(file, context_is_local=context_is_local)
    with remote_cli_session(host, timeout=timeout) as session:
        relative_dockerfile: str | None = None
        staged_context: str | None = None
        if context_is_local:
            if dockerfile is not None:
                try:
                    relative_dockerfile = dockerfile.resolve().relative_to(local_context.resolve()).as_posix()
                except ValueError, OSError:
                    relative_dockerfile = None  # outside the context: staged on its own below
            # Passing the Dockerfile's relative path through means the exclusion pass keeps it even when
            # `.dockerignore` would have swept it up (`*.dockerfile`), matching an SDK-driven build.
            staged_context = session.stage_build_context(local_context, dockerfile=relative_dockerfile)
            staged_args[-1] = staged_context
        if relative_dockerfile is not None and staged_context is not None:
            # Absolute, not relative-plus-a-working-directory. Running in the staged context would make a
            # relative `--file` resolve *there*, so a path the local CLI could not find (it resolves
            # `--file` against the CLI's own cwd) could be found remotely inside the copied context - the
            # same build succeeding remotely and failing locally. Every path this backend rewrites is
            # absolute, and the command gets no working directory at all, so that cannot happen.
            _replace_flag_value(staged_args, "--file", session.join(staged_context, relative_dockerfile))
        elif dockerfile is not None and dockerfile.is_file():
            # Outside the context, or alongside a URL context: buildx reads it from this filesystem, so
            # it has to be copied. A `--file` naming nothing here is left verbatim instead of raising, so
            # the remote CLI reports it exactly as the local one would.
            _replace_flag_value(staged_args, "--file", session.stage_file(dockerfile))
        _stage_composite_paths(session, staged_args, "--build-context", "")
        _stage_composite_paths(session, staged_args, "--secret", "src")
        return run_in_session(session, ["buildx", *staged_args], timeout=timeout)


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

    Use it for multi-target builds declared in `docker-bake.hcl`/compose files; for a single
    Dockerfile target use `buildx_build`.
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result.

    args:
        targets - Bake targets to build (default: the `default` group)
        files - Bake file paths (`-f`, repeatable)
        set_overrides - Per-target overrides, e.g. ["app.platform=linux/amd64"]
        push - Push results to the registry
        load - Load results into the local image store
        no_cache - Do not use cache when building
        pull - Always pull a newer base image
        builder - Override the active builder
        cwd - Working directory containing the bake file (defaults to the server's cwd; copied to
                      the target host if no local plugin)
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
        # `safe_positional` for the same reason every other positional gets it: a target beginning with
        # '-' would be parsed by the CLI as a flag. It also means a target can never be mistaken for one
        # of bake's own flags - though `path_values` below is passed explicitly rather than relying on
        # that.
        args.extend(safe_positional(target, "bake target") for target in targets)
    return _run_buildx(
        args,
        cwd=cwd,
        timeout=timeout_seconds,
        host=host,
        stage_cwd=True,
        path_values=list(files or []),
    ).to_dict()


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
    annotations - `buildx imagetools inspect` is the path forward and handles both
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
        raise ToolInputError(
            "buildx_imagetools_inspect: `raw` and `format` are mutually exclusive - `raw` "
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

    Replaces `docker manifest create` + `docker manifest push` - builds the index and pushes it in
    one operation. Source tags must already be pushed; this only stitches them together. Verify
    the result with `buildx_imagetools_inspect`.
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result.

    args:
        target - Tag for the new manifest list (`-t`)
        sources - Source image references to combine
        append - Append to the existing manifest at `target` rather than replacing
        dry_run - Print the resulting manifest without pushing
        annotations - OCI annotations (repeatable; passed verbatim)
        platforms - Filter source platforms when combining
        descriptor_files - Files to read source descriptors from, instead of refs (copied to the
                            target host if no local plugin)
        builder - Override the active builder
        timeout_seconds - Subprocess timeout (default 600s)
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not sources and not descriptor_files:
        raise ToolInputError("buildx_imagetools_create requires at least one source ref or file")
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
    return _run_buildx(args, timeout=timeout_seconds, host=host, path_values=list(descriptor_files or [])).to_dict()


@tool()
def buildx_list(host: str | None = None) -> list:
    """
    List builder instances.

    Machine-parsed view of every builder; use `buildx_inspect` for one builder's human-readable
    detail and `buildx_use` to switch the default.
    Raises RemoteFailureError if the CLI call fails.

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

    Each record is a past build with its ref, name, status, step counts, and timestamps - useful for
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

    Returns the full record for one build - duration, materials, attestations, error (if any) -
    for debugging a failed or slow build found via `buildx_history_list`. Requires buildx >=
    v0.13.
    Raises RemoteFailureError if the CLI call fails.

    args:
        ref - Build record ref. Pass the `ref` field from `buildx_history_list` directly - it
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

    Human-readable detail (driver, status, supported platforms) for one builder; `buildx_list`
    returns machine-parsed JSON for all builders.
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result.

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

    A large cache can easily generate more output than MAX_CLI_OUTPUT_BYTES; if that happens the
    captured stdout is truncated and this tool drops the final (partial) record before parsing.
    For an exhaustive accounting on a busy builder, run `docker buildx du --format '{{json .}}'`
    on the host directly. Reclaim the cache with `buildx_prune` (`system_df` covers daemon-side
    disk, not builder cache).
    Raises RemoteFailureError if the CLI call fails.

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
    filters: dict | None = None,
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
        filters - Filter by attributes (e.g. {"until": "24h", "type": "exec.cachemount"})
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
    args.extend(filter_args(filters))
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
    # Not an enum: buildx drivers are pluggable (docker, docker-container, kubernetes, remote,
    # cloud, ...) and the set grows with the plugin, so pinning it would date badly.
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
    Create a new BuildKit builder instance.

    Needed when the default `docker` driver falls short: multi-platform builds and cache export
    require a `docker-container` (or `kubernetes`/`remote`) builder. Pass `use=True` to make it
    the default for later `buildx_build` calls (else switch with `buildx_use`); `bootstrap=True`
    starts the builder now rather than on first build.
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result.

    args:
        name - Name for the new builder (defaults to a generated name)
        driver - BuildKit driver (e.g. "docker-container", "kubernetes", "remote")
        driver_opts - Driver-specific options (each becomes `--driver-opt KEY=VALUE`)
        use - Set the new builder as the current one
        bootstrap - Boot the builder immediately
        platforms - Platforms the builder advertises
        config - Path to a buildkitd config file (copied to the target host if no local plugin);
            passed as `--buildkitd-config`, so this argument needs buildx >= 0.17
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
        # buildx renamed `--config` to `--buildkitd-config` in v0.17; the old spelling still parses
        # as a hidden alias but no longer appears in `--help`, so it is a removal risk rather than a
        # supported option. Using the current name means this argument needs buildx >= 0.17.
        args.extend(["--buildkitd-config", config])
    if node_name is not None:
        args.extend(["--node", node_name])
    if append:
        args.append("--append")
    if name is not None:
        args.extend(["--name", name])
    return _run_buildx(
        args, timeout=_TIMEOUT_QUERY, host=host, path_values=[config] if config is not None else None
    ).to_dict()


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

    Deletes a builder made by `buildx_create`, including its build cache unless keep_state=True;
    use `buildx_prune` to reclaim cache while keeping the builder.
    Does not raise on a non-zero CLI exit - inspect `returncode`/`stderr` in the result.

    args:
        name - Builder name to remove (mutually exclusive with `all_inactive`)
        all_inactive - Remove every inactive builder
        keep_state - Keep the BuildKit state volume
        keep_daemon - Keep the BuildKit daemon process running
        force - Force removal even if the builder is in use
    returns: dict - {"returncode": int, "stdout": str, "stderr": str, "truncated": bool}
    """
    if not name and not all_inactive:
        raise ToolInputError("buildx_remove requires either `name` or `all_inactive=True`")
    if name and all_inactive:
        raise ToolInputError(
            "buildx_remove: `name` and `all_inactive=True` are mutually exclusive - pass `name` to "
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
