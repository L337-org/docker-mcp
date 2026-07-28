import json
import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = _ROOT / "pyproject.toml"
MANIFEST = _ROOT / "manifest.json"
UV_LOCK = _ROOT / "uv.lock"


def _version_tuple(version: str, length: int = 4) -> tuple[int, ...]:
    parts = [int(p) for p in version.split(".")]
    parts += [0] * (length - len(parts))
    return tuple(parts[:length])


def _dependency_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, f"could not parse a dependency name from {requirement!r}"
    return match.group(0)


def test_intel_macos_cryptography_pin_not_relaxed():
    """
    Dependabot doesn't get an automatic Copilot review (bots aren't billable for
    premium requests), so a bump here must be caught by a hard, deterministic
    CI failure instead of relying on review. See the pyproject.toml comment:
    cryptography>=49 dropped the macosx_10_9_universal2 wheel, breaking `uvx`
    installs on Intel macOS.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    deps = data["project"]["dependencies"]

    cryptography_deps = [d for d in deps if _dependency_name(d) == "cryptography"]
    assert cryptography_deps, "no direct 'cryptography' dependency found in pyproject.toml"

    intel_macos_deps = [
        d for d in cryptography_deps if "platform_system == 'Darwin'" in d and "platform_machine == 'x86_64'" in d
    ]
    assert intel_macos_deps, (
        "the Intel-macOS cryptography pin is missing: no 'cryptography' dependency is scoped to "
        f"platform_system == 'Darwin' and platform_machine == 'x86_64'; found: {cryptography_deps!r}"
    )
    assert len(intel_macos_deps) == 1, f"expected exactly one Intel-macOS cryptography pin, found: {intel_macos_deps!r}"

    dep = intel_macos_deps[0]
    m = re.search(r"cryptography\s*<\s*([0-9]+(?:\.[0-9]+)*)", dep)
    assert m, f"cryptography pin must use a '<N[.N...]' upper bound, got: {dep!r}"

    bound = _version_tuple(m.group(1))
    max_allowed = _version_tuple("49")
    assert bound <= max_allowed, (
        f"cryptography upper bound raised to {m.group(1)} — 49.x has no x86_64 macOS wheel; "
        "see the pyproject.toml comment before lifting this cap"
    )


def test_mcp_major_cap_not_relaxed():
    """
    mcp 2.0.0 removed `mcp.server.fastmcp`, which `server.py` imports FastMCP from, so an
    uncapped `mcp` resolves to a release that cannot import this package. That is not
    hypothetical: the published 2.2.0 shipped uncapped and `uvx docker-mcp-server` failed at
    import on a clean machine, while CI stayed green because it installs `--locked` against a
    lockfile pinning 1.x. Lifting this cap needs the `mcp.server.mcpserver` migration first.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    deps = data["project"]["dependencies"]

    mcp_deps = [d for d in deps if _dependency_name(d) == "mcp"]
    assert mcp_deps, "no direct 'mcp' dependency found in pyproject.toml"
    assert len(mcp_deps) == 1, f"expected exactly one 'mcp' dependency, found: {mcp_deps!r}"

    dep = mcp_deps[0]
    m = re.search(r"mcp[^,]*,\s*<\s*([0-9]+(?:\.[0-9]+)*)", dep)
    assert m, (
        f"the mcp dependency must carry a '<N' upper bound, got: {dep!r} — an uncapped mcp "
        "resolves to 2.x, which has no `mcp.server.fastmcp`"
    )

    bound = _version_tuple(m.group(1))
    max_allowed = _version_tuple("2")
    assert bound <= max_allowed, (
        f"mcp upper bound raised to {m.group(1)} — 2.x moved FastMCP to `mcp.server.mcpserver`; "
        "port `server.py` before lifting this cap"
    )


def test_the_declared_mcp_bound_matches_what_the_code_imports():
    """
    The cap and the import have to agree, and only one of them is in pyproject: assert the
    module we import FastMCP from is the one the *locked* mcp actually provides. Catches a
    lockfile that drifts past the cap as well as a cap raised without porting the import.
    """
    import importlib.util

    from docker_mcp import server as server_module

    source = Path(server_module.__file__).read_text(encoding="utf-8")
    match = re.search(r"from (mcp[\w.]*) import FastMCP", source)
    assert match, "server.py no longer imports FastMCP from an `mcp.*` module — update this guard"
    assert importlib.util.find_spec(match.group(1)) is not None, (
        f"server.py imports FastMCP from {match.group(1)!r}, which the installed mcp does not "
        "provide — the pyproject cap and the code disagree"
    )


# The release pipeline's preflight job re-asserts these against the release tag; the tests
# below catch the drift earlier, at PR time. server.json is intentionally NOT checked — its
# committed version is stale by design and stamped from the tag at release time.


def test_manifest_version_matches_pyproject():
    """
    manifest.json (the MCPB bundle manifest) is documented as kept in step with
    pyproject.toml; the publish workflow restamps it from the tag, but drift in the repo
    still confuses local bundle builds (scripts/build-mcpb.sh only warns).
    """
    pyproject_version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    manifest_version = json.loads(MANIFEST.read_text())["version"]
    assert manifest_version == pyproject_version, (
        f"manifest.json version {manifest_version!r} != pyproject.toml version {pyproject_version!r} — "
        "bump them together"
    )


def test_uv_lock_self_version_matches_pyproject():
    """
    Catches "bumped pyproject.toml, forgot `uv lock`": the lockfile embeds this package's
    own version, and a stale entry ships a lockfile that disagrees with the metadata.
    """
    pyproject_version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    packages = tomllib.loads(UV_LOCK.read_text())["package"]
    self_entries = [
        p for p in packages if p["name"] == "docker-mcp-server" and p.get("source", {}).get("editable") == "."
    ]
    assert len(self_entries) == 1, f"expected exactly one editable self-entry in uv.lock, found {len(self_entries)}"
    lock_version = self_entries[0]["version"]
    assert lock_version == pyproject_version, (
        f"uv.lock self-entry version {lock_version!r} != pyproject.toml version {pyproject_version!r} — "
        "run `uv lock` after bumping the version"
    )
