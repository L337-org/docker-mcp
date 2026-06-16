import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests.exceptions
from docker.errors import DockerException

from docker_mcp.tools.containers import (
    commit_container,
    container_diff,
    container_logs,
    container_stats,
    container_top,
    create_container,
    exec_in_container,
    export_container,
    export_container_to_file,
    follow_container_logs,
    get_container,
    get_container_archive,
    get_container_archive_to_file,
    kill_container,
    list_containers,
    pause_container,
    prune_containers,
    put_container_archive,
    put_container_archive_from_file,
    remove_container,
    rename_container,
    resize_container,
    restart_container,
    run_container,
    start_container,
    stop_container,
    unpause_container,
    update_container,
    wait_container,
    wait_for_container_healthy,
)


def _patch():
    return patch("docker_mcp.tools.containers._get_client")


def test_run_container_detached():
    container = MagicMock()
    container.attrs = {"Id": "abc", "Name": "/web"}
    with _patch() as mock_client:
        mock_client.return_value.containers.run.return_value = container
        result = run_container("nginx", name="web")
    assert result == {"Id": "abc", "Name": "/web"}
    args, kwargs = mock_client.return_value.containers.run.call_args
    assert args == ("nginx",)
    assert kwargs["detach"] is True
    assert kwargs["name"] == "web"


def test_run_container_foreground_returns_decoded_logs():
    with _patch() as mock_client:
        mock_client.return_value.containers.run.return_value = b"hello\n"
        result = run_container("alpine", command="echo hello", detach=False)
    assert result == "hello\n"


def test_create_container():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.create.return_value = container
        result = create_container("nginx", command="nginx -g daemon off;")
    assert result == {"Id": "abc"}
    mock_client.return_value.containers.create.assert_called_once()


def test_get_container():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert get_container("web") == {"Id": "abc"}


def test_list_containers_default_args():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.list.return_value = [container]
        result = list_containers()
    assert result == [{"Id": "abc"}]
    mock_client.return_value.containers.list.assert_called_once_with(all=False, sparse=False, ignore_removed=False)


def test_list_containers_with_filters():
    with _patch() as mock_client:
        mock_client.return_value.containers.list.return_value = []
        list_containers(all=True, limit=5, filters={"status": "running"})
    kwargs = mock_client.return_value.containers.list.call_args.kwargs
    assert kwargs["all"] is True
    assert kwargs["limit"] == 5
    assert kwargs["filters"] == {"status": "running"}


def test_list_containers_managed_only_injects_label_filter():
    with _patch() as mock_client:
        mock_client.return_value.containers.list.return_value = []
        list_containers(managed_only=True, filters={"status": "running"})
    kwargs = mock_client.return_value.containers.list.call_args.kwargs
    assert kwargs["filters"]["status"] == "running"
    assert kwargs["filters"]["label"] == "docker-mcp-server.managed=true"


def test_run_container_stamps_provenance_label():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.run.return_value = container
        run_container("nginx", labels={"team": "infra"})
    labels = mock_client.return_value.containers.run.call_args.kwargs["labels"]
    assert labels["docker-mcp-server.managed"] == "true"
    assert labels["team"] == "infra"  # caller label preserved


def test_create_container_stamps_provenance_label():
    container = MagicMock()
    container.attrs = {"Id": "abc"}
    with _patch() as mock_client:
        mock_client.return_value.containers.create.return_value = container
        create_container("nginx")
    labels = mock_client.return_value.containers.create.call_args.kwargs["labels"]
    assert labels["docker-mcp-server.managed"] == "true"
    assert labels["docker-mcp-server.tool"] == "create_container"


def test_prune_containers():
    with _patch() as mock_client:
        mock_client.return_value.containers.prune.return_value = {"SpaceReclaimed": 100}
        assert prune_containers() == {"SpaceReclaimed": 100}


def test_start_container():
    container = MagicMock()
    container.attrs = {"State": {"Status": "running"}}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        start_container("web")
    container.start.assert_called_once()
    container.reload.assert_called_once()


def test_stop_container_uses_timeout():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        stop_container("web", timeout=5)
    container.stop.assert_called_once_with(timeout=5)


def test_restart_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        restart_container("web", timeout=30)
    container.restart.assert_called_once_with(timeout=30)


def test_kill_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        kill_container("web", signal="SIGTERM")
    container.kill.assert_called_once_with(signal="SIGTERM")


