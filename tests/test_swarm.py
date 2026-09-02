from unittest.mock import MagicMock, patch

import pytest

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


_LIVE_SWARM_SPEC = {
    "Name": "default",
    "Labels": {"env": "prod"},
    "Orchestration": {"TaskHistoryRetentionLimit": 10},
    "Raft": {"SnapshotInterval": 10000, "KeepOldSnapshots": 0},
    "Dispatcher": {"HeartbeatPeriod": 5000000000},
    "CAConfig": {"NodeCertExpiry": 7776000000000000},
    "TaskDefaults": {"LogDriver": {"Name": "json-file"}},
    "EncryptionConfig": {"AutoLockManagers": True},
}


def _swarm_mock(spec=None):
    swarm = MagicMock()
    swarm.attrs = {"Spec": dict(_LIVE_SWARM_SPEC if spec is None else spec), "Version": {"Index": 42}}
    # Deliberately disagrees with attrs: the version sent must come from the freshly reloaded
    # inspect document, not from the `Swarm.version` property typeshed mistypes as `str | None`.
    swarm.version = "stale"
    return swarm


def test_swarm_update():
    swarm = _swarm_mock()
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_update(rotate_worker_token=True) is True
    mock_client.return_value.api.update_swarm.assert_called_once_with(
        version=42,
        swarm_spec=_LIVE_SWARM_SPEC,
        rotate_worker_token=True,
        rotate_manager_token=False,
        rotate_manager_unlock_key=False,
    )


def test_swarm_update_resubmits_the_live_spec_rather_than_resetting_it():
    # `POST /swarm/update` replaces the spec outright, so a rotation that sent only what docker-py's
    # `Swarm.update()` builds would silently clear autolock, labels, the default log driver and the
    # Raft/Orchestration/Dispatcher tuning. Every live block must come back unchanged.
    swarm = _swarm_mock()
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_update(rotate_manager_unlock_key=True) is True
    # Read fresh: a stale cached inspect would resubmit an out-of-date spec and a stale version index.
    swarm.reload.assert_called_once()
    sent = mock_client.return_value.api.update_swarm.call_args.kwargs["swarm_spec"]
    assert sent == _LIVE_SWARM_SPEC
    assert sent["EncryptionConfig"] == {"AutoLockManagers": True}
    # The high-level `swarm.update()` is exactly what must not be used here.
    swarm.update.assert_not_called()


def test_swarm_update_merges_updates_over_the_live_spec_by_top_level_key():
    swarm = _swarm_mock()
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_update(updates={"Orchestration": {"TaskHistoryRetentionLimit": 3}}) is True
    sent = mock_client.return_value.api.update_swarm.call_args.kwargs["swarm_spec"]
    # The named block is replaced whole...
    assert sent["Orchestration"] == {"TaskHistoryRetentionLimit": 3}
    # ...and every other block is carried over untouched.
    assert sent["Labels"] == {"env": "prod"}
    assert sent["TaskDefaults"] == {"LogDriver": {"Name": "json-file"}}
    assert mock_client.return_value.api.update_swarm.call_args.kwargs["rotate_worker_token"] is False


def test_swarm_update_does_not_mutate_the_cached_swarm_attrs():
    swarm = _swarm_mock()
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_update(updates={"Labels": {"env": "staging"}}) is True
    # The merge builds a new dict; the model's own attrs must not be edited underneath it.
    assert swarm.attrs["Spec"]["Labels"] == {"env": "prod"}


def test_swarm_update_tolerates_a_swarm_with_no_spec_key():
    swarm = _swarm_mock()
    swarm.attrs = {"Version": {"Index": 7}}
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        assert swarm_update(rotate_worker_token=True) is True
    assert mock_client.return_value.api.update_swarm.call_args.kwargs["swarm_spec"] == {}


def test_swarm_update_refuses_to_send_an_unguarded_update():
    # No Version.Index means no optimistic-concurrency token, so the update would race another
    # writer. Fail with something actionable rather than posting `version=None`.
    swarm = _swarm_mock()
    swarm.attrs = {"Spec": {}}
    with _patch() as mock_client:
        mock_client.return_value.swarm = swarm
        with pytest.raises(RuntimeError, match="Version.Index"):
            swarm_update(rotate_worker_token=True)
    mock_client.return_value.api.update_swarm.assert_not_called()


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
    # Rotation lives on swarm_update (the /swarm/update rotate flags); fetch fresh tokens
    # afterwards with swarm_join_tokens.
    with _patch() as mock_client:
        mock_client.return_value.swarm = _swarm_mock()
        assert swarm_update(rotate_worker_token=True) is True
    assert mock_client.return_value.api.update_swarm.call_args.kwargs["rotate_worker_token"] is True


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
