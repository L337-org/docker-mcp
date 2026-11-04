# integration tests — require a real Docker daemon at $DOCKER_HOST (or the default unix socket).
# run with: uv run pytest -m integration

from contextlib import closing

import pytest
from packaging import version

from docker_mcp.tools.system import (
    _from_env_no_context,
    _summarise_df,
    system_df,
    system_info,
    system_ping,
    system_reconnect,
    system_version,
)
from docker_mcp.tools.containers import container_list
from docker_mcp.tools.images import image_list


def test_ping_real_daemon():
    assert system_ping() is True


def test_reconnect_from_env_returns_version():
    # No host → rebuild from the environment against the same daemon; validates connectivity.
    payload = system_reconnect()
    assert "Version" in payload
    assert "ApiVersion" in payload
    # The rebuilt client is live.
    assert system_ping() is True


def test_version_returns_keys():
    payload = system_version()
    assert "Version" in payload
    assert "ApiVersion" in payload


def test_info_returns_keys():
    payload = system_info()
    assert "ID" in payload
    assert "ServerVersion" in payload


def test_df_returns_totals_per_category_and_items_only_with_verbose():
    payload = system_df()
    assert set(payload) == {"Images", "Containers", "Volumes", "BuildCache"}
    for totals in payload.values():
        assert set(totals) == {"ActiveCount", "TotalCount", "TotalSize", "Reclaimable"}
    verbose = system_df(verbose=True)
    assert all(len(verbose[category]["Items"]) == payload[category]["TotalCount"] for category in payload)


def test_df_totals_derived_from_a_pre_v152_reply_agree_with_the_daemons_own():
    """The legacy conversion, run on the same daemon's pre-v1.52 reply, against the totals it reports itself.

    Pins the port of moby's client conversion to the daemon rather than to our reading of the Go source.
    Build cache `TotalSize` is left out: the legacy conversion excludes shared records and a v1.52+ daemon
    does not, so the two differ by design (see `_summarise_df`).
    """
    # Through `_from_env_no_context`, as every client in the package is, so both reach the daemon
    # `system_df` uses rather than one the active CLI context names.
    # docker-py 7.2's DockerClient has `close()` but is not a context manager.
    with closing(_from_env_no_context()) as client, closing(_from_env_no_context(version="1.51")) as legacy:
        if version.parse(client.version()["ApiVersion"]) < version.parse("1.52"):
            pytest.skip("the daemon predates API v1.52, so it reports no totals of its own to compare with")
        own = _summarise_df(client.df(), verbose=False)
        derived = _summarise_df(legacy.df(), verbose=False)
    for category in ("Images", "Containers", "Volumes"):
        assert derived[category] == own[category], category
    assert derived["BuildCache"]["Reclaimable"] == own["BuildCache"]["Reclaimable"]
    assert derived["BuildCache"]["TotalCount"] == own["BuildCache"]["TotalCount"]


def test_list_containers_returns_list():
    assert isinstance(container_list(all=True), list)


def test_list_images_returns_list():
    assert isinstance(image_list(), list)
