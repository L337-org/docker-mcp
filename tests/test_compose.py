from unittest.mock import patch

import pytest

from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.compose import (
    _global_args,
    compose_build,
    compose_config,
    compose_cp,
    compose_down,
    compose_exec,
    compose_images,
    compose_kill,
    compose_logs,
    compose_list,
    compose_pause,
    compose_port,
    compose_ps,
    compose_pull,
    compose_restart,
    compose_run,
    compose_start,
    compose_stop,
    compose_top,
    compose_unpause,
    compose_up,
    compose_wait,
)


@pytest.fixture(autouse=True)
def _stub_plugin_check():  # pyright: ignore[reportUnusedFunction]
    # Every test that calls `_run_compose` ultimately calls `require_plugin("compose")`.
    # We don't want those tests to shell out to a real `docker compose version` probe.
    with patch("docker_mcp.tools.compose.require_plugin"):
        yield


def _ok(stdout: str = "", stderr: str = "") -> CliResult:
    return CliResult(returncode=0, stdout=stdout, stderr=stderr, truncated=False)


def _fail(stderr: str, returncode: int = 1) -> CliResult:
    return CliResult(returncode=returncode, stdout="", stderr=stderr, truncated=False)


# ---------- _global_args ----------


def test_global_args_empty_when_all_none():
    assert _global_args(None, None, None) == []


def test_global_args_multiple_files_and_profiles():
    assert _global_args(["a.yml", "b.yml"], "demo", ["dev", "tools"]) == [
        "-f",
        "a.yml",
        "-f",
        "b.yml",
        "--project-name",
        "demo",
        "--profile",
        "dev",
        "--profile",
        "tools",
    ]


# ---------- compose_up ----------


def test_compose_up_minimal_uses_detach():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_up()
    args = run.call_args.args[0]
    assert args[:2] == ["compose", "up"]
    assert "-d" in args


def test_compose_up_passes_global_flags_and_subcommand_flags():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_up(
            project_dir="/tmp/proj",
            files=["docker-compose.yml", "docker-compose.override.yml"],
            project_name="demo",
            profiles=["dev"],
            services=["web", "db"],
            build=True,
            pull="always",
            remove_orphans=True,
            wait=True,
        )
    args = run.call_args.args[0]
    # Global flags come before the subcommand
    assert args.index("-f") < args.index("up")
    assert args.index("--project-name") < args.index("up")
    assert args.index("--profile") < args.index("up")
    # Subcommand flags
    assert "--build" in args
    assert "--pull" in args and args[args.index("--pull") + 1] == "always"
    assert "--remove-orphans" in args
    assert "--wait" in args
    # Services tacked on at the end
    assert args[-2:] == ["web", "db"]
    # cwd forwarded
    assert run.call_args.kwargs["cwd"] == "/tmp/proj"


# ---------- compose_down ----------


def test_compose_down_includes_volumes_and_orphans_when_set():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_down(volumes=True, remove_orphans=True)
    args = run.call_args.args[0]
    assert "down" in args
    assert "--volumes" in args
    assert "--remove-orphans" in args


# ---------- compose_ps ----------


def test_compose_ps_parses_ndjson():
    body = '{"Name":"web","State":"running"}\n{"Name":"db","State":"running"}\n'
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok(body)):
        result = compose_ps()
    assert result["services"] == [
        {"Name": "web", "State": "running"},
        {"Name": "db", "State": "running"},
    ]
    assert result["raw"]["returncode"] == 0


def test_compose_ps_handles_single_object_response():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok('{"Name":"web"}')):
        result = compose_ps()
    assert result["services"] == [{"Name": "web"}]


def test_compose_ps_returns_empty_services_on_failure():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_fail("no such project")):
        result = compose_ps()
    assert result["services"] == []
    assert result["raw"]["returncode"] == 1
    assert "no such project" in result["raw"]["stderr"]


