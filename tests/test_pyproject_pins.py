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


def test_the_declared_docker_floor_supports_the_kwarg_the_code_passes():
    """
    `_build_default_client` passes `use_context=False` to `docker.from_env()` so docker-py never
    resolves a Docker CLI context behind our back — `_hosts.py` does that itself and pins the answer
    at startup. That kwarg arrived in docker-py 7.2.0; on 7.1.0 `from_env` does not pop it and it
    reaches `kwargs_from_env` as an unexpected argument, so an install below the floor raises
    TypeError on the *first* client build rather than at import — invisible to a smoke test that only
    imports the package.

    Checked by introspection rather than by comparing version strings: this stays true if docker-py
    renames the release that carries the kwarg, and fails if a future version drops it (at which
    point the call site needs revisiting, not the floor). Mirrors the mcp guard above — assert what
    the code actually depends on, not a number someone has to remember to update.
    """
    import inspect

    import docker

    source = (Path(inspect.getfile(docker.DockerClient)).parent / "client.py").read_text(encoding="utf-8")
    assert "use_context" in source, (
        "the installed docker-py's from_env no longer mentions `use_context`, which "
        "system.py:_build_default_client passes — revisit that call site and the declared floor"
    )

    dependencies = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    declared = [d for d in dependencies if Requirement(d).name == "docker"]
    assert declared, "no direct 'docker' dependency found in pyproject.toml"
    assert not _admits(declared[0], "7.1.0"), (
        f"{declared[0]!r} admits docker-py 7.1.0, which does not accept `use_context` — a fresh "
        "resolve could install it and every client build would raise TypeError"
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
