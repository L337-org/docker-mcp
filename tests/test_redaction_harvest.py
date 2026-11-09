import base64
import logging
import secrets
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
# package: `api.containers(all=, quiet=)` returning `[{"Id": ...}]`, `api.images(quiet=)` returning a list of
# IDs, `api.services()` and `api.configs()` returning the daemon's dicts, `containers.list(all=,
# ignore_removed=)`, `images.get(id)` with `.attrs` and `.history()`, `services.list()`, and
# `configs.list()` / `configs.get(id)` with `.attrs`.
class _FakeClient:
    def __init__(self, *, containers=(), images=(), services=(), configs=(), swarm=True):
        self.harvests = 0
        self.snapshots = 0
        self.containers_map = {c.id: c for c in containers}
        self.images_map = {i.id: i for i in images}
        self.services_list = list(services)
        self.configs_map = {c.id: c for c in configs}
        self.swarm = swarm
        self.removed_during_harvest: set[str] = set()
        self.containers = SimpleNamespace(list=self._list_containers)
        self.images = SimpleNamespace(get=self._get_image)
        self.services = SimpleNamespace(list=self._list_services)
        self.configs = SimpleNamespace(list=self._list_configs, get=lambda config_id: self.configs_map[config_id])
        self.api = SimpleNamespace(
            containers=self._api_containers,
            images=self._api_images,
            services=self._api_services,
            configs=self._api_configs,
        )

    def _api_containers(self, all=False, quiet=False):
        assert all is True and quiet is True
        self.snapshots += 1
        return [{"Id": cid} for cid in self.containers_map]

    def _api_images(self, quiet=False):
        assert quiet is True
        return list(self.images_map)

    def _swarm_only(self):
        if not self.swarm:
            raise docker.errors.APIError("This node is not a swarm manager.")

    def _api_services(self):
        self._swarm_only()
        return [{"ID": s.id, "Version": {"Index": s.version}} for s in self.services_list]

    def _api_configs(self):
        self._swarm_only()
        return [{"ID": cid} for cid in self.configs_map]

    def _list_containers(self, all=False, ignore_removed=False):
        assert all is True and ignore_removed is True
        self.harvests += 1
        return [c for cid, c in self.containers_map.items() if cid not in self.removed_during_harvest]

    def _get_image(self, image_id):
        if image_id in self.removed_during_harvest:
            raise docker.errors.NotFound(f"No such image: {image_id}")
        return self.images_map[image_id]

    def _list_services(self):
        self._swarm_only()
        return list(self.services_list)

    def _list_configs(self):
        self._swarm_only()
        return list(self.configs_map.values())


def _container(cid, env):
    return SimpleNamespace(id=cid, attrs={"Config": {"Env": env}})


def _image(iid, env, history=()):
    return SimpleNamespace(id=iid, attrs={"Config": {"Env": env}}, history=lambda: list(history))


def _service(sid, env, version=1):
    return SimpleNamespace(id=sid, version=version, attrs={"Spec": {"TaskTemplate": {"ContainerSpec": {"Env": env}}}})


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
        services=[_service("s1", ["X_SECRET=svc"])],
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


def test_later_calls_harvest_again_only_when_the_snapshot_changes():
    client = _FakeClient(containers=[_container("c1", ["DB_PASSWORD=one"])])
    h, remembered = _harvester(client)
    h.ensure_fresh()
    h.ensure_fresh()
    assert client.harvests == 1 and client.snapshots == 2
    client.containers_map["c2"] = _container("c2", ["DB_PASSWORD=two"])
    h.ensure_fresh()
    assert client.harvests == 2 and ("two", "prod") in remembered


def test_a_new_container_is_harvested_however_busy_the_daemon_and_whatever_its_clock():
    # Freshness used to hang on the daemon's event history, which holds only the last 256 events and is
    # filtered by the daemon's clock, so a `create` could be missed.  The snapshot depends on neither.
    client = _FakeClient(containers=[_container("c1", [])])
    h, remembered = _harvester(client)
    h.ensure_fresh()
    client.containers_map["late"] = _container("late", ["API_TOKEN=late-value"])
    h.ensure_fresh()
    assert ("late-value", "prod") in remembered


def test_a_service_updated_under_the_same_id_is_harvested_again():
    service = _service("s1", ["X_SECRET=old"])
    client = _FakeClient(services=[service])
    h, remembered = _harvester(client)
    h.ensure_fresh()
    service.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"] = ["X_SECRET=new"]
    service.version = 2
    h.ensure_fresh()
    assert ("new", "prod") in remembered


def test_image_histories_are_read_once_per_image():
    reads = []
    image = SimpleNamespace(id="i1", attrs={"Config": {"Env": []}}, history=lambda: reads.append(1) or [])
    client = _FakeClient(images=[image])
    h, _ = _harvester(client)
    h.ensure_fresh()
    client.containers_map["c1"] = _container("c1", [])
    h.ensure_fresh()
    assert client.harvests == 2 and len(reads) == 1


