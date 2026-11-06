import base64
import logging
from types import SimpleNamespace

import docker.errors
import pytest

from docker_mcp.redaction import harvest, settings
from docker_mcp.redaction.harvest import HostHarvest, credential_values, history_build_args
from docker_mcp.redaction.settings import NameRules, parse_names


@pytest.fixture(autouse=True)
def _default_names(monkeypatch):
    monkeypatch.setattr(settings, "_names", NameRules())


# A stand-in for the docker-py client, shaped after docker-py 7.2.0 as checked against the installed
# package: `containers.list(all=...)`, `images.list()`, `Image.history()`, `services.list()`,
# `configs.list()` / `configs.get(id)` with `.attrs`, and `api.events(since=, until=, decode=)` returning a
# closable stream of dicts.
class _Stream(list):
    closed = False

    def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, *, containers=(), images=(), services=(), configs=(), swarm=True):
        self.events: list[dict] = []
        self.event_queries: list[tuple] = []
        self.container_lists = 0
        self._containers = list(containers)
        self._images = list(images)
        self._services = list(services)
        self._configs = {c.id: c for c in configs}
        self._swarm = swarm
        self.containers = SimpleNamespace(list=self._list_containers)
        self.images = SimpleNamespace(list=lambda: list(self._images))
        self.services = SimpleNamespace(list=self._list_services)
        self.configs = SimpleNamespace(list=self._list_configs, get=lambda config_id: self._configs[config_id])
        self.api = SimpleNamespace(events=self._events)

    def _list_containers(self, all=False):
        assert all is True
        self.container_lists += 1
        return list(self._containers)

    def _swarm_only(self):
        if not self._swarm:
            raise docker.errors.APIError("This node is not a swarm manager.")

    def _list_services(self):
        self._swarm_only()
        return list(self._services)

    def _list_configs(self):
        self._swarm_only()
        return list(self._configs.values())

    def _events(self, since=None, until=None, decode=None):
        assert decode is True and since is not None and until is not None
        self.event_queries.append((since, until))
        return _Stream(self.events)


def _container(cid, env):
    return SimpleNamespace(id=cid, attrs={"Config": {"Env": env}})


def _image(iid, env, history=()):
    return SimpleNamespace(id=iid, attrs={"Config": {"Env": env}}, history=lambda: list(history))


def _harvester(client):
    remembered: list[tuple[str, str]] = []
    h = HostHarvest("prod", lambda label: client, lambda value, label: remembered.append((value, label)))
    return h, remembered


def test_credential_values_takes_only_credential_names():
    # The 3 is deliberate: a malformed environment entry is skipped rather than crashing the harvest.
    entries = ["DB_PASSWORD=pw", "PATH=/bin", "API_TOKEN=", "noequals", 3]
    assert list(credential_values(entries)) == ["pw"]  # pyright: ignore[reportArgumentType]


def test_the_names_setting_widens_and_narrows_the_harvest(monkeypatch):
    monkeypatch.setattr(settings, "_names", parse_names("UPSTREAM_*, !SORT_KEY"))
    assert list(credential_values(["UPSTREAM_URL=u", "SORT_KEY=k", "API_KEY=a"])) == ["u", "a"]


@pytest.mark.parametrize(
    ("created_by", "expected"),
    [
        ("|2 NPM_TOKEN=abc BUILD=1 /bin/sh -c npm ci", ["NPM_TOKEN=abc", "BUILD=1"]),
        ("RUN |1 SECRET_KEY=xyz /bin/sh -c make", ["SECRET_KEY=xyz"]),
        ('/bin/sh -c #(nop)  CMD ["sh"]', []),
        ("", []),
    ],
)
def test_build_arguments_are_read_from_image_history(created_by, expected):
    assert history_build_args(created_by) == expected