def test_pause_and_unpause_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        pause_container("web")
        unpause_container("web")
    container.pause.assert_called_once()
    container.unpause.assert_called_once()


def test_remove_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert remove_container("web", v=True, force=True) is True
    container.remove.assert_called_once_with(v=True, link=False, force=True)


def test_container_logs_decodes_bytes():
    container = MagicMock()
    container.logs.return_value = b"line1\nline2\n"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_logs("web") == "line1\nline2\n"


def test_follow_container_logs_stops_at_limit():
    container = MagicMock()
    container.logs.return_value = iter([b"a\nb\n", b"c\nd\n", b"e\nf\n"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = follow_container_logs("web", limit_lines=3)
    assert result == "a\nb\nc"
    kwargs = container.logs.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["follow"] is True


def test_follow_container_logs_returns_all_when_stream_ends_first():
    container = MagicMock()
    container.logs.return_value = iter([b"only\n"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert follow_container_logs("web", limit_lines=200) == "only"


def test_follow_container_logs_returns_on_timeout_when_quiet():
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
        result = follow_container_logs("web", timeout_seconds=0.1)
    assert result == ""
    assert closed.is_set()


def test_container_stats():
    container = MagicMock()
    container.stats.return_value = {"cpu": 1}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert container_stats("web") == {"cpu": 1}
    container.stats.assert_called_once_with(decode=True, stream=False)


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
        result = exec_in_container("web", ["sh", "-c", "echo ok"])
    assert result == {"exit_code": 0, "output": "ok\n"}


def test_commit_container():
    container = MagicMock()
    image = MagicMock()
    image.attrs = {"Id": "img1"}
    container.commit.return_value = image
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = commit_container("web", repository="myrepo", tag="v1")
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


def test_rename_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        rename_container("web", "api")
    container.rename.assert_called_once_with("api")


def test_resize_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert resize_container("web", 24, 80) is True
    container.resize.assert_called_once_with(24, 80)


def test_update_container():
    container = MagicMock()
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        update_container("web", {"cpu_shares": 512})
    container.update.assert_called_once_with(cpu_shares=512)


def test_wait_container_uses_finite_default_timeout():
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert wait_container("web") == {"StatusCode": 0}
    # Default timeout is finite so the tool can't block the server indefinitely.
    container.wait.assert_called_once_with(timeout=600, condition="not-running")


def test_wait_container_raises_clean_error_on_timeout():
    container = MagicMock()
    container.wait.side_effect = requests.exceptions.ReadTimeout("timed out")
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(RuntimeError, match="did not reach condition .* within 600s"):
            wait_container("web")


def test_export_container():
    container = MagicMock()
    container.export.return_value = iter([b"chunk1", b"chunk2"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert export_container("web") == b"chunk1chunk2"


def test_export_container_raises_when_max_bytes_exceeded():
    container = MagicMock()
    container.export.return_value = iter([b"x" * 50, b"x" * 60])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(ValueError, match="exceeded max_bytes=100"):
            export_container("web", max_bytes=100)


def test_get_container_archive():
    container = MagicMock()
    container.get_archive.return_value = (iter([b"tar"]), {"name": "etc"})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = get_container_archive("web", "/etc")
    assert result == {"archive": b"tar", "stat": {"name": "etc"}}


def test_get_container_archive_raises_when_max_bytes_exceeded():
    container = MagicMock()
    container.get_archive.return_value = (iter([b"x" * 50, b"x" * 60]), {"name": "etc"})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(ValueError, match="exceeded max_bytes=100"):
            get_container_archive("web", "/etc", max_bytes=100)


def test_put_container_archive():
    container = MagicMock()
    container.put_archive.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert put_container_archive("web", "/etc", b"tar") is True
    container.put_archive.assert_called_once_with("/etc", b"tar")


# ---------- file-path archive variants ----------


def test_export_container_to_file_streams_and_returns_metadata(tmp_path):
    container = MagicMock()
    container.export.return_value = iter([b"aa", b"bbb"])
    dest = tmp_path / "ct.tar"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = export_container_to_file("web", str(dest))
    assert dest.read_bytes() == b"aabbb"
    assert result == {"path": str(dest), "bytes_written": 5}


def test_export_container_to_file_refuses_existing(tmp_path):
    dest = tmp_path / "ct.tar"
    dest.write_bytes(b"old")
    container = MagicMock()
    container.export.return_value = iter([b"new"])
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        with pytest.raises(FileExistsError):
            export_container_to_file("web", str(dest))


def test_get_container_archive_to_file_writes_and_returns_stat(tmp_path):
    container = MagicMock()
    container.get_archive.return_value = (iter([b"tar", b"data"]), {"name": "etc", "size": 7})
    dest = tmp_path / "etc.tar"
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = get_container_archive_to_file("web", "/etc", str(dest))
    assert dest.read_bytes() == b"tardata"
    assert result == {"path": str(dest), "bytes_written": 7, "stat": {"name": "etc", "size": 7}}


def test_put_container_archive_from_file_streams_handle(tmp_path):
    src = tmp_path / "payload.tar"
    src.write_bytes(b"archive-bytes")
    container = MagicMock()
    container.put_archive.return_value = True
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert put_container_archive_from_file("web", "/dest", str(src)) is True
    call = container.put_archive.call_args
    assert call.args[0] == "/dest"
    assert hasattr(call.args[1], "read")  # an open file handle, not raw bytes


def test_follow_container_logs_returns_collected_when_stream_close_raises():
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
        result = follow_container_logs("web", limit_lines=200)
    assert result == "line1\nline2"


# ---------- wait_for_container_healthy ----------


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


def test_wait_for_container_healthy_returns_when_healthy():
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "healthy"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = wait_for_container_healthy("web", timeout=5)
    assert result["healthy"] is True
    assert result["health"] == "healthy"
    assert result["timed_out"] is False
    container.reload.assert_called()


def test_wait_for_container_healthy_polls_through_starting():
    container = _health_container(
        {"State": {"Status": "running", "Health": {"Status": "starting"}}},
        {"State": {"Status": "running", "Health": {"Status": "healthy"}}},
    )
    with _patch() as mock_client, patch("docker_mcp.tools.containers.time.sleep") as sleep:
        mock_client.return_value.containers.get.return_value = container
        result = wait_for_container_healthy("web", timeout=10, poll_interval=0.01)
    assert result["healthy"] is True
    assert container.reload.call_count == 2  # starting, then healthy
    sleep.assert_called_once()  # slept once between the two polls


def test_wait_for_container_healthy_reports_unhealthy():
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "unhealthy"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = wait_for_container_healthy("web", timeout=5)
    assert result["healthy"] is False
    assert result["health"] == "unhealthy"


def test_wait_for_container_healthy_stops_if_container_exits():
    container = _health_container({"State": {"Status": "exited", "Health": {"Status": "starting"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = wait_for_container_healthy("batch", timeout=5)
    assert result["healthy"] is False
    assert result["status"] == "exited"


def test_wait_for_container_healthy_no_healthcheck_returns_promptly():
    # Running container with no Health key (no HEALTHCHECK): return at once, health=None.
    container = _health_container({"State": {"Status": "running"}})
    with _patch() as mock_client, patch("docker_mcp.tools.containers.time.sleep") as sleep:
        mock_client.return_value.containers.get.return_value = container
        result = wait_for_container_healthy("web", timeout=5)
    assert result["healthy"] is False
    assert result["health"] is None
    assert result["status"] == "running"
    sleep.assert_not_called()  # did not poll-wait


def test_wait_for_container_healthy_times_out():
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "starting"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        result = wait_for_container_healthy("web", timeout=0.0)  # deadline already passed
    assert result["healthy"] is False
    assert result["timed_out"] is True
    assert result["health"] == "starting"


def test_wait_for_container_healthy_sleep_bounded_by_timeout():
    # A poll_interval far larger than the timeout must NOT push the total wait past the timeout:
    # the sleep is clamped to the remaining time, so this returns in ~timeout, not ~poll_interval.
    container = _health_container({"State": {"Status": "running", "Health": {"Status": "starting"}}})
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        started = time.monotonic()
        result = wait_for_container_healthy("web", timeout=0.05, poll_interval=100)
        elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert elapsed < 2.0, f"sleep overshot the timeout ({elapsed:.2f}s); should be bounded near 0.05s"


def test_wait_for_container_healthy_rejects_nonpositive_poll_interval():
    with pytest.raises(ValueError, match="poll_interval"):
        wait_for_container_healthy("web", poll_interval=0)