def test_compose_ps_passes_all_and_services():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("[]")) as run:
        compose_ps(services=["web"], all=True)
    args = run.call_args.args[0]
    assert args[-3:] == ["--all", "web"] or args[-2:] == ["--all", "web"] or "--all" in args


# ---------- compose_logs ----------


def test_compose_logs_default_tail_and_no_color():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("hello")) as run:
        result = compose_logs()
    args = run.call_args.args[0]
    assert "--no-color" in args
    assert "--no-log-prefix" in args
    assert args[args.index("--tail") + 1] == "200"
    assert result["stdout"] == "hello"


def test_compose_logs_tail_all_literal():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_logs(tail="all")
    args = run.call_args.args[0]
    assert args[args.index("--tail") + 1] == "all"


def test_compose_logs_tail_defaults_bounded():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_logs()
    args = run.call_args.args[0]
    assert args[args.index("--tail") + 1] == "200"


def test_compose_logs_with_since_until_timestamps_services():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_logs(since="10m", until="2024-01-01T00:00:00", timestamps=True, services=["web"])
    args = run.call_args.args[0]
    assert args[args.index("--since") + 1] == "10m"
    assert args[args.index("--until") + 1] == "2024-01-01T00:00:00"
    assert "--timestamps" in args
    assert args[-1] == "web"


# ---------- compose_config ----------


def test_compose_config_default_returns_yaml_text():
    yaml_text = "services:\n  web:\n    image: nginx\n"
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok(yaml_text)) as run:
        result = compose_config()
    args = run.call_args.args[0]
    assert "config" in args
    assert "--format" not in args  # default is yaml
    assert result["config"] == yaml_text


def test_compose_config_json_returns_parsed_dict():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok('{"services": {"web": {}}}')) as run:
        result = compose_config(format="json")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "json"
    assert result["config"] == {"services": {"web": {}}}


def test_compose_config_services_only_lists_names():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("web\ndb\n")) as run:
        result = compose_config(services_only=True)
    args = run.call_args.args[0]
    assert "--services" in args
    assert "--format" not in args  # services list and --format json are exclusive
    assert result["config"] == "web\ndb\n"


def test_compose_config_returns_none_on_failure():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_fail("invalid compose file")):
        result = compose_config(format="json")
    assert result["config"] is None
    assert result["raw"]["returncode"] == 1


# ---------- compose_build / compose_pull / compose_restart ----------


def test_compose_build_flags():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_build(pull=True, no_cache=True, services=["web"])
    args = run.call_args.args[0]
    assert "build" in args
    assert "--pull" in args
    assert "--no-cache" in args
    assert args[-1] == "web"


def test_compose_pull_ignore_failures():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_pull(ignore_pull_failures=True, services=["web", "db"])
    args = run.call_args.args[0]
    assert "pull" in args
    assert "--ignore-pull-failures" in args
    assert args[-2:] == ["web", "db"]


def test_compose_restart_with_stop_timeout():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_restart(stop_timeout_seconds=30, services=["web"])
    args = run.call_args.args[0]
    assert "restart" in args
    assert args[args.index("--timeout") + 1] == "30"
    assert args[-1] == "web"


# ---------- compose_run / compose_exec ----------


def test_compose_run_defaults_to_detach_rm_and_no_tty():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_run(service="web", command=["python", "-V"])
    args = run.call_args.args[0]
    assert args[:2] == ["compose", "run"]
    assert "-T" in args
    assert "-d" in args
    assert "--rm" in args
    # service must come before command argv so docker can distinguish them
    assert args.index("web") < args.index("python")
    assert args[-2:] == ["python", "-V"]


