import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests.exceptions
from docker.errors import DockerException

from docker_mcp.tools.containers import (
    _read_log_tail,
    _read_stats_summary,
    _summarize_stats,
    container_commit,
    container_diff,
    container_logs,
    container_stats,
    container_top,
    container_create,
    container_exec,
    container_export,
    container_inspect,
    container_archive_get,
    container_archive_get_to_file,
    container_kill,
    container_list,
    container_pause,
    container_prune,
    container_archive_put,
    container_remove,
    container_rename,
    container_restart,
    container_run,
    container_start,
    container_stop,
    container_unpause,
    container_update,
    container_wait,
)


def _patch():
    return patch("docker_mcp.tools.containers._get_client")


def test_run_container_detached():
    container = MagicMock()
    container.attrs = {"Id": "abc", "Name": "/web"}
    with _patch() as mock_client:
        mock_client.return_value.containers.run.return_value = container
        result = container_run("nginx", name="web")
    assert result == {"Id": "abc", "Name": "/web"}
    args, kwargs = mock_client.return_value.containers.run.call_args
    assert args == ("nginx",)
    assert kwargs["detach"] is True
    assert kwargs["name"] == "web"


def test_run_container_foreground_returns_decoded_logs():
    with _patch() as mock_client:
        mock_client.return_value.containers.run.return_value = b"hello\n"
        result = container_run("alpine", command="echo hello", detach=False)
    assert result == "hello\n"


def test_container_create():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.create.return_value = container
        result = container_create("nginx", command="nginx -g daemon off;")
    assert result == {"Id": "abc"}
    mock_client.return_value.containers.create.assert_called_once()


def test_container_inspect():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_inspect("web") == {"Id": "abc"}


def test_list_containers_default_args():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.list.return_value = [container]
        result = container_list()
    assert result == [{"Id": "abc"}]
    mock_client.return_value.containers.list.assert_called_once_with(all=False, sparse=False, ignore_removed=False)


def test_list_containers_with_filters():
    with _patch() as mock_client:
        mock_client.return_value.containers.list.return_value = []
        container_list(all=True, limit=5, filters={"status": "running"})
    kwargs = mock_client.return_value.containers.list.call_args.kwargs
    assert kwargs["all"] is True
    assert kwargs["limit"] == 5
    assert kwargs["filters"] == {"status": "running"}


def test_list_containers_managed_only_injects_label_filter():
    with _patch() as mock_client:
        mock_client.return_value.containers.list.return_value = []
        container_list(managed_only=True, filters={"status": "running"})
    kwargs = mock_client.return_value.containers.list.call_args.kwargs
    assert kwargs["filters"]["status"] == "running"
    assert kwargs["filters"]["label"] == "docker-mcp-server.managed=true"


def test_run_container_stamps_provenance_label():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.run.return_value = container
        container_run("nginx", labels={"team": "infra"})
    labels = mock_client.return_value.containers.run.call_args.kwargs["labels"]
    assert labels["docker-mcp-server.managed"] == "true"
    assert labels["team"] == "infra"  # caller label preserved


def test_create_container_stamps_provenance_label():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.create.return_value = container
        container_create("nginx")
    labels = mock_client.return_value.containers.create.call_args.kwargs["labels"]
    assert labels["docker-mcp-server.managed"] == "true"
    assert labels["docker-mcp-server.tool"] == "container_create"


def test_container_prune():
    with _patch() as mock_client:
        mock_client.return_value.containers.prune.return_value = {"SpaceReclaimed": 100}
        assert container_prune() == {"SpaceReclaimed": 100}


def test_container_start():
    container = MagicMock()
    container.attrs = {"State": {"Status": "running"}}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_start("web")
    container.start.assert_called_once()
    container.reload.assert_called_once()


def test_stop_container_uses_timeout():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_stop("web", stop_timeout_seconds=5)
    container.stop.assert_called_once_with(timeout=5)


def test_container_restart():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_restart("web", stop_timeout_seconds=30)
    container.restart.assert_called_once_with(timeout=30)


def test_container_kill():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_kill("web", signal="SIGTERM")
    container.kill.assert_called_once_with(signal="SIGTERM")


def test_pause_and_unpause_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_pause("web")
        container_unpause("web")
    container.pause.assert_called_once()
    container.unpause.assert_called_once()


def test_container_remove():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_remove("web", volumes=True, force=True) is True
    container.remove.assert_called_once_with(v=True, link=False, force=True)


