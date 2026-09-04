"""Tools for Docker Scout: what is wrong with an image, and what to do about it."""

# library of mcp tools for `docker scout`.
#
# Scout is a CLI plugin that talks to Docker's vulnerability database. Most operations
# require `docker login` against Docker Hub to fetch policy data and per-image scans;
# anonymous calls work for basic CVE listing on public images but degrade for the
# `recommendations` and policy-related subcommands.
#
# Scout is the first consumer of the remote-exec fallback in `_cli.py`: when the target host is
# reached over ssh:// and this machine has no scout plugin (or no `docker` binary at all), the
# subcommand runs on that host instead of failing. Scout is the simplest shape for it - every
# subcommand takes image references and reads nothing from the local filesystem, so there is nothing
# to stage; the one exception (`scout_compare`'s `to`, which may name a local directory or archive)
# is refused rather than resolved against the remote filesystem. Consequence to keep in mind: Hub
# credentials then come from the *remote* user's `~/.docker/config.json`, so an anonymous-capable
# call may succeed while a policy-dependent one reports an auth failure in `raw.stderr`.

import json
from pathlib import Path
from typing import Literal

from docker_mcp.exceptions import ToolInputError
from docker_mcp.server import tool
from docker_mcp.tools._cli import (
    CliResult,
    remote_exec_cli,
    require_plugin,
    run_docker,
    safe_positional,
    should_remote_exec,
)

# Scout calls are CDN-backed network queries; 5 minutes is plenty for any one image.
_TIMEOUT_SCOUT = 300.0

# Scout's severity vocabulary, shared by `cves` and `compare`. Closed set, verified against
# `docker scout cves --help` ("Filter by severity, comma separated (default [], accepts:
# critical, high, medium, low, unspecified])"). Constraining it as a Literal makes an invalid
# value a schema-validation failure rather than a CLI error the agent has to interpret.
Severity = Literal["critical", "high", "medium", "low", "unspecified"]


def _refuse_local_path_args(candidates: dict[str, str | None]) -> None:
    """
    Refuse a parameter that names an existing *local* path when the call is about to run remotely.

    Only reached on the remote-exec path. Existence is the test rather than the value's shape, because
    an image reference and a relative path are not distinguishable by syntax (`org/app:v1` contains a
    '/' too). A path that exists here would resolve on the remote host to something else or nothing at
    all, so refusing names the cause; a value that is not a local path passes through untouched.
    """
    for name, value in candidates.items():
        if value and Path(value).exists():
            raise ToolInputError(
                f"Refusing to run `docker scout` on the remote host with {name}={value!r}: that names a path on "
                f"the host running this MCP server, but with no local scout plugin available the command runs "
                f"on the target host over SSH, where the path means something else (or nothing). Pass an image "
                f"reference instead, or install the docker CLI and its scout plugin on this host."
            )


def _run_scout(
    args: list[str],
    *,
    timeout: float = _TIMEOUT_SCOUT,
    host: str | None = None,
    local_path_args: dict[str, str | None] | None = None,
) -> CliResult:
    """
    Run `docker scout <args...>`, locally or - with no usable local plugin - on the ssh:// host itself.

    args:
        args - the scout subcommand argv, without the leading `scout`
        timeout - seconds allowed for the call (also bounds the SSH handshake on the remote path)
        host - configured host label, or None for the default host
        local_path_args - `{param: value}` pairs that may name a local path; each is refused on the
                          remote path if it exists here (see `_refuse_local_path_args`)
    returns: CliResult - the same shape from either backend
    """
    if should_remote_exec(host, plugin="scout"):
        _refuse_local_path_args(local_path_args or {})
        return remote_exec_cli(host, ["scout", *args], timeout=timeout)
    require_plugin("scout")
    return run_docker(["scout", *args], timeout=timeout, host=host)


