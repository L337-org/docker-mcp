import io
import pathlib
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from docker_mcp.tools._cli import CliResult
from docker_mcp.tools.compose import (
    _global_args,
    compose_build,
    compose_config,
    compose_copy,
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


# ---------- remote-exec fallback ----------


def test_compose_stages_the_project_directory_when_there_is_no_local_plugin():
    """
    Every compose subcommand reads its file from a working directory, so the remote path has to copy
    that directory over first — and the `-f` values go with it so an absolute one can be reconciled.
    """
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=True) as should,
        patch("docker_mcp.tools.compose.remote_stage_and_exec", return_value=_ok("")) as staged,
        patch("docker_mcp.tools.compose.run_docker") as run,
        patch("docker_mcp.tools.compose.require_plugin") as require,
    ):
        compose_up(project_dir="/srv/app", files=["docker-compose.yml"], host="prod")
    run.assert_not_called()
    require.assert_not_called()  # a *local* capability question, irrelevant once we go remote
    should.assert_called_with("prod", plugin="compose")
    assert staged.call_args.args == ("prod", ["compose", "-f", "docker-compose.yml", "up", "-d"])
    assert staged.call_args.kwargs["cwd"] == "/srv/app"
    assert staged.call_args.kwargs["path_values"] == ["docker-compose.yml"]


def test_compose_passes_project_dir_none_through_so_the_backend_resolves_it():
    # The backend turns None into the server's own cwd; the tool must not pre-empt that with a guess.
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.compose.remote_stage_and_exec", return_value=_ok("")) as staged,
    ):
        compose_ps(host="prod")
    assert staged.call_args.kwargs["cwd"] is None


def test_compose_uses_the_local_cli_when_it_can():
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.compose.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.compose.run_docker", return_value=_ok("")) as run,
        patch("docker_mcp.tools.compose.require_plugin") as require,
    ):
        compose_down(project_dir="/srv/app", host="prod")
    staged.assert_not_called()
    require.assert_called_once_with("compose")
    assert run.call_args.kwargs["cwd"] == "/srv/app"


def test_compose_list_runs_remotely_without_staging_anything():
    # The one compose tool that asks the daemon rather than reading a directory.
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.compose.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.compose.remote_exec_cli", return_value=_ok("[]")) as remote,
    ):
        assert compose_list(host="prod") == []
    staged.assert_not_called()
    assert remote.call_args.args == ("prod", ["compose", "ls", "--format", "json"])


def test_compose_cp_is_refused_on_the_remote_path_and_names_the_alternatives():
    """
    Its whole purpose is the *local* side of the copy, which running the CLI on the far host cannot be.
    The SDK-backed archive tools do the same job against any daemon with no local CLI at all, so the
    refusal points at them rather than half-implementing a download.
    """
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.compose.remote_stage_and_exec") as staged,
        patch("docker_mcp.tools.compose.run_docker") as run,
    ):
        with pytest.raises(RuntimeError, match="container_archive_put") as excinfo:
            compose_cp("web:/etc/app.conf", "/tmp/app.conf", host="prod")
    assert "container_archive_get_to_file" in str(excinfo.value)
    staged.assert_not_called()
    run.assert_not_called()


def test_compose_cp_still_works_on_the_local_path():
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=False),
        patch("docker_mcp.tools.compose.run_docker", return_value=_ok("")) as run,
    ):
        compose_cp("web:/etc/app.conf", "/tmp/app.conf")
    assert run.call_args.args[0][-2:] == ["web:/etc/app.conf", "/tmp/app.conf"]


def test_compose_run_command_tokens_are_not_mistaken_for_compose_files():
    """
    `compose_run` / `compose_exec` append an arbitrary container command, so a `-f` inside *that* is not
    a compose file: `command=["python", "-f", "/etc/hosts"]` must not make the remote path upload
    /etc/hosts and rewrite the argument. Only the global prefix `_global_args` emits counts.
    """
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.compose.remote_stage_and_exec", return_value=_ok("")) as staged,
    ):
        compose_run(
            service="app",
            command=["python", "-f", "/etc/hosts"],
            project_dir="/srv/app",
            files=["docker-compose.yml"],
            host="prod",
        )
    assert staged.call_args.kwargs["path_values"] == ["docker-compose.yml"]


def test_compose_exec_command_tokens_are_not_mistaken_for_compose_files():
    with (
        patch("docker_mcp.tools.compose.should_remote_exec", return_value=True),
        patch("docker_mcp.tools.compose.remote_stage_and_exec", return_value=_ok("")) as staged,
    ):
        compose_exec(service="app", command=["tar", "-f", "/etc/hosts", "-x"], host="prod")
    assert staged.call_args.kwargs["path_values"] == []


def test_compose_cp_docstring_does_not_promise_staging_it_refuses():
    """
    Mechanical guard rather than a note to remember: the staging clause was applied to every tool with a
    `project_dir`, and `compose_cp` is the one where the remote path is a refusal, so nothing is ever
    copied. A future sweep must not put it back.
    """
    assert compose_cp.__doc__ is not None
    assert "copied to the target host" not in compose_cp.__doc__
    # ...and every other project_dir-taking tool must still carry it, so this guard cannot be satisfied
    # by dropping the clause everywhere.
    assert "copied to the target host" in (compose_up.__doc__ or "")


# ---------- compose_copy (SDK-backed, no CLI) ----------


