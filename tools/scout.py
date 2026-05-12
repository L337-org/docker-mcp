# library of mcp tools for `docker scout`.
#
# Scout is a CLI plugin that talks to Docker's vulnerability database. Most operations
# require `docker login` against Docker Hub to fetch policy data and per-image scans;
# anonymous calls work for basic CVE listing on public images but degrade for the
# `recommendations` and policy-related subcommands.

import json

from server import mcp
from tools._cli import CliResult, require_plugin, run_docker

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


@mcp.tool()
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

    Anonymous scans work for public images, but Hub policy enforcement and richer
    recommendations require `docker login` on the host running this MCP server.

    args:
        image: str - Image reference (a tag or a digest)
        only_fixed: bool - Only report CVEs with a fixed version available
        only_severity: list[str] - Filter to these severities. Accepted values:
                                   "critical", "high", "medium", "low", "unspecified"
        ignore_base: bool - Exclude CVEs introduced by the base image
        format: str - Output format: "json" (default; parsed into the return dict),
                      "sarif", "spdx", "list", "markdown", or "text"
        platform: str - Platform of the image to analyze, e.g. "linux/amd64"
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
    args.append(image)
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@mcp.tool()
def scout_quickview(image: str, format: str = "json", platform: str | None = None) -> dict:
    """
    Render a compact summary of an image's CVE posture.

    args:
        image: str - Image reference
        format: str - Output format: "json" (default) or "text"
        platform: str - Platform of the image to analyze, e.g. "linux/amd64"
    returns: dict - {"format": <format>, "result": <parsed-json-or-raw-text>,
                     "raw": <CliResult dict>}
    """
    args: list[str] = ["quickview", "--format", format]
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(image)
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@mcp.tool()
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

    Recommendations are computed against Docker Scout's catalog and generally require
    `docker login` on the host running this MCP server to return useful results for
    private or rarely-scanned base images.

    args:
        image: str - Image reference
        only_refresh: bool - Only show "refresh" recommendations (same major/minor)
        only_update: bool - Only show "update" recommendations (newer minor/major)
        tag: str - Restrict to suggestions matching this tag pattern
        format: str - Output format: "json" (default) or "text"
        platform: str - Platform of the image to analyze
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
    args.append(image)
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@mcp.tool()
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
        image: str - The new / candidate image reference
        to: str - Compare against this image reference, directory, or archive
        to_env: str - Compare against an image associated with this Scout environment
        to_latest: bool - Compare against the latest scan of `image`
        only_severity: list[str] - Filter to these severities
                                   ("critical", "high", "medium", "low", "unspecified")
        ignore_unchanged: bool - Exclude unchanged packages from the diff
        format: str - Output format: "json" (default), "markdown", or "text"
        platform: str - Platform of the image to analyze
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
    args.append(image)
    result = _run_scout(args)
    return {"format": format, "result": _maybe_parse_json(result.stdout, format), "raw": result.to_dict()}


@mcp.tool()
def scout_sbom(
    image: str,
    format: str = "spdx",
    platform: str | None = None,
) -> dict:
    """
    Generate a Software Bill of Materials (SBOM) for an image.

    SBOMs can be large; the captured stdout is subject to the standard MAX_CLI_OUTPUT_BYTES
    cap and may be flagged as truncated for very large images. If that's a concern, run
    `docker scout sbom -o file.json …` on the host directly and load the file separately.

    args:
        image: str - Image reference
        format: str - SBOM format. Accepted values (per `docker scout sbom --format`):
                      - "spdx" (the default for this tool) — SPDX JSON
                      - "cyclonedx" — CycloneDX JSON
                      - "json" — Scout's native SBOM JSON (the CLI's own default)
                      - "list" — a plain-text list of packages, no schema
        platform: str - Platform of the image to analyze
    returns: dict - {"format": <format>, "result": <…>, "raw": <CliResult dict>}.
                    `result` is a parsed dict when `format` is one of "spdx", "cyclonedx",
                    or "json" (all JSON serializations) and the stdout parses cleanly;
                    when `format="list"` or the JSON fails to parse, `result` is the raw text.
    """
    args: list[str] = ["sbom", "--format", format]
    if platform is not None:
        args.extend(["--platform", platform])
    args.append(image)
    result = _run_scout(args)
    # SPDX and CycloneDX are both JSON; the cyclonedx-xml variant returns XML.
    parse_as_json = format in {"spdx", "cyclonedx", "json"}
    parsed = _maybe_parse_json(result.stdout, "json") if parse_as_json else result.stdout
    return {"format": format, "result": parsed, "raw": result.to_dict()}