# Scout format values that emit JSON, across every subcommand that takes `--format`. Scout names
# these after the *schema* rather than the encoding ("sarif", "spdx", "cyclonedx", "gitlab", "sbom"
# are all JSON documents), so keying the parse on the literal string "json" only ever worked for
# `compare` and `sbom` - and silently returned unparsed text for the rest.
_JSON_FORMATS = frozenset({"json", "sarif", "spdx", "gitlab", "sbom", "cyclonedx"})


def _maybe_parse_json(text: str, format: str) -> dict | list | str | None:
    """Parse `text` as JSON when `format` names a JSON-emitting format, else return the raw text."""
    if format not in _JSON_FORMATS:
        return text
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text


@tool()
def scout_cves(
    image: str,
    only_fixed: bool = False,
    only_severity: list[Severity] | None = None,
    ignore_base: bool = False,
    format: Literal["packages", "sarif", "spdx", "gitlab", "markdown", "sbom"] = "sarif",
    platform: str | None = None,
    host: str | None = None,
) -> dict:
    """
    List vulnerabilities (CVEs) in an image via Docker Scout.

    Anonymous scans work for public images; Hub policy enforcement and richer recommendations need
    `docker login` on the host that runs the CLI - this server's host, or the target `ssh://` host
    itself when no local scout plugin is installed. Start with `scout_quickview` for a
    per-severity summary; `scout_sbom` inventories packages without vulnerability matching.
    Does not raise on a non-zero CLI exit (a missing scout plugin still raises) - inspect
    `raw.stderr`.

    args:
        image - Image reference (a tag or a digest)
        only_fixed - Only report CVEs with a fixed version available
        only_severity - Filter to these severities (omit for all)
        ignore_base - Exclude CVEs introduced by the base image
        format - Parsed into `result` as JSON: "sarif" (default, the standard vulnerability-report
            schema), "spdx", "gitlab", "sbom". Returned verbatim as text: "packages" (Scout's own
            default, grouped by package), "markdown". There is no plain "json" for this subcommand
        platform - Platform of the image to analyze, e.g. "linux/amd64"
    returns: dict - {"format": <format>, "result": <parsed-json-or-raw-text>,
                     "raw": <CliResult dict>}
    """
    args: list[str] = ["cves", "--format", format]
    if only_fixed:
        args.append("--only-fixed")
    if only_severity:
        args.extend(["--only-severity", ",".join(only_severity)])
    if ignore_base:
        args.append("--ignore-base")
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args, host=host)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@tool()
def scout_quickview(image: str, platform: str | None = None, host: str | None = None) -> dict:
    """
    Render a compact summary of an image's CVE posture.

    The fastest triage step - counts per severity plus base-image status. Drill into individual
    findings with `scout_cves`, which unlike this tool can emit machine-readable JSON; get upgrade
    suggestions with `scout_recommendations`.
    Output is plain text only: `docker scout quickview` has no output-format option, so `result`
    is always the rendered text rather than a parsed document.
    Does not raise on a non-zero CLI exit (a missing scout plugin still raises) - inspect
    `raw.stderr`.

    args:
        image - Image reference
        platform - Platform of the image to analyze, e.g. "linux/amd64"
    returns: dict - {"result": <rendered text>, "raw": <CliResult dict>}
    """
    args: list[str] = ["quickview"]
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args, host=host)
    return {"result": result.stdout, "raw": result.to_dict()}


