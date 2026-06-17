# library of mcp tools for `docker scout`.
#
# Scout is a CLI plugin that talks to Docker's vulnerability database. Most operations
# require `docker login` against Docker Hub to fetch policy data and per-image scans;
# anonymous calls work for basic CVE listing on public images but degrade for the
# `recommendations` and policy-related subcommands.

import json

from docker_mcp.server import tool
from docker_mcp.tools._cli import CliResult, require_plugin, run_docker, safe_positional

# Scout calls are CDN-backed network queries; 5 minutes is plenty for any one image.
_TIMEOUT_SCOUT = 300.0


def _run_scout(args: list[str], *, timeout: float = _TIMEOUT_SCOUT) -> CliResult:
    require_plugin("scout")
    return run_docker(["scout", *args], timeout=timeout)


def _maybe_parse_json(text: str, format: str) -> dict | list | str | None:
    """Parse `text` as JSON when `format=='json'`, otherwise return the raw text."""
    if format != "json":
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
    only_severity: list[str] | None = None,
    ignore_base: bool = False,
    format: str = "json",
    platform: str | None = None,
) -> dict:
    """
    List vulnerabilities (CVEs) in an image via Docker Scout.

    Anonymous scans work for public images; Hub policy enforcement and richer recommendations need
    `docker login` on the host running this MCP server.

    args:
        image - Image reference (a tag or a digest)
        only_fixed - Only report CVEs with a fixed version available
        only_severity - Filter to severities: "critical", "high", "medium", "low", "unspecified"
        ignore_base - Exclude CVEs introduced by the base image
        format - Output format: "json" (default; parsed into the return dict),
                      "sarif", "spdx", "list", "markdown", or "text"
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
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@tool()
def scout_quickview(image: str, format: str = "json", platform: str | None = None) -> dict:
    """
    Render a compact summary of an image's CVE posture.

    args:
        image - Image reference
        format - Output format: "json" (default) or "text"
        platform - Platform of the image to analyze, e.g. "linux/amd64"
    returns: dict - {"format": <format>, "result": <parsed-json-or-raw-text>,
                     "raw": <CliResult dict>}
    """
    args: list[str] = ["quickview", "--format", format]
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@tool()
def scout_recommendations(
    image: str,
    only_refresh: bool = False,
    only_update: bool = False,
    tag: str | None = None,
    format: str = "json",
    platform: str | None = None,
) -> dict:
    """
    Suggest base-image upgrades for an image.

    Computed against Docker Scout's catalog; generally needs `docker login` on the host running this
    MCP server to return useful results for private or rarely-scanned base images.

    args:
        image - Image reference
        only_refresh - Only show "refresh" recommendations (same major/minor)
        only_update - Only show "update" recommendations (newer minor/major)
        tag - Restrict to suggestions matching this tag pattern
        format - Output format: "json" (default) or "text"
        platform - Platform of the image to analyze
    returns: dict - {"format": <format>, "result": <parsed-json-or-raw-text>,
                     "raw": <CliResult dict>}
    """
    args: list[str] = ["recommendations", "--format", format]
    if only_refresh:
        args.append("--only-refresh")
    if only_update:
        args.append("--only-update")
    if tag is not None:
        args.extend(["--tag", tag])
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@tool()
def scout_compare(
    image: str,
    to: str | None = None,
    to_env: str | None = None,
    to_latest: bool = False,
    only_severity: list[str] | None = None,
    ignore_unchanged: bool = False,
    format: str = "json",
    platform: str | None = None,
) -> dict:
    """
    Compare two image references and report the CVE delta.

    Exactly one of `to`, `to_env`, or `to_latest=True` must be supplied to identify
    the comparison target.

    args:
        image - The new / candidate image reference
        to - Compare against this image reference, directory, or archive
        to_env - Compare against an image associated with this Scout environment
        to_latest - Compare against the latest scan of `image`
        only_severity - Filter to severities ("critical", "high", "medium", "low", "unspecified")
        ignore_unchanged - Exclude unchanged packages from the diff
        format - Output format: "json" (default), "markdown", or "text"
        platform - Platform of the image to analyze
    returns: dict - {"format": <format>, "result": <parsed-json-or-raw-text>,
                     "raw": <CliResult dict>}
    """
    targets = [bool(to), bool(to_env), bool(to_latest)]
    if sum(targets) != 1:
        raise ValueError("scout_compare requires exactly one of `to`, `to_env`, or `to_latest=True`")
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
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@tool()
def scout_sbom(
    image: str,
    format: str = "spdx",
    platform: str | None = None,
) -> dict:
    """
    Generate a Software Bill of Materials (SBOM) for an image.

    SBOMs can be large; captured stdout is subject to MAX_CLI_OUTPUT_BYTES and may be truncated for
    big images. If that's a concern, run `docker scout sbom -o file.json …` on the host and load the
    file separately.

    args:
        image - Image reference
        format - SBOM format: "spdx" (default, SPDX JSON), "cyclonedx" (CycloneDX JSON),
                      "json" (Scout's native JSON), "list" (plain-text package list)
        platform - Platform of the image to analyze
    returns: dict - {"format", "result", "raw": <CliResult dict>}. `result` is a parsed dict when
                    `format` is "spdx"/"cyclonedx"/"json" and stdout parses cleanly; for "list" or a
                    parse failure it's the raw text.
    """
    args: list[str] = ["sbom", "--format", format]
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(safe_positional(image, "image"))
    result = _run_scout(args)
    # SPDX and CycloneDX are both JSON; the cyclonedx-xml variant returns XML.
    parse_as_json = format in {"spdx", "cyclonedx", "json"}
    parsed = _maybe_parse_json(result.stdout, "json") if parse_as_json else result.stdout
    return {"format": format, "result": parsed, "raw": result.to_dict()}
