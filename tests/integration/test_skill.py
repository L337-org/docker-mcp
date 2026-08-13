"""Daemon-backed checks that the `skills/l337-docker` skill's documented snippets actually behave.

`tests/test_skill.py` covers structure and the regression greps without a daemon. This file does the
part that only a real daemon can answer: it **extracts the shell snippets out of the skill's own
markdown and executes them**, so a snippet cannot drift from what the CLI does without failing here.
That distinction matters - a copy of `wait_healthy` pasted into a test would keep passing after the
documented version broke, which is exactly the failure mode being guarded against.

Also pins the CLI behaviours the skill's prose asserts and which were each found the hard way:
`--format json` output shapes, `ls` sizes being display strings, `--until now` being rejected,
label-key case sensitivity, and the volume archive being rooted at its last path component.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.integration.conftest import fail_unless_environmental

_SKILL_DIR = Path(__file__).resolve().parent.parent.parent / "skills" / "l337-docker"
_OBSERVABILITY_MD = _SKILL_DIR / "reference" / "observability.md"

# Everything this module creates carries this label, so teardown can find it even after a failure
# part-way through a test. Deliberately not the skill's own provenance label: a test must not be
# indistinguishable from real skill output on a developer's daemon.
_TEST_LABEL = "l337-docker-skill-test.owner=pytest"
_IMAGE = "alpine:3.20"


def _run(argv: list[str], timeout: float = 120, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Run a command with no shell, returning the completed process regardless of exit code."""
    return subprocess.run(  # noqa: S603
        argv, capture_output=True, input=stdin, timeout=timeout, check=False
    )


