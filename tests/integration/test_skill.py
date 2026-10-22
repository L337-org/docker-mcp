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
import time
from pathlib import Path

import pytest

from tests.integration.conftest import fail_unless_environmental

_SKILL_DIR = Path(__file__).resolve().parent.parent.parent / "skills" / "l337-docker"
_OBSERVABILITY_MD = _SKILL_DIR / "reference" / "observability.md"
_REGISTRY_MD = _SKILL_DIR / "reference" / "registry.md"

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
    """Remove every service, container and volume this test created, however the test ended."""
    names: dict[str, list[str]] = {"service": [], "container": [], "volume": [], "network": []}
    yield names
    for name in names["service"]:
        _docker("service", "rm", name, timeout=60)
    for name in names["container"]:
        _docker("rm", "-f", name, timeout=60)
    for name in names["volume"]:
        _docker("volume", "rm", "-f", name, timeout=60)
    for name in names["network"]:
        # A service's tasks leave its overlay network a moment after the service goes.
        for _ in range(10):
            if _docker("network", "rm", name, timeout=60).returncode == 0:
                break
            time.sleep(1)


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
def test_wait_healthy_reports_running_without_a_healthcheck_as_not_met(cleanup, wait_healthy_snippet):
    """No HEALTHCHECK means health can never be observed, so the wait is not met - promptly, not by timeout.

    The server's `system_wait(until="healthy")` answers the same way, and the two are kept in step.
    """
    _start(cleanup, "sktest-nohc")
    result = _run_shell(f"{wait_healthy_snippet}\nwait_healthy sktest-nohc 20")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT MET" in result.stdout
    assert "no healthcheck" in result.stdout
    assert "TIMEOUT" not in result.stdout


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
    assert "NOT MET" in result.stdout, result.stdout + result.stderr


