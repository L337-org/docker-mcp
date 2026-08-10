import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = _ROOT / "pyproject.toml"
MANIFEST = _ROOT / "manifest.json"
UV_LOCK = _ROOT / "uv.lock"


def _dependency_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, f"could not parse a dependency name from {requirement!r}"
    return match.group(0)


def _admits(requirement: str, version: str) -> bool:
    """
    Whether a requirement's specifiers allow `version` to be installed.

    This is the question the caps actually pose, and asking it directly avoids two mistakes a bound
    comparison makes. Ordering: PEP 508 does not fix specifier order, so `mcp<2,>=1.27.1` must read the
    same as `mcp>=1.27.1,<2`. Strictness: `<=2` *admits* 2.0.0 — the very release that cannot import —
    yet compares equal to a `<2` bound, so a guard built on bounds passes it. `packaging` answers the
    real question and handles markers, extras, `!=` and `~=` for free.

    args:
        requirement - a PEP 508 requirement string from pyproject's dependency list
        version - the version to test, normally the first known-bad one
    returns: bool - True if that version satisfies the requirement
    """
    return Requirement(requirement).specifier.contains(version)


def test_cryptography_floor_is_not_capped_below_the_fix():
    """
    Dependabot doesn't get an automatic Copilot review (bots aren't billable for premium
    requests), so a regression here must be caught by a hard, deterministic CI failure instead of
    relying on review. See the pyproject.toml comment: cryptography<50 admits the vulnerable range
    for GHSA (Dependabot alert #16, high severity). We deliberately require >=50 on every
    platform — including Intel macOS, which has had no x86_64/universal2 wheel since 49.0.0 and
    must build from source (Rust + OpenSSL 3.x) — rather than caching everyone on a known-vulnerable
    version to keep one platform wheel-only. Don't reintroduce a platform-scoped cap below 50.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    deps = data["project"]["dependencies"]

    cryptography_deps = [d for d in deps if _dependency_name(d) == "cryptography"]
    assert cryptography_deps, "no direct 'cryptography' dependency found in pyproject.toml"
    assert len(cryptography_deps) == 1, f"expected exactly one 'cryptography' dependency, found: {cryptography_deps!r}"

    dep = cryptography_deps[0]
    assert "platform_system" not in dep and "platform_machine" not in dep, (
        f"the cryptography dependency {dep!r} is scoped to a platform marker — this pin must apply "
        "unconditionally, or Intel macOS would silently stay on a vulnerable version while every "
        "other platform gets the fix."
    )
    assert not _admits(dep, "49.0"), (
        f"the cryptography pin {dep!r} admits 49.0, which is within the vulnerable range fixed by 50.0.0 "
        "(GHSA, Dependabot alert #16). See the pyproject.toml comment before relaxing this floor."
    )


def test_the_declared_mcp_bound_matches_what_the_code_imports():
    """
    `server.py` imports its MCP server class from a specific `mcp.*` submodule; assert that module
    is one the *locked* mcp actually provides. This is the permanent guard for the class of failure
    that bit the published 2.2.0 release: mcp 2.0.0 removed `mcp.server.fastmcp` (which `server.py`
    used to import `FastMCP` from), but CI stayed green because it installs `--locked` against a
    lockfile still pinning 1.x, so a *fresh* resolve broke at import while every test passed. Unlike
    that incident's hotfix (a version cap), this test needs no cap to stay current — it fails
    whenever an installed mcp stops providing the import path the code actually uses, including a
    future major bump, with no reliance on remembering to add a new cap first.
    """
    import importlib.util

    from docker_mcp import server as server_module

    source = Path(server_module.__file__).read_text(encoding="utf-8")
    match = re.search(r"from (mcp[\w.]*) import MCPServer", source)
    assert match, "server.py no longer imports MCPServer from an `mcp.*` module — update this guard"
    assert importlib.util.find_spec(match.group(1)) is not None, (
        f"server.py imports MCPServer from {match.group(1)!r}, which the installed mcp does not provide"
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


@pytest.mark.parametrize(
    ("requirement", "admits_two"),
    [
        # Specifier order is not guaranteed; these three declare the same real cap.
        ("mcp>=1.27.1,<2", False),
        ("mcp<2,>=1.27.1", False),
        ("mcp<2", False),
        # `<=2` reads like a cap and is not one: it admits 2.0.0, the release that cannot import.
        ("mcp<=2", True),
        ("mcp<=2.0.0", True),
        # Uncapped, and a cap above the bad version — both states the guard must reject.
        ("mcp>=1.27.1", True),
        ("mcp>=1.27.1,<3", True),
        # Markers and extras are the parser's problem, not the guard's.
        ("mcp<2; python_version >= '3.14'", False),
        ("mcp[ws]<2", False),
    ],
)
def test_admits_answers_the_question_the_caps_actually_pose(requirement, admits_two):
    """
    A guard is only as good as its predicate. `<=2` is the case a bound comparison gets wrong, and a
    guard that cries wolf on a reordered specifier gets disabled — so both are pinned here.
    """
    assert _admits(requirement, "2.0.0") is admits_two