def _run_shell(script: str, shell_bin: str = "bash", timeout: float = 180) -> subprocess.CompletedProcess:
    """Run a snippet under an explicit shell, so shell-specific breakage is attributable."""
    return subprocess.run(  # noqa: S603
        [shell_bin, "-c", script], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker(*args: str, timeout: float = 120, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return _run(["docker", *args], timeout=timeout, stdin=stdin)


def _extract_shell_function(path: Path, name: str) -> str:
    """The text of a `name()` shell function as it appears in a fenced block of `path`.

    Fails loudly rather than skipping if it is missing: a renamed or deleted helper means the skill
    no longer documents the behaviour these tests claim to cover.
    """
    for block in re.findall(r"```bash\n(.*?)```", path.read_text(encoding="utf-8"), re.S):
        if block.lstrip().startswith(f"{name}()"):
            return block
    raise AssertionError(f"{name}() is no longer a bash snippet in {path.name}")


@pytest.fixture(scope="module")
def image() -> str:
    """Make sure the helper image is present; the first pull can be slow on a cold CI runner."""
    result = _docker("image", "inspect", _IMAGE, timeout=30)
    if result.returncode != 0:
        pulled = _docker("pull", _IMAGE, timeout=300)
        fail_unless_environmental(
            returncode=pulled.returncode,
            stderr=pulled.stderr.decode(errors="replace"),
            stdout=pulled.stdout.decode(errors="replace"),
            what=f"pulling {_IMAGE}",
        )
    return _IMAGE


@pytest.fixture
def cleanup():
    """Remove every container and volume this test created, however the test ended."""
    names: dict[str, list[str]] = {"container": [], "volume": []}
    yield names
    for name in names["container"]:
        _docker("rm", "-f", name, timeout=60)
    for name in names["volume"]:
        _docker("volume", "rm", "-f", name, timeout=60)


@pytest.fixture(scope="module")
def wait_healthy_snippet() -> str:
    return _extract_shell_function(_OBSERVABILITY_MD, "wait_healthy")


# --------------------------------------------------------------------------------------------
# The documented wait_healthy loop, executed as written
# --------------------------------------------------------------------------------------------


def _start(cleanup, name: str, *extra: str) -> None:
    args = ["run", "-d", "--name", name, "--label", _TEST_LABEL, *extra, _IMAGE, "sleep", "300"]
    result = _docker(*args)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    cleanup["container"].append(name)


@pytest.mark.usefixtures("image")
def test_wait_healthy_reports_running_without_a_healthcheck_as_success(cleanup, wait_healthy_snippet):
    """The ambiguous case: no HEALTHCHECK is not a failure, but it must not be reported as healthy."""
    _start(cleanup, "sktest-nohc")
    result = _run_shell(f"{wait_healthy_snippet}\nwait_healthy sktest-nohc 20")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no healthcheck" in result.stdout
    # The distinction the skill insists on: it must not claim health it never observed.
    assert "OK: healthy" not in result.stdout


@pytest.mark.usefixtures("image")
def test_wait_healthy_detects_a_passing_healthcheck(cleanup, wait_healthy_snippet):
    _start(cleanup, "sktest-hc", "--health-cmd", "true", "--health-interval", "2s", "--health-retries", "2")
    result = _run_shell(f"{wait_healthy_snippet}\nwait_healthy sktest-hc 60")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: healthy" in result.stdout


@pytest.mark.usefixtures("image")
def test_wait_healthy_fails_fast_on_a_failing_healthcheck(cleanup, wait_healthy_snippet):
    _start(cleanup, "sktest-bad", "--health-cmd", "false", "--health-interval", "2s", "--health-retries", "1")
    result = _run_shell(f"{wait_healthy_snippet}\nwait_healthy sktest-bad 60")
    assert result.returncode == 1
    assert "FAIL: unhealthy" in result.stdout


@pytest.mark.usefixtures("image")
def test_wait_healthy_surfaces_the_exit_code_of_a_container_that_died(cleanup):
    """A crashed container must be reported as exited with its code, never as a timeout."""
    result = _docker("run", "-d", "--name", "sktest-exit", "--label", _TEST_LABEL, _IMAGE, "sh", "-c", "exit 3")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    cleanup["container"].append("sktest-exit")
    # Block until it has actually exited, so the assertion is not racing the container's startup.
    _docker("wait", "sktest-exit", timeout=60)

    snippet = _extract_shell_function(_OBSERVABILITY_MD, "wait_healthy")
    ran = _run_shell(f"{snippet}\nwait_healthy sktest-exit 20")
    assert ran.returncode == 1
    assert "exited(3)" in ran.stdout


def test_wait_healthy_distinguishes_a_missing_container_from_a_timeout(wait_healthy_snippet):
    result = _run_shell(f"{wait_healthy_snippet}\nwait_healthy sktest-does-not-exist 10")
    assert result.returncode == 1
    assert "no such container" in result.stdout
    assert "TIMEOUT" not in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed on this runner")
@pytest.mark.usefixtures("image")
def test_wait_healthy_also_runs_under_zsh(cleanup, wait_healthy_snippet):
    """zsh makes `status` read-only, so `local status=...` aborts the function outright.

    That bug shipped in a draft of this skill and was invisible under bash. macOS defaults to zsh,
    so the snippet has to work there; this is the executable half of the static name check in
    `tests/test_skill.py`.
    """
    _start(cleanup, "sktest-zsh")
    result = _run_shell(f"{wait_healthy_snippet}\nwait_healthy sktest-zsh 20", shell_bin="zsh")
    assert "read-only variable" not in result.stderr, "snippet uses a variable name reserved by zsh"
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------------------------
# Output shapes the skill's parsing guidance depends on
# --------------------------------------------------------------------------------------------


def test_ls_format_json_is_ndjson_while_inspect_is_an_array(image):
    """`jq -s` is required for one and wrong for the other; the skill documents both."""
    listed = _docker("image", "ls", "--format", "json")
    assert listed.returncode == 0
    lines = [line for line in listed.stdout.decode().splitlines() if line.strip()]
    assert lines, "no images to inspect"
    for line in lines:
        assert json.loads(line), "each line of ls --format json must be a standalone object"
    assert not listed.stdout.decode().lstrip().startswith("["), "ls --format json must not be an array"

    inspected = _docker("image", "inspect", image)
    assert inspected.returncode == 0
    assert isinstance(json.loads(inspected.stdout), list), "inspect must return an array"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_ls_is_an_array_unlike_the_rest_of_the_cli():
    """The Compose plugin disagrees with the core CLI *and* with `compose ps`."""
    if _docker("compose", "version", timeout=30).returncode != 0:
        pytest.skip("compose plugin not installed")
    result = _docker("compose", "ls", "-a", "--format", "json", timeout=60)
    assert result.returncode == 0
    assert isinstance(json.loads(result.stdout), list), "compose ls --format json must be a JSON array"


def test_ls_sizes_are_display_strings_but_inspect_gives_bytes(image):
    """The skill tells the agent never to do arithmetic on `ls` sizes. This is why."""
    listed = _docker("image", "ls", "--format", "json")
    rows = [json.loads(line) for line in listed.stdout.decode().splitlines() if line.strip()]
    assert rows
    assert all(isinstance(row["Size"], str) for row in rows), "ls sizes must be strings"
    assert any(not row["Size"].isdigit() for row in rows), "ls sizes are humanised, not raw bytes"

    inspected = _docker("image", "inspect", image, "--format", "{{.Size}}")
    assert inspected.stdout.decode().strip().isdigit(), "inspect .Size must be raw bytes"


# --------------------------------------------------------------------------------------------
# CLI behaviours the skill's prose asserts
# --------------------------------------------------------------------------------------------


def test_docker_events_rejects_until_now_but_accepts_a_relative_duration():
    """`--until now` reads naturally and is invalid; the skill says so in four places."""
    rejected = _docker("events", "--since", "1m", "--until", "now", timeout=60)
    assert rejected.returncode != 0, "--until now unexpectedly succeeded; the skill's warning is stale"
    assert b"parse" in rejected.stderr.lower() or b"invalid" in rejected.stderr.lower()

    accepted = _docker("events", "--since", "1m", "--until", "0s", timeout=60)
    assert accepted.returncode == 0, accepted.stderr.decode(errors="replace")


@pytest.mark.usefixtures("image")
def test_label_keys_are_case_sensitive(cleanup):
    """Why the provenance label is lowercase: a mixed-case key silently matches nothing in a
    lowercase filter, so a teardown would report a clean daemon while resources remained."""
    _start(cleanup, "sktest-case", "--label", "L337-MixedCase.managed=true")
    exact = _docker("ps", "--filter", "label=L337-MixedCase.managed=true", "--format", "{{.Names}}")
    lowered = _docker("ps", "--filter", "label=l337-mixedcase.managed=true", "--format", "{{.Names}}")
    assert b"sktest-case" in exact.stdout
    assert b"sktest-case" not in lowered.stdout, "label filters are case-insensitive; the skill's rationale is stale"


# --------------------------------------------------------------------------------------------
# The volume backup/restore procedure from workflows/maintenance.md
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("image")
def test_volume_backup_and_restore_round_trips(cleanup, tmp_path):
    """Exercises the documented procedure: copy out of a never-started helper, restore at `/`."""
    src, dst = "sktest-vol-src", "sktest-vol-dst"
    for vol in (src, dst):
        assert _docker("volume", "create", vol).returncode == 0
        cleanup["volume"].append(vol)

    seeded = _docker(
        "run",
        "--rm",
        "-v",
        f"{src}:/data",
        "--label",
        _TEST_LABEL,
        _IMAGE,
        "sh",
        "-c",
        "echo hello > /data/a.txt; mkdir -p /data/sub; echo world > /data/sub/b.txt",
    )
    assert seeded.returncode == 0, seeded.stderr.decode(errors="replace")

    # Backup: the helper is created but never started, which is what makes no `tar` binary necessary.
    assert (
        _docker("create", "--name", "sktest-vbackup", "-v", f"{src}:/data", "--label", _TEST_LABEL, _IMAGE).returncode
        == 0
    )
    cleanup["container"].append("sktest-vbackup")
    archive = tmp_path / "backup.tar"
    copied = _docker("cp", "sktest-vbackup:/data", "-")
    assert copied.returncode == 0, copied.stderr.decode(errors="replace")
    archive.write_bytes(copied.stdout)

    # The archive is rooted at the last path component. Restore depends on it, so assert it.
    listed = _run(["tar", "-tf", str(archive)])
    assert listed.returncode == 0
    assert any(line.startswith("data/") for line in listed.stdout.decode().splitlines())

    # Restore into a different volume by extracting at `/`, not `/data`.
    assert (
        _docker("create", "--name", "sktest-vrestore", "-v", f"{dst}:/data", "--label", _TEST_LABEL, _IMAGE).returncode
        == 0
    )
    cleanup["container"].append("sktest-vrestore")
    restored = _docker("cp", "-", "sktest-vrestore:/", stdin=archive.read_bytes())
    assert restored.returncode == 0, restored.stderr.decode(errors="replace")

    check = _docker(
        "run",
        "--rm",
        "-v",
        f"{dst}:/data",
        "--label",
        _TEST_LABEL,
        _IMAGE,
        "sh",
        "-c",
        "cat /data/a.txt /data/sub/b.txt",
    )
    assert check.returncode == 0, check.stderr.decode(errors="replace")
    assert check.stdout.decode().split() == ["hello", "world"]


@pytest.mark.usefixtures("image")
def test_restoring_at_the_data_path_nests_the_archive(cleanup, tmp_path):
    """The failure mode the workflow warns about, pinned so the warning cannot go stale."""
    vol = "sktest-vol-wrong"
    assert _docker("volume", "create", vol).returncode == 0
    cleanup["volume"].append(vol)
    seeded = _docker(
        "run", "--rm", "-v", f"{vol}:/data", "--label", _TEST_LABEL, _IMAGE, "sh", "-c", "echo hello > /data/a.txt"
    )
    assert seeded.returncode == 0

    assert (
        _docker("create", "--name", "sktest-wsrc", "-v", f"{vol}:/data", "--label", _TEST_LABEL, _IMAGE).returncode == 0
    )
    cleanup["container"].append("sktest-wsrc")
    archive = tmp_path / "b.tar"
    archive.write_bytes(_docker("cp", "sktest-wsrc:/data", "-").stdout)

    target = "sktest-vol-wrong2"
    assert _docker("volume", "create", target).returncode == 0
    cleanup["volume"].append(target)
    assert (
        _docker("create", "--name", "sktest-wdst", "-v", f"{target}:/data", "--label", _TEST_LABEL, _IMAGE).returncode
        == 0
    )
    cleanup["container"].append("sktest-wdst")

    # Extracting at /data instead of / - the documented mistake.
    assert _docker("cp", "-", "sktest-wdst:/data", stdin=archive.read_bytes()).returncode == 0
    nested = _docker(
        "run",
        "--rm",
        "-v",
        f"{target}:/data",
        "--label",
        _TEST_LABEL,
        _IMAGE,
        "sh",
        "-c",
        "test -f /data/data/a.txt && echo nested",
    )
    assert b"nested" in nested.stdout, "extracting at /data no longer nests; the workflow's warning is stale"
