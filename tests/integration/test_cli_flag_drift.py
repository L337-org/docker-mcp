"""Check that every CLI flag the tool modules pass still exists in the installed docker CLI.

This exists because three Scout tools shipped passing a `--format` flag that `docker scout
quickview` and `docker scout recommendations` do not define, and a fourth defaulted to a `--format`
value `docker scout cves` rejects. Nothing noticed: the unit tests mock the subprocess, so they
assert only that we pass what we think we pass, and the integration tests skipped on any non-zero
exit, so the failure looked like being offline.

A mocked test can never catch this, because the thing that changed is outside the mock. This walks
the source for the argv each CLI-backed tool builds, then asks the installed CLI whether those
flags exist. It is deliberately a *drift* check against whatever is installed rather than a pin to
a known-good version, since the whole failure mode is upstream changing under us.

Scope and limits, stated because a check that silently verifies nothing is worse than none:
tools whose argv is assembled dynamically (notably `buildx_build`, which drives a staging session)
have no static list to read and are not covered. `_MIN_FUNCTIONS_CHECKED` guards against the
extractor silently matching nothing after a refactor.
"""

import ast
import pathlib
import re
import shutil
import subprocess

import pytest

from docker_mcp.tools._cli import has_plugin
from tests.integration.conftest import fail_unless_environmental

# module -> (docker CLI name, plugin name or None when it is a core CLI command)
_CLI_MODULES = {
    "compose": ("compose", "compose"),
    "stack": ("stack", None),
    "buildx": ("buildx", "buildx"),
    "scout": ("scout", "scout"),
    "context": ("context", None),
}

# Below this, assume the extractor has stopped matching rather than that the surface shrank.
# The count at the time of writing was 28.
_MIN_FUNCTIONS_CHECKED = 20

_SRC = pathlib.Path(__file__).resolve().parent.parent.parent / "docker_mcp" / "tools"


def _leading_subcommand(fn: ast.FunctionDef, cli: str) -> list[str] | None:
    """The literal subcommand path a function builds, e.g. ["imagetools", "inspect"]."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        lead: list[str] = []
        for element in node.elts:
            if not isinstance(element, ast.Constant):
                break
            value = element.value
            if not isinstance(value, str) or value.startswith("-"):
                break
            lead.append(value)
        if lead and all(" " not in part for part in lead) and len(lead) <= 3:
            # The module's own `_run_*` wrapper prepends the CLI name, so drop it if repeated.
            while lead and lead[0] == cli:
                lead = lead[1:]
            return lead or None
    return None


def _run_help(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `docker <argv> --help`, skipping cleanly rather than crashing when it cannot run.

    The return code is reported rather than inferred: deciding success from "did it print
    anything" would treat an error message as help text, and then report every flag as missing.
    An explicit timeout matches the project's own rule that every CLI call carries one, and stops a
    wedged CLI stalling the whole integration suite.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no docker binary on PATH")
    try:
        return subprocess.run(  # noqa: S603
            [docker, *argv, "--help"], capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"`docker {' '.join(argv)} --help` timed out after 30s")


def _collect() -> list[tuple[str, str, list[str], list[str]]]:
    """(qualified name, cli, subcommand path, flags) for every statically-readable tool."""
    found = []
    for module, (cli, _) in _CLI_MODULES.items():
        source = (_SRC / f"{module}.py").read_text()
        tree = ast.parse(source)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            segment = ast.get_source_segment(source, fn) or ""
            subs = _leading_subcommand(fn, cli)
            if not subs:
                continue
            flags = sorted(set(re.findall(r'"(--[a-z][a-z0-9-]*)"', segment)))
            if flags:
                found.append((f"{module}.{fn.name}", cli, subs, flags))
    return found


def test_the_extractor_still_finds_the_cli_tools():
    """Guards the checks below: if this drops, they are silently verifying nothing."""
    collected = _collect()
    assert len(collected) >= _MIN_FUNCTIONS_CHECKED, (
        f"only {len(collected)} CLI-backed functions were readable, expected at least "
        f"{_MIN_FUNCTIONS_CHECKED}. The argv extractor has probably stopped matching after a "
        f"refactor, which would make the flag-drift check pass without checking anything."
    )


@pytest.mark.parametrize(
    ("qualname", "cli", "subs", "flags"), _collect(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_flag_passed_still_exists_in_the_installed_cli(qualname, cli, subs, flags):
    module = qualname.split(".", 1)[0]
    plugin = _CLI_MODULES[module][1]
    if plugin is not None and not has_plugin(plugin):
        pytest.skip(f"docker {plugin} plugin not installed on this host")

    completed = _run_help([cli, *subs])
    fail_unless_environmental(
        returncode=completed.returncode,
        stderr=completed.stderr,
        stdout=completed.stdout,
        what=f"docker {cli} {' '.join(subs)} --help",
    )
    help_text = completed.stdout + completed.stderr
    assert help_text.strip(), (
        f"`docker {cli} {' '.join(subs)} --help` exited 0 but printed nothing, so there is no flag "
        f"list to check against. Treating that as drift would be wrong; something is off with the "
        f"CLI itself."
    )

    missing = [f for f in flags if re.search(rf"(^|\s){re.escape(f)}(\s|=|,|$)", help_text, re.M) is None]
    assert not missing, (
        f"{qualname} passes {missing} to `docker {cli} {' '.join(subs)}`, which the installed CLI "
        f"does not document. Either the flag was renamed or removed upstream, or it never existed. "
        f"A flag that still parses as a hidden alias (as `buildx create --config` did before it "
        f"became `--buildkitd-config`) should be migrated to the documented spelling rather than "
        f"added to an exemption list."
    )
