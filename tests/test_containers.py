from unittest.mock import MagicMock, patch

import pytest

from tools.containers import (
    commit_container,
    container_diff,
    container_logs,
    container_stats,
    container_top,
    create_container,
    exec_in_container,
    export_container,
    follow_container_logs,
    get_container,
    get_container_archive,
    kill_container,
    list_containers,
    pause_container,
    prune_containers,
    put_container_archive,
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
)


def _patch():
    return patch("tools.containers._get_client")


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


def test_wait_container():
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    with _patch() as mock_client:
        mock_client.return_value.containers.get.return_value = container
        assert wait_container("web") == {"StatusCode": 0}
    container.wait.assert_called_once_with(timeout=None, condition="not-running")


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