def test_compose_run_with_env_workdir_user_name():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_run(
            service="web",
            command=["sh", "-c", "echo hi"],
            workdir="/srv",
            user="1000:1000",
            env={"FOO": "1", "BAR": "two"},
            name="oneoff",
            rm=False,
            detach=False,
            no_deps=True,
        )
    args = run.call_args.args[0]
    assert "-d" not in args
    assert "--rm" not in args
    assert "--no-deps" in args
    assert args[args.index("--workdir") + 1] == "/srv"
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--name") + 1] == "oneoff"
    # env entries get one --env per key=value
    env_values = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert set(env_values) == {"FOO=1", "BAR=two"}


def test_compose_exec_uses_no_tty_and_passes_argv():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_exec(service="web", command=["ls", "/srv"])
    args = run.call_args.args[0]
    assert args[:2] == ["compose", "exec"]
    assert "-T" in args
    assert args.index("web") < args.index("ls")
    assert args[-2:] == ["ls", "/srv"]


def test_compose_exec_with_index_workdir_user_env():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_exec(
            service="web",
            command=["env"],
            index=3,
            workdir="/app",
            user="root",
            env={"DEBUG": "1"},
        )
    args = run.call_args.args[0]
    assert args[args.index("--index") + 1] == "3"
    assert args[args.index("--workdir") + 1] == "/app"
    assert args[args.index("--user") + 1] == "root"
    assert args[args.index("--env") + 1] == "DEBUG=1"


# ---------- compose_list ----------


def test_compose_ls_parses_array():
    body = '[{"Name":"demo","Status":"running(2)"}]'
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok(body)):
        result = compose_list()
    assert result == [{"Name": "demo", "Status": "running(2)"}]


def test_compose_ls_all_flag():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("[]")) as run:
        compose_list(all=True)
    args = run.call_args.args[0]
    assert "--all" in args


def test_compose_ls_raises_on_failure():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_fail("daemon unreachable")):
        with pytest.raises(RuntimeError, match="daemon unreachable"):
            compose_list()


# ---------- compose_stop / compose_start ----------


def test_compose_stop_with_timeout_and_services():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_stop(project_dir="/srv/app", stop_timeout_seconds=15, services=["web"])
    args = run.call_args.args[0]
    assert "stop" in args
    assert args[args.index("--timeout") + 1] == "15"
    assert args[-1] == "web"
    assert run.call_args.kwargs["cwd"] == "/srv/app"


def test_compose_stop_rejects_flag_like_service():
    with pytest.raises(ValueError, match="parses as a flag"):
        compose_stop(services=["--all"])


def test_compose_start_passes_services_last():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_start(project_name="demo", services=["web", "db"])
    args = run.call_args.args[0]
    assert "start" in args
    assert args[args.index("--project-name") + 1] == "demo"
    assert args[-2:] == ["web", "db"]


def test_compose_start_returns_raw_dict_on_failure():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_fail("no such project")):
        result = compose_start()
    assert result["returncode"] == 1
    assert "no such project" in result["stderr"]


# ---------- compose_images ----------


def test_compose_images_parses_json_list():
    body = '[{"Service":"web","Repository":"nginx","Tag":"1.27"}]'
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok(body)) as run:
        result = compose_images(project_dir="/srv/app", services=["web"])
    assert result == [{"Service": "web", "Repository": "nginx", "Tag": "1.27"}]
    argv = run.call_args.args[0]
    assert argv[:1] == ["compose"]
    assert "images" in argv and "--format" in argv and "json" in argv
    assert argv[-1] == "web"


def test_compose_images_single_object_wrapped():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok('{"Service":"web"}')):
        assert compose_images() == [{"Service": "web"}]


def test_compose_images_raises_on_failure():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_fail("no such project")):
        with pytest.raises(RuntimeError, match="compose images"):
            compose_images()


# ---------- compose_port ----------


def test_compose_port_parses_host_and_port():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("0.0.0.0:49153\n")) as run:
        result = compose_port("web", 80, protocol="tcp")
    assert result["published"] == "0.0.0.0:49153"
    assert result["host"] == "0.0.0.0"  # noqa: S104 — asserting parsed CLI output, not binding a socket
    assert result["port"] == 49153
    argv = run.call_args.args[0]
    assert "port" in argv
    assert "--protocol" in argv and "tcp" in argv
    assert argv[-2:] == ["web", "80"]