@tool()
def scout_recommendations(
    image: str,
    only_refresh: bool = False,
    only_update: bool = False,
    tag: str | None = None,
    platform: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Suggest base-image upgrades for an image.

    Computed against Docker Scout's catalog; generally needs `docker login` on the host that runs the
    CLI (the target `ssh://` host itself when no local scout plugin is installed) to return useful
    results for private or rarely-scanned base images. The natural follow-up to `scout_cves` when the
    fix is a newer base image.
    Output is plain text only: `docker scout recommendations` has no output-format option, so
    `result` is always the rendered text rather than a parsed document.
    Does not raise on a non-zero CLI exit (a missing scout plugin still raises) - inspect
    `raw.stderr`.

    args:
        image - Image reference
        only_refresh - Only show "refresh" recommendations (same major/minor)
        only_update - Only show "update" recommendations (newer minor/major)
        tag - Restrict to suggestions matching this tag pattern
        platform - Platform of the image to analyze
    returns: dict - {"result": <rendered text>, "raw": <CliResult dict>}
    """
    args: list[str] = ["recommendations"]
    if only_refresh:
        args.append("--only-refresh")
    if only_update:
        args.append("--only-update")
    if tag is not None:
        args.extend(["--tag", tag])
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args, host=host)
    return {"result": result.stdout, "raw": result.to_dict()}


@tool()
def scout_compare(
    image: str,
    to: str | None = None,
    to_env: str | None = None,
    to_latest: bool = False,
    only_severity: list[Severity] | None = None,
    ignore_unchanged: bool = False,
    format: Literal["json", "markdown", "text"] = "json",
    platform: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Compare two image references and report the CVE delta.

    Exactly one of `to`, `to_env`, or `to_latest=True` must be supplied to identify the comparison
    target. Use it after a rebuild to check the new image against the old (`scout_cves` scans a
    single image).
    Does not raise on a non-zero CLI exit (a missing scout plugin still raises) - inspect
    `raw.stderr`. Raises ToolInputError if `to` names a local directory/archive while the call has to run
    on a remote `ssh://` host (no local scout plugin): the file is not staged, so it would resolve
    against that host's filesystem instead.

    args:
        image - The new / candidate image reference
        to - Compare against this image reference, directory, or archive (a local directory/archive
                      only when the CLI runs on this host - see above)
        to_env - Compare against an image associated with this Scout environment
        to_latest - Compare against the latest scan of `image`
        only_severity - Filter to these severities (omit for all)
        ignore_unchanged - Exclude unchanged packages from the diff
        format - Output format; only "json" (the default) is parsed into `result`
        platform - Platform of the image to analyze
    returns: dict - {"format": <format>, "result": <parsed-json-or-raw-text>,
                     "raw": <CliResult dict>}
    """
    targets = [bool(to), bool(to_env), bool(to_latest)]
    if sum(targets) != 1:
        raise ToolInputError("scout_compare requires exactly one of `to`, `to_env`, or `to_latest=True`")
    args: list[str] = ["compare", "--format", format]
    if to is not None:
        args.extend(["--to", to])
    if to_env is not None:
        args.extend(["--to-env", to_env])
    if to_latest:
        args.append("--to-latest")
    if only_severity:
        args.extend(["--only-severity", ",".join(only_severity)])
    if ignore_unchanged:
        args.append("--ignore-unchanged")
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args, host=host, local_path_args={"to": to})
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@tool()
def scout_sbom(
    image: str,
    format: Literal["list", "json", "spdx", "cyclonedx"] = "spdx",
    platform: str | None = None,
    host: str | None = None,
) -> dict:
    """
    Generate a Software Bill of Materials (SBOM) for an image.

    Package inventory only - `scout_cves` adds vulnerability matching on top. SBOMs can be large;
    captured stdout is subject to MAX_CLI_OUTPUT_BYTES and may be truncated for big images. If
    that's a concern, run `docker scout sbom -o file.json ...` on the host and load the file
    separately.
    Does not raise on a non-zero CLI exit (a missing scout plugin still raises) - inspect
    `raw.stderr`.

    args:
        image - Image reference
        format - "spdx" (default, SPDX JSON), "cyclonedx" (CycloneDX JSON), "json" (Scout's native
                      JSON), or "list" (plain-text package list)
        platform - Platform of the image to analyze
    returns: dict - {"format", "result", "raw": <CliResult dict>}. `result` is a parsed dict when
                    `format` is "spdx"/"cyclonedx"/"json" and stdout parses cleanly; for "list" or a
                    parse failure it's the raw text.
    """
    args: list[str] = ["sbom", "--format", format]
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args, host=host)
    # "spdx", "cyclonedx" and "json" are all JSON documents and are parsed; "list" is plain text.
    # `_JSON_FORMATS` is the single place that distinction lives, shared with the other scout tools.
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}
