from unittest.mock import MagicMock, patch


from docker_mcp.tools.swarm import (
    swarm_join_tokens,
    swarm_unlock_key,
    swarm_init,
    swarm_join,
    swarm_leave,
    swarm_inspect,
    swarm_task_inspect,
    swarm_task_list,
    swarm_unlock,
    swarm_update,
)


def _patch():
    return patch("docker_mcp.tools.swarm._get_client")


def test_swarm_init():
    with _patch() as mock_client:
        mock_client.return_value.swarm.init.return_value = "node-id"
        assert swarm_init(advertise_addr="10.0.0.1") == "node-id"
    kwargs = mock_client.return_value.swarm.init.call_args.kwargs
    assert kwargs["advertise_addr"] == "10.0.0.1"
    assert kwargs["listen_addr"] == "0.0.0.0:2377"


def test_swarm_join():
    with _patch() as mock_client:
        mock_client.return_value.swarm.join.return_value = True
        assert swarm_join(["10.0.0.1:2377"], "TOKEN") is True
    kwargs = mock_client.return_value.swarm.join.call_args.kwargs
    assert kwargs["remote_addrs"] == ["10.0.0.1:2377"]
    assert kwargs["join_token"] == "TOKEN"


def test_swarm_leave():
    with _patch() as mock_client:
        mock_client.return_value.swarm.leave.return_value = True
        assert swarm_leave(force=True) is True
    mock_client.return_value.swarm.leave.assert_called_once_with(force=True)


def test_swarm_update():
    with _patch() as mock_client:
        mock_client.return_value.swarm.update.return_value = True
        assert swarm_update(rotate_worker_token=True) is True
    mock_client.return_value.swarm.update.assert_called_once_with(
        rotate_worker_token=True,
        rotate_manager_token=False,
        rotate_manager_unlock_key=False,
    )


def test_swarm_inspect():
    swarm = MagicMock()
    swarm.attrs = {"ID": "swarm1"}
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_inspect() == {"ID": "swarm1"}
    swarm.reload.assert_called_once()


def test_swarm_unlock():
    with _patch() as mock_client:
        mock_client.return_value.swarm.unlock.return_value = True
        assert swarm_unlock("KEY") is True
    mock_client.return_value.swarm.unlock.assert_called_once_with("KEY")


def test_swarm_unlock_key():
    with _patch() as mock_client:
        mock_client.return_value.swarm.get_unlock_key.return_value = {"UnlockKey": "K"}
        assert swarm_unlock_key() == {"UnlockKey": "K"}


def test_get_swarm_join_tokens_reloads_and_extracts():
    swarm = MagicMock()
    swarm.attrs = {"JoinTokens": {"Worker": "SWMTKN-worker", "Manager": "SWMTKN-manager"}}
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_join_tokens() == {"Worker": "SWMTKN-worker", "Manager": "SWMTKN-manager"}
    # Must reload so the tokens reflect current state, not a stale cached inspect.
    swarm.reload.assert_called_once()


def test_get_swarm_join_tokens_tolerates_missing_tokens():
    swarm = MagicMock()
    swarm.attrs = {}  # not a swarm manager / no tokens present
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_join_tokens() == {"Worker": None, "Manager": None}


def test_swarm_update_rotates_tokens_via_flags():
    # Rotation lives on swarm_update (the docker-py swarm.update flags); fetch fresh tokens
    # afterwards with swarm_join_tokens.
    with _patch() as mock_client:
        assert swarm_update(rotate_worker_token=True) is True
    mock_client.return_value.swarm.update.assert_called_once_with(
        rotate_worker_token=True, rotate_manager_token=False, rotate_manager_unlock_key=False
    )


def test_swarm_task_list_asks_the_daemon_for_every_task():
    with _patch() as mock_client:
        mock_client.return_value.api.tasks.return_value = [{"ID": "t1"}, {"ID": "t2"}]
        assert swarm_task_list() == [{"ID": "t1"}, {"ID": "t2"}]
    # No service lookup: the whole point is the cluster-wide read `service_ps` cannot do.
    mock_client.return_value.services.get.assert_not_called()
    mock_client.return_value.api.tasks.assert_called_once_with(filters=None)


def test_swarm_task_list_forwards_filters():
    with _patch() as mock_client:
        mock_client.return_value.api.tasks.return_value = []
        assert swarm_task_list(filters={"node": "node1", "desired-state": "running"}) == []
    mock_client.return_value.api.tasks.assert_called_once_with(filters={"node": "node1", "desired-state": "running"})


def test_swarm_task_inspect_forwards_the_reference_unmodified():
    # The daemon resolves full id -> full name -> id prefix itself (moby's getTask), so a name or a
    # prefix must reach it as given rather than being parsed or expanded here.
    with _patch() as mock_client:
        mock_client.return_value.api.inspect_task.return_value = {"ID": "t1"}
        assert swarm_task_inspect("web.1.abc123") == {"ID": "t1"}
    mock_client.return_value.api.inspect_task.assert_called_once_with("web.1.abc123")