def test_compose_port_passes_index_and_udp():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("0.0.0.0:5353")) as run:
        compose_port("dns", 53, protocol="udp", index=2)
    argv = run.call_args.args[0]
    assert "udp" in argv
    assert "--index" in argv and "2" in argv


def test_compose_port_unpublished_is_none():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("")):
        result = compose_port("web", 80)
    assert result["published"] is None
    assert result["host"] is None and result["port"] is None
    assert result["bindings"] == []


def test_compose_port_multiline_parses_first_binding_and_lists_all():
    # A port can be published on several addresses (IPv4 + IPv6); each is its own line.
    out = "0.0.0.0:8080\n[::]:8080\n"
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok(out)):
        result = compose_port("web", 80)
    # First binding drives the scalar fields; no newline/second-line leakage into host.
    assert result["published"] == "0.0.0.0:8080"
    assert result["host"] == "0.0.0.0"  # noqa: S104 — asserting parsed CLI output, not binding a socket
    assert result["port"] == 8080
    # All bindings are preserved, and the IPv6 line splits on the last colon (port stays intact).
    assert result["bindings"] == ["0.0.0.0:8080", "[::]:8080"]


def test_compose_port_raises_on_failure():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_fail("no such service")):
        with pytest.raises(RuntimeError, match="compose port"):
            compose_port("web", 80)


# ---------- compose_wait ----------


def test_compose_wait_builds_args_and_returns_raw():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("0\n")) as run:
        result = compose_wait(["batch"], project_dir="/srv/app", timeout_seconds=120)
    assert result["returncode"] == 0
    argv = run.call_args.args[0]
    assert "wait" in argv
    assert argv[-1] == "batch"
    assert run.call_args.kwargs["timeout"] == 120


def test_compose_wait_requires_a_service():
    with pytest.raises(ValueError, match="at least one"):
        compose_wait([])


# ---------- compose_top ----------


def test_compose_top_returns_raw_output():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok("UID PID ...")) as run:
        result = compose_top(services=["web"])
    assert result["stdout"] == "UID PID ..."
    argv = run.call_args.args[0]
    assert "top" in argv
    assert argv[-1] == "web"


# ---------- compose_cp ----------


def test_compose_cp_builds_args_both_positionals():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_cp("web:/app/log.txt", "/tmp/log.txt", index=2, all_containers=True)
    argv = run.call_args.args[0]
    assert "cp" in argv
    assert "--index" in argv and "2" in argv
    assert "--all" in argv
    assert argv[-2:] == ["web:/app/log.txt", "/tmp/log.txt"]


def test_compose_cp_rejects_stdout_dash_dest():
    # `-` (stdout) starts with '-', so safe_positional blocks it; binary streaming isn't supported here.
    with pytest.raises(ValueError, match="flag"):
        compose_cp("web:/app/log.txt", "-")


# ---------- compose_kill / pause / unpause ----------


def test_compose_kill_default_signal_omits_flag():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_kill(services=["web"])
    argv = run.call_args.args[0]
    assert "kill" in argv
    assert "--signal" not in argv  # SIGKILL is the default; no flag needed
    assert argv[-1] == "web"


def test_compose_kill_custom_signal():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_kill(signal="SIGTERM", remove_orphans=True)
    argv = run.call_args.args[0]
    assert "--signal" in argv and "SIGTERM" in argv
    assert "--remove-orphans" in argv


def test_compose_pause_and_unpause():
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_pause(services=["web"])
    assert "pause" in run.call_args.args[0]
    with patch("docker_mcp.tools.compose.run_docker", return_value=_ok()) as run:
        compose_unpause(services=["web"])
    assert "unpause" in run.call_args.args[0]