def test_container_logs_decodes_bytes():
    container = MagicMock()
    container.logs.return_value = b"line1\nline2\n"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_logs("web") == "line1\nline2\n"


def test_container_logs_follow_stops_at_limit():
    container = MagicMock()
    container.logs.return_value = iter([b"a\nb\n", b"c\nd\n", b"e\nf\n"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_logs("web", follow=True, limit_lines=3)
    assert result == "a\nb\nc"
    kwargs = container.logs.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["follow"] is True


def test_container_logs_follow_returns_all_when_stream_ends_first():
    container = MagicMock()
    container.logs.return_value = iter([b"only\n"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_logs("web", follow=True, limit_lines=200) == "only"


def test_container_logs_follow_returns_on_timeout_when_quiet():
    # A long-lived container that emits nothing would block forever; the watchdog closes the
    # CancellableStream after timeout_seconds so the call returns what it has.
    closed = threading.Event()

    class _BlockingLogStream:
        def __iter__(self):
            return self

        def __next__(self):
            closed.wait(timeout=5)
            raise StopIteration

        def close(self):
            closed.set()

    container = MagicMock()
    container.logs.return_value = _BlockingLogStream()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_logs("web", follow=True, timeout_seconds=0.1)
    assert result == ""
    assert closed.is_set()


def test_container_stats():
    container = MagicMock()
    container.stats.return_value = {"cpu": 1}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_stats("web") == {"cpu": 1}
    # `decode` is only valid with stream=True; a one-shot stream=False read already returns a dict.
    container.stats.assert_called_once_with(stream=False)


def test_container_top():
    container = MagicMock()
    container.top.return_value = {"Processes": []}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_top("web") == {"Processes": []}


def test_exec_in_container_decodes_output():
    container = MagicMock()
    exec_result = MagicMock(exit_code=0, output=b"ok\n")
    container.exec_run.return_value = exec_result
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_exec("web", ["sh", "-c", "echo ok"])
    assert result == {"exit_code": 0, "output": "ok\n"}


def test_container_commit():
    container = MagicMock()
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    container.commit.return_value = image
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_commit("web", repository="myrepo", tag="v1")
    assert result == {"Id": "img1"}
    container.commit.assert_called_once_with(
        repository="myrepo",
        tag="v1",
        message=None,
        author=None,
        pause=True,
        changes=None,
        conf=None,
    )


def test_container_diff():
    container = MagicMock()
    container.diff.return_value = [{"Path": "/etc"}]
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_diff("web") == [{"Path": "/etc"}]


def test_container_rename():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_rename("web", "api")
    container.rename.assert_called_once_with("api")


def test_container_update():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_update("web", {"cpu_shares": 512})
    container.update.assert_called_once_with(cpu_shares=512)


def test_container_wait_uses_finite_default_timeout():
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web")
    assert result["met"] is True
    assert result["timed_out"] is False
    assert result["status_code"] == 0
    # Default timeout is finite so the tool can't block the server indefinitely.
    container.wait.assert_called_once_with(timeout=600, condition="not-running")


def test_container_wait_rounds_fractional_timeout_up():
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        container_wait("web", timeout_seconds=0.5)
    # int() would truncate 0.5 to an immediate 0s daemon timeout; the tool rounds up instead.
    container.wait.assert_called_once_with(timeout=1, condition="not-running")


def test_container_wait_rejects_negative_timeout():
    with pytest.raises(ValueError, match="timeout_seconds"):
        container_wait("web", timeout_seconds=-1)


def test_container_wait_returns_timed_out_instead_of_raising():
    container = MagicMock()
    container.wait.side_effect = requests.exceptions.ReadTimeout("timed out")
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web")
    assert result["met"] is False
    assert result["timed_out"] is True
    assert result["status_code"] is None


def test_container_wait_surfaces_daemon_error_message():
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 137, "Error": {"Message": "oom"}}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web", until="next-exit")
    assert result["status_code"] == 137
    assert result["error"] == "oom"
    container.wait.assert_called_once_with(timeout=600, condition="next-exit")


def test_container_export():
    container = MagicMock()
    container.export.return_value = iter([b"chunk1", b"chunk2"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_export("web") == b"chunk1chunk2"


def test_container_export_raises_when_max_bytes_exceeded():
    container = MagicMock()
    container.export.return_value = iter([b"x" * 50, b"x" * 60])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(ValueError, match="exceeded max_bytes=100"):
            container_export("web", max_bytes=100)


def test_container_archive_get():
    container = MagicMock()
    container.get_archive.return_value = (iter([b"tar"]), {"name": "etc"})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_archive_get("web", "/etc")
    assert result == {"archive": b"tar", "stat": {"name": "etc"}}


def test_container_archive_get_raises_when_max_bytes_exceeded():
    container = MagicMock()
    container.get_archive.return_value = (iter([b"x" * 50, b"x" * 60]), {"name": "etc"})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(ValueError, match="exceeded max_bytes=100"):
            container_archive_get("web", "/etc", max_bytes=100)


def test_container_archive_put_rejects_ambiguous_source(tmp_path):
    src = tmp_path / "payload.tar"
    src.write_bytes(b"archive-bytes")
    with pytest.raises(ValueError, match="exactly one"):
        container_archive_put("web", "/dest")
    with pytest.raises(ValueError, match="exactly one"):
        container_archive_put("web", "/dest", data=b"tar", from_file=str(src))


def test_container_archive_put():
    container = MagicMock()
    container.put_archive.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_archive_put("web", "/etc", b"tar") is True
    container.put_archive.assert_called_once_with("/etc", b"tar")


# ---------- file-path archive variants ----------


def test_container_export_to_dest_path_streams_and_returns_metadata(tmp_path):
    container = MagicMock()
    container.export.return_value = iter([b"aa", b"bbb"])
    dest = tmp_path / "ct.tar"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_export("web", dest_path=str(dest))
    assert dest.read_bytes() == b"aabbb"
    assert result == {"path": str(dest), "bytes_written": 5}


def test_container_export_to_dest_path_refuses_existing(tmp_path):
    dest = tmp_path / "ct.tar"
    dest.write_bytes(b"old")
    container = MagicMock()
    container.export.return_value = iter([b"new"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(FileExistsError):
            container_export("web", dest_path=str(dest))


def test_container_archive_get_to_file_writes_and_returns_stat(tmp_path):
    container = MagicMock()
    container.get_archive.return_value = (iter([b"tar", b"data"]), {"name": "etc", "size": 7})
    dest = tmp_path / "etc.tar"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_archive_get_to_file("web", "/etc", str(dest))
    assert dest.read_bytes() == b"tardata"
    assert result == {"path": str(dest), "bytes_written": 7, "stat": {"name": "etc", "size": 7}}


def test_container_archive_put_from_file_streams_handle(tmp_path):
    src = tmp_path / "payload.tar"
    src.write_bytes(b"archive-bytes")
    container = MagicMock()
    container.put_archive.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_archive_put("web", "/dest", from_file=str(src)) is True
    call = container.put_archive.call_args
    assert call.args[0] == "/dest"
    assert hasattr(call.args[1], "read")  # an open file handle, not raw bytes


def test_container_logs_follow_returns_collected_when_stream_close_raises():
    # ssh:// daemons: CancellableStream.close() raises in the finally — the collected lines must
    # still be returned rather than the close error replacing them.
    class _FiniteRaisingCloseStream:
        def __init__(self):
            self._it = iter([b"line1\n", b"line2\n"])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def close(self):
            raise DockerException("Cancellable streams not supported for the SSH protocol")

    container = MagicMock()
    container.logs.return_value = _FiniteRaisingCloseStream()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_logs("web", follow=True, limit_lines=200)
    assert result == "line1\nline2"


# ---------- container_wait(until="healthy") ----------


def _health_container(*states: dict) -> MagicMock:
    """A mock container whose attrs advance through `states` on each reload() call."""
    container = MagicMock()
    container.attrs = states[0]
    seq = {"i": 0}

    def _reload():
        container.attrs = states[min(seq["i"], len(states) - 1)]
        seq["i"] += 1

    container.reload.side_effect = _reload
    return container


def test_container_wait_healthy_returns_when_healthy():
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "healthy"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web", until="healthy", timeout_seconds=5)
    assert result["met"] is True
    assert result["health"] == "healthy"
    assert result["timed_out"] is False
    container.reload.assert_called()


def test_container_wait_healthy_polls_through_starting():
    container = _health_container(
        {"State": {"Status": "running", "Health": {"Status": "starting"}}},
        {"State": {"Status": "running", "Health": {"Status": "healthy"}}},
    )
    with _patch() as mock_client, patch("docker_mcp.tools.containers.time.sleep") as sleep:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web", until="healthy", timeout_seconds=10, poll_interval=0.01)
    assert result["met"] is True
    assert container.reload.call_count == 2  # starting, then healthy
    sleep.assert_called_once()  # slept once between the two polls


def test_container_wait_healthy_reports_unhealthy():
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "unhealthy"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web", until="healthy", timeout_seconds=5)
    assert result["met"] is False
    assert result["health"] == "unhealthy"


def test_container_wait_healthy_stops_if_container_exits():
    container = _health_container({"State": {"Status": "exited", "Health": {"Status": "starting"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("batch", until="healthy", timeout_seconds=5)
    assert result["met"] is False
    assert result["status"] == "exited"


def test_container_wait_healthy_no_healthcheck_returns_promptly():
    # Running container with no Health key (no HEALTHCHECK): return at once, health=None.
    container = _health_container({"State": {"Status": "running"}})
    with _patch() as mock_client, patch("docker_mcp.tools.containers.time.sleep") as sleep:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web", until="healthy", timeout_seconds=5)
    assert result["met"] is False
    assert result["health"] is None
    assert result["status"] == "running"
    sleep.assert_not_called()  # did not poll-wait


def test_container_wait_healthy_times_out():
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "starting"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = container_wait("web", until="healthy", timeout_seconds=0.0)  # deadline already passed
    assert result["met"] is False
    assert result["timed_out"] is True
    assert result["health"] == "starting"


def test_container_wait_healthy_sleep_bounded_by_timeout():
    # A poll_interval far larger than the timeout must NOT push the total wait past the timeout:
    # the sleep is clamped to the remaining time, so this returns in ~timeout, not ~poll_interval.
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "starting"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        started = time.monotonic()
        result = container_wait("web", until="healthy", timeout_seconds=0.05, poll_interval=100)
        elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert elapsed < 2.0, f"sleep overshot the timeout ({elapsed:.2f}s); should be bounded near 0.05s"


def test_container_wait_healthy_rejects_nonpositive_poll_interval():
    with pytest.raises(ValueError, match="poll_interval"):
        container_wait("web", until="healthy", poll_interval=0)


# ---------- shared resource helpers: log tail + computed stats summary ----------


def test_read_log_tail_decodes_and_bounds_the_read():
    container = MagicMock()
    container.logs.return_value = b"hello\nworld\n"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert _read_log_tail("web") == "hello\nworld\n"
    kwargs = container.logs.call_args.kwargs
    assert kwargs["stream"] is False
    assert kwargs["tail"] == 200  # bounded by default so a resource read can't flood context


def test_read_stats_summary_raises_when_not_running():
    container = MagicMock()
    container.attrs = {"State": {"Status": "exited"}}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(RuntimeError, match="not running"):
            _read_stats_summary("job")


def test_summarize_stats_computes_cpu_mem_net_blk():
    mb = 1024 * 1024
    snapshot = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200, "percpu_usage": [1, 1]},
            "system_cpu_usage": 2000,
            "online_cpus": 2,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 200 * mb, "limit": 500 * mb, "stats": {"inactive_file": 50 * mb}},
        "networks": {"eth0": {"rx_bytes": 10 * mb, "tx_bytes": 2 * mb}},
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 8 * mb},
                {"op": "Write", "value": 4 * mb},
            ]
        },
    }
    summary = _summarize_stats("web", snapshot)
    assert summary["container"] == "web"
    # cpu_delta=100, system_delta=1000, 2 cpus -> (100/1000)*2*100 = 20.0
    assert summary["cpu_percent"] == 20.0
    # usage 200MB minus 50MB reclaimable cache -> 150MB used of 500MB -> 30%
    assert summary["mem_used_mb"] == 150.0
    assert summary["mem_limit_mb"] == 500.0
    assert summary["mem_percent"] == 30.0
    assert summary["net_rx_mb"] == 10.0
    assert summary["net_tx_mb"] == 2.0
    assert summary["blk_read_mb"] == 8.0
    assert summary["blk_write_mb"] == 4.0


def test_summarize_stats_degrades_to_zero_on_empty_snapshot():
    # A stats shape missing the usual keys (cgroup quirks) must not raise.
    summary = _summarize_stats("web", {})
    assert summary["cpu_percent"] == 0.0
    assert summary["mem_percent"] == 0.0
    assert summary["blk_read_mb"] == 0.0