class _FakeContainer:
    def __init__(self, name="proj-web-1", project="proj", service="web", number="1", oneoff="False"):
        self.name = name
        self.labels = {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
            "com.docker.compose.container-number": number,
            "com.docker.compose.oneoff": oneoff,
        }
        self.put_calls: list[tuple[str, int]] = []
        self.get_calls: list[str] = []
        self.archive: bytes = b""
        self.put_result = True

    def get_archive(self, path):
        self.get_calls.append(path)
        return iter([self.archive]), {"name": pathlib.Path(path).name, "size": len(self.archive)}

    def put_archive(self, path, data):
        payload = data if isinstance(data, bytes) else data.read()
        self.put_calls.append((path, len(payload)))
        self._last_payload = payload
        return self.put_result


def _tar_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for name, body in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(body.encode())
            bundle.addfile(info, io.BytesIO(body.encode()))
    return buffer.getvalue()


def _fake_client(containers):
    client = MagicMock()
    client.containers.list.return_value = containers
    return client


def test_compose_copy_pulls_a_container_path_into_a_host_directory(tmp_path):
    container = _FakeContainer()
    container.archive = _tar_bytes({"app.conf": "key=value\n"})
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([container])):
        result = compose_copy("web:/etc/app.conf", str(tmp_path))
    assert container.get_calls == ["/etc/app.conf"]
    # Unpacked, keeping its own name — the semantics `compose_cp` has and the archive tools do not.
    assert (tmp_path / "app.conf").read_text(encoding="utf-8") == "key=value\n"
    assert result["direction"] == "container_to_host"
    assert (result["service"], result["project"], result["entries"]) == ("web", "proj", 1)


def test_compose_copy_pushes_a_host_file_into_a_container_directory(tmp_path):
    source = tmp_path / "app.env"
    source.write_text("TOKEN=x\n", encoding="utf-8")
    container = _FakeContainer()
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([container])):
        result = compose_copy(str(source), "web:/etc/config")
    assert container.put_calls and container.put_calls[0][0] == "/etc/config"
    # The archive's single member keeps the source basename, matching `docker cp ./x SERVICE:/dir/`.
    with tarfile.open(fileobj=io.BytesIO(container._last_payload)) as bundle:
        assert bundle.getnames() == ["app.env"]
    assert result["direction"] == "host_to_container"


def test_compose_copy_refuses_when_both_or_neither_side_names_a_service(tmp_path):
    with pytest.raises(ValueError, match="exactly one side"):
        compose_copy("web:/etc/a", "db:/etc/b")
    with pytest.raises(ValueError, match="exactly one side"):
        compose_copy(str(tmp_path / "a"), str(tmp_path / "b"))


def test_compose_copy_selects_the_replica_and_can_disambiguate_by_project(tmp_path):
    container = _FakeContainer(name="proj-web-3", number="3")
    container.archive = _tar_bytes({"x": "y"})
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([container])) as get_client:
        compose_copy("web:/x", str(tmp_path), index=3, project_name="proj")
    labels = get_client.return_value.containers.list.call_args.kwargs["filters"]["label"]
    assert "com.docker.compose.service=web" in labels
    assert "com.docker.compose.container-number=3" in labels
    assert "com.docker.compose.project=proj" in labels
    # Stopped containers are eligible: a copy into one is legitimate, and `docker compose cp` allows it.
    assert get_client.return_value.containers.list.call_args.kwargs["all"] is True


def test_compose_copy_ignores_one_off_run_containers(tmp_path):
    oneoff = _FakeContainer(name="proj-web-run-abc", oneoff="True")
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([oneoff])):
        with pytest.raises(RuntimeError, match="No Compose container matches"):
            compose_copy("web:/x", str(tmp_path))


def test_compose_copy_names_the_candidate_projects_when_ambiguous(tmp_path):
    one = _FakeContainer(name="a-web-1", project="alpha")
    two = _FakeContainer(name="b-web-1", project="beta")
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([one, two])):
        with pytest.raises(RuntimeError, match=r"more than one Compose project.*alpha.*beta"):
            compose_copy("web:/x", str(tmp_path))


def test_compose_copy_requires_an_existing_host_directory(tmp_path):
    container = _FakeContainer()
    container.archive = _tar_bytes({"x": "y"})
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([container])):
        with pytest.raises(ValueError, match="must be an existing directory"):
            compose_copy("web:/x", str(tmp_path / "nope"))


def test_compose_copy_extraction_cannot_escape_the_destination(tmp_path):
    """
    A container is not a trusted source of tar member names. Python does not filter extraction by
    default (`TarFile.extraction_filter` is None on 3.14), so `filter="data"` is passed explicitly —
    without it, a member named `../escaped` would be written outside the destination.
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    container = _FakeContainer()
    container.archive = _tar_bytes({"../escaped": "nope\n"})
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([container])):
        with pytest.raises(tarfile.TarError):
            compose_copy("web:/x", str(dest))
    assert not (tmp_path / "escaped").exists()


def test_compose_copy_surfaces_a_rejected_upload(tmp_path):
    source = tmp_path / "f"
    source.write_text("x", encoding="utf-8")
    container = _FakeContainer()
    container.put_result = False
    with patch("docker_mcp.tools.compose._get_client", return_value=_fake_client([container])):
        with pytest.raises(RuntimeError, match="does not exist in the container"):
            compose_copy(str(source), "web:/missing-dir")


def test_compose_copy_does_not_mistake_a_windows_path_for_a_service(tmp_path):
    # `C:\...` has a colon but is a host path; the service pattern needs 2+ leading characters.
    from docker_mcp.tools.compose import _split_service_path

    assert _split_service_path("C:\\Users\\gavin\\file.txt") is None
    assert _split_service_path("web:/etc/app.conf") == ("web", "/etc/app.conf")
    assert _split_service_path("./local/path") is None