def test_the_first_call_harvests_every_source():
    config_data = base64.b64encode(b"s3cret-config-value").decode()
    client = _FakeClient(
        containers=[_container("c1", ["DB_PASSWORD=from-container", "HOME=/root"])],
        images=[_image("i1", ["GPG_TOKEN=from-image"], [{"CreatedBy": "|1 NPM_TOKEN=from-history /bin/sh"}])],
        services=[
            SimpleNamespace(id="s1", attrs={"Spec": {"TaskTemplate": {"ContainerSpec": {"Env": ["X_SECRET=svc"]}}}})
        ],
        configs=[
            SimpleNamespace(id="cfg1", attrs={"Spec": {"Name": "db_password", "Data": config_data}}),
            SimpleNamespace(id="cfg2", attrs={"Spec": {"Name": "nginx.conf", "Data": config_data}}),
        ],
    )
    h, remembered = _harvester(client)
    h.ensure_fresh()
    assert sorted(v for v, _ in remembered) == sorted(
        ["from-container", "from-image", "from-history", "svc", "s3cret-config-value"]
    )
    assert {label for _, label in remembered} == {"prod"}


def test_later_calls_harvest_again_only_after_a_relevant_event():
    client = _FakeClient(containers=[_container("c1", ["DB_PASSWORD=one"])])
    h, _ = _harvester(client)
    h.ensure_fresh()
    h.ensure_fresh()
    assert client.container_lists == 1 and len(client.event_queries) == 1
    client.events = [{"Type": "network", "Action": "connect"}]
    h.ensure_fresh()
    assert client.container_lists == 1
    client.events = [{"Type": "container", "Action": "create"}]
    h.ensure_fresh()
    assert client.container_lists == 2


def test_image_histories_are_read_once_per_image():
    reads = []
    image = SimpleNamespace(id="i1", attrs={"Config": {"Env": []}}, history=lambda: reads.append(1) or [])
    client = _FakeClient(images=[image])
    h, _ = _harvester(client)
    h.ensure_fresh()
    client.events = [{"Type": "image", "Action": "tag"}]
    h.ensure_fresh()
    assert len(reads) == 1


def test_a_daemon_that_is_not_a_swarm_manager_is_tolerated_and_logged_once(caplog):
    client = _FakeClient(containers=[_container("c1", ["DB_PASSWORD=x"])], swarm=False)
    h, remembered = _harvester(client)
    with caplog.at_level(logging.INFO, logger="docker_mcp.redaction"):
        h.ensure_fresh()
        client.events = [{"Type": "container"}]
        h.ensure_fresh()
    assert [v for v, _ in remembered].count("x") >= 1
    assert sum("no Swarm services or configs" in r.getMessage() for r in caplog.records) == 1


def test_an_unseen_container_triggers_a_fresh_harvest_and_a_seen_one_does_not():
    client = _FakeClient(containers=[_container("abcdef0123456789", ["DB_PASSWORD=x"])])
    h, _ = _harvester(client)
    h.ensure_fresh()
    assert h.harvest_if_unseen(["abcdef012345"]) is False  # an abbreviated ID of a known container
    assert h.harvest_if_unseen(["99999999aaaa"]) is True
    assert client.container_lists == 2


def test_the_event_stream_is_closed_after_each_check():
    client = _FakeClient()
    streams = []
    original = client._events
    client.api.events = lambda **kw: streams.append(original(**kw)) or streams[-1]
    h, _ = _harvester(client)
    h.ensure_fresh()
    h.ensure_fresh()
    assert streams and all(stream.closed for stream in streams)


def test_binary_config_content_is_left_to_the_rules():
    client = _FakeClient(
        configs=[
            SimpleNamespace(id="c", attrs={"Spec": {"Name": "tls_key", "Data": base64.b64encode(b"\xff\xfe").decode()}})
        ]
    )
    h, remembered = _harvester(client)
    h.ensure_fresh()
    assert remembered == []
    assert harvest._MAX_CONFIG_VALUE > 0


@pytest.mark.parametrize("action", ["exec_create", "exec_start", "start", "attach", "health_status: healthy", "die"])
def test_routine_container_events_do_not_cost_a_harvest(action):
    client = _FakeClient(containers=[_container("c1", ["DB_PASSWORD=one"])])
    h, _ = _harvester(client)
    h.ensure_fresh()
    client.events = [{"Type": "container", "Action": action}]
    h.ensure_fresh()
    assert client.container_lists == 1
