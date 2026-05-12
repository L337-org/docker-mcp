from unittest.mock import patch

import pytest

from tools._cli import CliResult
from tools.compose import (
    _global_args,
    _parse_compose_json,
    compose_build,
    compose_config,
    compose_down,
    compose_exec,
    compose_logs,
    compose_ls,
    compose_ps,
    compose_pull,
    compose_restart,
    compose_run,
    compose_up,
)


@pytest.fixture(autouse=True)
def _stub_plugin_check():  # pyright: ignore[reportUnusedFunction]
    # Every test that calls `_run_compose` ultimately calls `require_plugin("compose")`.
    # We don't want those tests to shell out to a real `docker compose version` probe.
    with patch("tools.compose.require_plugin"):
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


# ---------- _parse_compose_json ----------


def test_parse_compose_json_handles_array():
    assert _parse_compose_json('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]


def test_parse_compose_json_handles_ndjson():
    assert _parse_compose_json('{"a": 1}\n{"a": 2}\n') == [{"a": 1}, {"a": 2}]


def test_parse_compose_json_handles_single_object():
    assert _parse_compose_json('{"a": 1}') == {"a": 1}


def test_parse_compose_json_empty_returns_none():
    assert _parse_compose_json("") is None
    assert _parse_compose_json("   \n  ") is None


# ---------- compose_up ----------


def test_compose_up_minimal_uses_detach():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_up()
    args = run.call_args.args[0]
    assert args[:2] == ["compose", "up"]
    assert "-d" in args


def test_compose_up_passes_global_flags_and_subcommand_flags():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
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
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_down(volumes=True, remove_orphans=True)
    args = run.call_args.args[0]
    assert "down" in args
    assert "--volumes" in args
    assert "--remove-orphans" in args


# ---------- compose_ps ----------


def test_compose_ps_parses_ndjson():
    body = '{"Name":"web","State":"running"}\n{"Name":"db","State":"running"}\n'
    with patch("tools.compose.run_docker", return_value=_ok(body)):
        result = compose_ps()
    assert result["services"] == [
        {"Name": "web", "State": "running"},
        {"Name": "db", "State": "running"},
    ]
    assert result["raw"]["returncode"] == 0


def test_compose_ps_handles_single_object_response():
    with patch("tools.compose.run_docker", return_value=_ok('{"Name":"web"}')):
        result = compose_ps()
    assert result["services"] == [{"Name": "web"}]


def test_compose_ps_returns_empty_services_on_failure():
    with patch("tools.compose.run_docker", return_value=_fail("no such project")):
        result = compose_ps()
    assert result["services"] == []
    assert result["raw"]["returncode"] == 1
    assert "no such project" in result["raw"]["stderr"]


def test_compose_ps_passes_all_and_services():
    with patch("tools.compose.run_docker", return_value=_ok("[]")) as run:
        compose_ps(services=["web"], all=True)
    args = run.call_args.args[0]
    assert args[-3:] == ["--all", "web"] or args[-2:] == ["--all", "web"] or "--all" in args


# ---------- compose_logs ----------


def test_compose_logs_default_tail_and_no_color():
    with patch("tools.compose.run_docker", return_value=_ok("hello")) as run:
        result = compose_logs()
    args = run.call_args.args[0]
    assert "--no-color" in args
    assert "--no-log-prefix" in args
    assert args[args.index("--tail") + 1] == "200"
    assert result["stdout"] == "hello"


def test_compose_logs_tail_zero_means_all():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_logs(tail=0)
    args = run.call_args.args[0]
    assert args[args.index("--tail") + 1] == "all"


def test_compose_logs_with_since_until_timestamps_services():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_logs(since="10m", until="2024-01-01T00:00:00", timestamps=True, services=["web"])
    args = run.call_args.args[0]
    assert args[args.index("--since") + 1] == "10m"
    assert args[args.index("--until") + 1] == "2024-01-01T00:00:00"
    assert "--timestamps" in args
    assert args[-1] == "web"


# ---------- compose_config ----------


def test_compose_config_default_returns_yaml_text():
    yaml_text = "services:\n  web:\n    image: nginx\n"
    with patch("tools.compose.run_docker", return_value=_ok(yaml_text)) as run:
        result = compose_config()
    args = run.call_args.args[0]
    assert "config" in args
    assert "--format" not in args  # default is yaml
    assert result["config"] == yaml_text


def test_compose_config_json_returns_parsed_dict():
    with patch("tools.compose.run_docker", return_value=_ok('{"services": {"web": {}}}')) as run:
        result = compose_config(format="json")
    args = run.call_args.args[0]
    assert args[args.index("--format") + 1] == "json"
    assert result["config"] == {"services": {"web": {}}}


def test_compose_config_services_only_lists_names():
    with patch("tools.compose.run_docker", return_value=_ok("web\ndb\n")) as run:
        result = compose_config(services_only=True)
    args = run.call_args.args[0]
    assert "--services" in args
    assert "--format" not in args  # services list and --format json are exclusive
    assert result["config"] == "web\ndb\n"


def test_compose_config_returns_none_on_failure():
    with patch("tools.compose.run_docker", return_value=_fail("invalid compose file")):
        result = compose_config(format="json")
    assert result["config"] is None
    assert result["raw"]["returncode"] == 1


# ---------- compose_build / compose_pull / compose_restart ----------


def test_compose_build_flags():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_build(pull=True, no_cache=True, services=["web"])
    args = run.call_args.args[0]
    assert "build" in args
    assert "--pull" in args
    assert "--no-cache" in args
    assert args[-1] == "web"


def test_compose_pull_ignore_failures():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_pull(ignore_pull_failures=True, services=["web", "db"])
    args = run.call_args.args[0]
    assert "pull" in args
    assert "--ignore-pull-failures" in args
    assert args[-2:] == ["web", "db"]


def test_compose_restart_with_stop_timeout():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_restart(stop_timeout_seconds=30, services=["web"])
    args = run.call_args.args[0]
    assert "restart" in args
    assert args[args.index("--timeout") + 1] == "30"
    assert args[-1] == "web"


# ---------- compose_run / compose_exec ----------


def test_compose_run_defaults_to_detach_rm_and_no_tty():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
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
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
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
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
        compose_exec(service="web", command=["ls", "/srv"])
    args = run.call_args.args[0]
    assert args[:2] == ["compose", "exec"]
    assert "-T" in args
    assert args.index("web") < args.index("ls")
    assert args[-2:] == ["ls", "/srv"]


def test_compose_exec_with_index_workdir_user_env():
    with patch("tools.compose.run_docker", return_value=_ok()) as run:
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


# ---------- compose_ls ----------


def test_compose_ls_parses_array():
    body = '[{"Name":"demo","Status":"running(2)"}]'
    with patch("tools.compose.run_docker", return_value=_ok(body)):
        result = compose_ls()
    assert result == [{"Name": "demo", "Status": "running(2)"}]


def test_compose_ls_all_flag():
    with patch("tools.compose.run_docker", return_value=_ok("[]")) as run:
        compose_ls(all=True)
    args = run.call_args.args[0]
    assert "--all" in args


def test_compose_ls_raises_on_failure():
    with patch("tools.compose.run_docker", return_value=_fail("daemon unreachable")):
        with pytest.raises(RuntimeError, match="daemon unreachable"):
            compose_ls()