def test_a_daemon_that_is_not_a_swarm_manager_is_tolerated_and_logged_once(caplog):
    client = _FakeClient(containers=[_container("c1", ["DB_PASSWORD=x"])], swarm=False)
    h, remembered = _harvester(client)
    with caplog.at_level(logging.INFO, logger="docker_mcp.redaction"):
        h.ensure_fresh()
        client.containers_map["c2"] = _container("c2", [])
        h.ensure_fresh()
    assert ("x", "prod") in remembered
    assert sum("no Swarm services or configs" in r.getMessage() for r in caplog.records) == 1


def test_objects_removed_during_the_harvest_are_skipped():
    client = _FakeClient(
        containers=[_container("gone", ["DB_PASSWORD=g"]), _container("c1", ["DB_PASSWORD=kept"])],
        images=[_image("gone-image", ["API_TOKEN=gi"]), _image("i1", ["API_TOKEN=kept-image"])],
    )
    client.removed_during_harvest = {"gone", "gone-image"}
    h, remembered = _harvester(client)
    h.ensure_fresh()
    assert {v for v, _ in remembered} == {"kept", "kept-image"}


def test_a_failing_daemon_propagates_for_the_caller_to_judge():
    client = _FakeClient()

    def refuse(**kwargs):
        raise docker.errors.APIError("500 Server Error: daemon is unhappy")

    client.api.containers = refuse
    h, _ = _harvester(client)
    with pytest.raises(docker.errors.APIError, match="daemon is unhappy"):
        h.ensure_fresh()


def test_an_unseen_container_prompts_a_check_and_a_known_one_does_not():
    client = _FakeClient(containers=[_container("abcdef0123456789", ["DB_PASSWORD=x"])])
    h, _ = _harvester(client)
    h.ensure_fresh()
    assert h.harvest_if_unseen(["abcdef012345"]) is False  # an abbreviated ID of a known container
    assert client.snapshots == 1
    client.containers_map["99999999aaaa"] = _container("99999999aaaa", [])
    assert h.harvest_if_unseen(["99999999aaaa"]) is True
    assert client.harvests == 2


def test_known_ids_are_matched_in_full_and_abbreviated_among_many_without_a_check():
    # Indexed by their first 12 characters, so a list of thousands of containers is not checked by
    # scanning every known ID for every mentioned one.
    ids = [secrets.token_hex(32) for _ in range(2000)]
    client = _FakeClient(containers=[_container(i, []) for i in ids])
    h, _ = _harvester(client)
    h.ensure_fresh()
    mentioned = [*ids, *(i[:12] for i in ids[::7]), *(i[:20] for i in ids[::11])]
    assert h.harvest_if_unseen(mentioned) is False
    assert client.snapshots == 1
    # The lookup scans one bucket, not every known ID: with random IDs no 12-character prefix repeats.
    assert max(len(bucket) for bucket in h._known_by_prefix.values()) == 1


def test_an_id_that_is_no_container_costs_one_snapshot_and_never_a_harvest():
    # Network and plugin IDs, and removed containers' IDs in events, look just like container IDs.
    client = _FakeClient(containers=[_container("c1", [])])
    h, _ = _harvester(client)
    h.ensure_fresh()
    network_id = "f" * 64
    assert h.harvest_if_unseen([network_id]) is False
    assert h.harvest_if_unseen([network_id]) is False
    assert client.harvests == 1 and client.snapshots == 2


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


def test_checked_ids_are_forgotten_when_the_daemon_changes_and_never_grow_past_their_bound(monkeypatch):
    monkeypatch.setattr(harvest, "_MAX_CHECKED_IDS", 3)
    client = _FakeClient(containers=[_container("c1", [])])
    h, _ = _harvester(client)
    h.ensure_fresh()
    h.harvest_if_unseen(["a" * 64, "b" * 64])
    assert len(h._checked_ids) == 2
    h.harvest_if_unseen(["c" * 64, "d" * 64])
    assert len(h._checked_ids) == 2  # the bound cleared the earlier two first
    h.harvest_if_unseen([c * 64 for c in "56789"])
    assert len(h._checked_ids) == 3  # one result naming more than the bound still stays within it
    client.containers_map["c2"] = _container("c2", [])
    h.ensure_fresh()
    assert h._checked_ids == set()


@pytest.mark.parametrize(
    "value",
    ["/etc/ssl/certs/ca-certificates.crt", "/certs", "/run/secrets/db_password", "~/.ssh/id_ed25519", "/opt/app/"],
)
def test_an_absolute_path_under_a_credential_name_is_not_harvested(value):
    # It says where a credential is; harvested, it would be replaced in every mount and command too.
    assert list(credential_values([f"SSL_CERT_FILE={value}", f"DB_PASSWORD_FILE={value}"])) == []


@pytest.mark.parametrize("value", ["/Summer 2025", "/pa$$word!", "relative/path", "s3cr3t/with/slashes", "//x?y"])
def test_a_value_that_is_not_a_plain_absolute_path_is_still_harvested(value):
    assert list(credential_values([f"DB_PASSWORD={value}"])) == [value]