# --------------------------------------------------------------------------------------------
# The documented wait_log loop and bounded follow
# --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("image")
def test_wait_log_finds_a_line_logged_after_the_wait_starts(cleanup):
    result = _docker(
        "run", "-d", "--name", "sktest-log", "--label", _TEST_LABEL, _IMAGE,
        "sh", "-c", "sleep 3; echo 'listening on :80'; sleep 300",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    cleanup["container"].append("sktest-log")
    snippet = _extract_shell_function(_OBSERVABILITY_MD, "wait_log")
    ran = _run_shell(f"{snippet}\nwait_log sktest-log 'listening on' 30")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "OK:" in ran.stdout
    assert "listening on :80" in ran.stdout


@pytest.mark.usefixtures("image")
def test_wait_log_stops_when_the_container_exits_without_the_line(cleanup):
    """A container that has exited will log nothing more, so waiting out the timeout would be wasted."""
    result = _docker("run", "-d", "--name", "sktest-logexit", "--label", _TEST_LABEL, _IMAGE, "sh", "-c", "echo nope")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    cleanup["container"].append("sktest-logexit")
    _docker("wait", "sktest-logexit", timeout=60)
    snippet = _extract_shell_function(_OBSERVABILITY_MD, "wait_log")
    ran = _run_shell(f"{snippet}\nwait_log sktest-logexit 'listening' 60")
    assert ran.returncode == 1
    assert "NOT MET" in ran.stdout
    assert "TIMEOUT" not in ran.stdout


@pytest.mark.usefixtures("image")
def test_follow_logs_returns_on_time_from_a_container_that_logs_nothing(cleanup):
    """The case a future `--until` gets wrong: on a quiet container `docker logs --follow --until` never returns."""
    _start(cleanup, "sktest-follow")
    snippet = _extract_shell_function(_OBSERVABILITY_MD, "follow_logs")
    ran = _run_shell(f"{snippet}\nfollow_logs sktest-follow 3", timeout=30)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "--- followed 3s" in ran.stdout


@pytest.mark.usefixtures("image")
def test_follow_logs_shows_only_what_is_logged_during_the_window(cleanup):
    """ "What does it log over the next N seconds" must not be answered with lines from before the call."""
    result = _docker(
        "run", "-d", "--name", "sktest-followold", "--label", _TEST_LABEL, _IMAGE,
        "sh", "-c", "echo before-the-window; sleep 6; echo during-the-window; sleep 300",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    cleanup["container"].append("sktest-followold")
    assert _run_shell("sleep 2").returncode == 0  # let the first line be logged before the window opens
    snippet = _extract_shell_function(_OBSERVABILITY_MD, "follow_logs")
    ran = _run_shell(f"{snippet}\nfollow_logs sktest-followold 10", timeout=40)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "during-the-window" in ran.stdout
    assert "before-the-window" not in ran.stdout


# --------------------------------------------------------------------------------------------
# The documented swarm waits, against a real swarm
# --------------------------------------------------------------------------------------------


def _wait_service_snippet() -> str:
    return _extract_shell_function(_OBSERVABILITY_MD, "wait_service")


def _create_service(cleanup, name: str, *extra: str) -> None:
    # --init because `sleep` as PID 1 ignores SIGTERM, so without an init every update, rollback and
    # removal waits out the stop grace period before the task is killed.
    args = ["service", "create", "-d", "--init", "--name", name, "--label", _TEST_LABEL, *extra, _IMAGE]
    result = _docker(*args, "sleep", "3600")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    cleanup["service"].append(name)


@pytest.mark.usefixtures("image", "skip_if_no_swarm")
def test_wait_service_reports_a_converged_replicated_service(cleanup):
    _create_service(cleanup, "sktest-svc", "--replicas", "2")
    ran = _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-svc 90")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "OK: 2 running (replicated)" in ran.stdout


@pytest.mark.usefixtures("image", "skip_if_no_swarm")
def test_wait_service_does_not_report_a_rolling_update_as_converged(cleanup):
    """Straight after `update -d` the old tasks still run on the old spec, and must not count as converged.

    The update adds a field the old tasks' specs lack entirely, the case a comparison that ignored
    fields missing from the task would get wrong. A 60 s delay between tasks keeps the rollout
    unfinished for the whole wait, whether or not `UpdateStatus` has been set yet.
    """
    _create_service(cleanup, "sktest-roll", "--replicas", "2")
    assert _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-roll 90").returncode == 0
    updated = _docker("service", "update", "-d", "--env-add", "X=1", "--update-delay", "60s", "sktest-roll")
    assert updated.returncode == 0, updated.stderr.decode(errors="replace")
    ran = _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-roll 10")
    assert ran.returncode == 1, ran.stdout + ran.stderr
    assert "OK:" not in ran.stdout
    assert "TIMEOUT" in ran.stdout


@pytest.mark.usefixtures("image", "skip_if_no_swarm")
def test_wait_service_matches_tasks_of_a_service_with_many_options_set(cleanup):
    """`service inspect` fills swarm's defaults into the template; a task's spec lacks them.

    A partial restart policy, a stop grace period at its default, limits, a constraint and a
    healthcheck each make the two documents differ; none may stop the tasks counting as current.
    """
    network = _docker("network", "create", "-d", "overlay", "--label", _TEST_LABEL, "sktest-optsnet")
    assert network.returncode == 0, network.stderr.decode(errors="replace")
    cleanup["network"].append("sktest-optsnet")
    _create_service(
        cleanup, "sktest-opts", "--replicas", "1", "--restart-condition", "on-failure",
        "--stop-grace-period", "10s", "--limit-memory", "64m", "--constraint", "node.role==manager",
        "--health-cmd", "true", "--env", "A=1", "--mount", "type=tmpfs,target=/t",
        "--network", "sktest-optsnet", "--log-driver", "json-file", "--log-opt", "max-size=1m",
    )  # fmt: skip
    ran = _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-opts 90")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "OK: 1 running (replicated)" in ran.stdout


@pytest.mark.usefixtures("image", "skip_if_no_swarm")
def test_wait_service_fails_promptly_on_a_paused_update(cleanup):
    _create_service(cleanup, "sktest-pause", "--replicas", "1")
    assert _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-pause 90").returncode == 0
    broken = _docker(
        "service", "update", "-d", "--args", "sh -c 'exit 1'",
        "--update-failure-action", "pause", "--update-monitor", "3s", "sktest-pause",
    )  # fmt: skip
    assert broken.returncode == 0, broken.stderr.decode(errors="replace")
    ran = _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-pause 100")
    assert ran.returncode == 1, ran.stdout + ran.stderr
    assert "FAIL: update paused" in ran.stdout


@pytest.mark.usefixtures("image", "skip_if_no_swarm")
def test_wait_service_reports_a_rollback_distinctly(cleanup):
    """`rollback_completed` is neither success nor failure for a deploy, so it has its own outcome."""
    _create_service(cleanup, "sktest-rb", "--replicas", "1")
    assert _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-rb 90").returncode == 0
    assert _docker("service", "update", "-d", "--env-add", "X=1", "sktest-rb").returncode == 0
    assert _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-rb 90").returncode == 0
    assert _docker("service", "rollback", "-d", "sktest-rb").returncode == 0
    ran = _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-rb 90")
    assert ran.returncode == 2, ran.stdout + ran.stderr
    assert "ROLLED BACK" in ran.stdout


@pytest.mark.usefixtures("image", "skip_if_no_swarm")
def test_wait_service_waits_for_a_global_service_to_have_a_task(cleanup):
    _create_service(cleanup, "sktest-glob", "--mode", "global")
    ran = _run_shell(f"{_wait_service_snippet()}\nwait_service sktest-glob 90")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "(global)" in ran.stdout


@pytest.mark.usefixtures("skip_if_no_swarm")
def test_wait_node_takes_the_target_state_and_an_optional_availability():
    snippet = _extract_shell_function(_OBSERVABILITY_MD, "wait_node")
    node = _docker("node", "inspect", "self", "--format", "{{.ID}}").stdout.decode().strip()
    ready = _run_shell(f"{snippet}\nwait_node {node} ready 20 active")
    assert ready.returncode == 0, ready.stdout + ready.stderr
    down = _run_shell(f"{snippet}\nwait_node {node} down 6")
    assert down.returncode == 1
    assert "TIMEOUT: state=ready" in down.stdout


# --------------------------------------------------------------------------------------------
# The documented registry recipe, against Docker Hub
# --------------------------------------------------------------------------------------------


def _hub_rate_limit_report() -> str:
    """Docker Hub's rate-limit headers for this address, read with a HEAD, which Docker says costs no pull."""
    probe = _run_shell(
        "t=$(curl -s 'https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository:ratelimitpreview/test:pull'"
        ' | jq -r .token); curl -sI -H "Authorization: Bearer $t"'
        " https://registry-1.docker.io/v2/ratelimitpreview/test/manifests/latest",
        timeout=60,
    )
    return probe.stdout + probe.stderr


def _skip_if_hub_is_the_cause(output: str) -> None:
    """Skip, naming the cause, when a Hub recipe failed because Hub refused or could not be reached."""
    if "FAIL: no token" in output:
        pytest.skip(f"Docker Hub's token service is unreachable from here: {output.strip()}")
    headers = _hub_rate_limit_report()
    fail_unless_environmental(returncode=1, stdout=output, stderr=headers, what="image_config against Docker Hub")


def test_image_config_follows_an_index_and_fails_cleanly_on_a_missing_tag():
    """A tag naming an index needs one more hop; a missing tag must stop with FAIL, not an empty digest."""
    snippet = _extract_shell_function(_REGISTRY_MD, "image_config")
    found = _run_shell(f"{snippet}\nimage_config library/alpine 3.20", timeout=120)
    if found.returncode != 0:
        _skip_if_hub_is_the_cause(found.stdout + found.stderr)
    assert '"cmd"' in found.stdout
    missing = _run_shell(f"{snippet}\nimage_config library/alpine no-such-tag-l337", timeout=120)
    assert missing.returncode == 1
    assert "FAIL: no manifest" in missing.stdout


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
