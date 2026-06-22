import hashlib
import json
import sys
from unittest.mock import patch

import pytest

import docker_mcp._hosts as hosts
from docker_mcp._hosts import Host, HostConfigError, parse_registry


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with a clean host-related environment and an empty pinned registry."""
    for var in ("DOCKER_MCP_SERVER_HOSTS", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(hosts, "_registry", {})
    monkeypatch.setattr(hosts, "_docker_host_notice_shown", False)


@pytest.fixture
def stub_resolution(monkeypatch):
    """Make auto/local resolution deterministic (no filesystem/context dependency) for parsing tests."""
    monkeypatch.setattr(hosts, "resolve_auto", lambda: "unix:///auto.sock")
    monkeypatch.setattr(hosts, "resolve_local", lambda: "unix:///local.sock")


# --- grammar: bare single-host shorthand ---------------------------------------------------------


def test_bare_explicit_url():
    reg = parse_registry("ssh://ops@prod.example.com")
    assert reg == {"default": Host(label="default", url="ssh://ops@prod.example.com")}


def test_bare_read_only_marker():
    reg = parse_registry("ssh://ops@prod(ro)")
    assert reg["default"].read_only is True
    assert reg["default"].url == "ssh://ops@prod"


def test_bare_auto_keyword(stub_resolution):
    assert parse_registry("auto")["default"].url == "unix:///auto.sock"


def test_bare_local_keyword(stub_resolution):
    assert parse_registry("local")["default"].url == "unix:///local.sock"


def test_unset_falls_back_to_docker_host(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.5:2375")
    assert parse_registry(None)["default"].url == "tcp://10.0.0.5:2375"


def test_unset_without_docker_host_resolves_auto(stub_resolution):
    assert parse_registry("")["default"].url == "unix:///auto.sock"


# --- grammar: labeled list -----------------------------------------------------------------------


def test_labeled_multi_preserves_order_and_markers(stub_resolution):
    reg = parse_registry("local=auto, prod=ssh://ops@prod(ro)")
    assert list(reg) == ["local", "prod"]
    assert reg["local"] == Host(label="local", url="unix:///auto.sock")
    assert reg["prod"] == Host(label="prod", url="ssh://ops@prod", read_only=True)


def test_single_labeled_entry_is_not_multi(stub_resolution):
    reg = parse_registry("local=auto")
    assert list(reg) == ["local"]


def test_whitespace_is_tolerated(stub_resolution):
    reg = parse_registry("  local = auto ,  prod = ssh://h  ")
    assert list(reg) == ["local", "prod"]
    assert reg["prod"].url == "ssh://h"


# --- fail-fast parse errors ----------------------------------------------------------------------


def test_duplicate_label_fails(stub_resolution):
    with pytest.raises(HostConfigError, match="duplicate host label 'a'"):
        parse_registry("a=auto, a=local")


def test_empty_label_fails():
    with pytest.raises(HostConfigError, match="empty label"):
        parse_registry("=auto")


def test_missing_equals_in_multi_entry_fails(stub_resolution):
    with pytest.raises(HostConfigError, match="missing '='"):
        parse_registry("local=auto, justaword")


def test_invalid_label_characters_fail():
    with pytest.raises(HostConfigError, match="invalid"):
        parse_registry("pr od=auto")


def test_unknown_marker_fails():
    with pytest.raises(HostConfigError, match="unknown marker"):
        parse_registry("prod=ssh://h(rw)")


def test_unrecognized_scheme_fails():
    with pytest.raises(HostConfigError, match="unrecognized endpoint"):
        parse_registry("prod=ftp://h")


# --- TLS marker ----------------------------------------------------------------------------------


def _write_certs(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for filename in ("ca.pem", "cert.pem", "key.pem"):
        (directory / filename).write_text("x", encoding="utf-8")


def test_tls_marker_sets_cert_dir(tmp_path):
    _write_certs(tmp_path / "certs")
    reg = parse_registry(f"prod=tcp://prod:2376(tls={tmp_path / 'certs'})")
    assert reg["prod"].cert_dir == str(tmp_path / "certs")


def test_tls_markers_combine_in_any_order(tmp_path):
    certs = tmp_path / "certs"
    _write_certs(certs)
    for spec in (f"tcp://prod:2376(tls={certs})(ro)", f"tcp://prod:2376(ro)(tls={certs})"):
        reg = parse_registry(f"prod={spec}")
        assert reg["prod"].read_only is True
        assert reg["prod"].cert_dir == str(certs)


def test_tls_marker_expands_user(tmp_path, monkeypatch):
    _write_certs(tmp_path / ".certs")
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.expanduser() resolves ~ via $HOME on POSIX
    reg = parse_registry("prod=tcp://prod:2376(tls=~/.certs)")
    assert reg["prod"].cert_dir == str(tmp_path / ".certs")


def test_tls_marker_on_non_tcp_fails(tmp_path):
    _write_certs(tmp_path / "certs")
    with pytest.raises(HostConfigError, match="only valid on a tcp://"):
        parse_registry(f"prod=ssh://h(tls={tmp_path / 'certs'})")


def test_tls_marker_missing_files_fails(tmp_path):
    with pytest.raises(HostConfigError, match="missing or cannot read"):
        parse_registry(f"prod=tcp://prod:2376(tls={tmp_path / 'nonexistent'})")


def test_tls_marker_empty_dir_fails():
    with pytest.raises(HostConfigError, match=r"\(tls=\) needs a directory"):
        parse_registry("prod=tcp://prod:2376(tls=)")


# --- accessors -----------------------------------------------------------------------------------


def test_accessors(monkeypatch, stub_resolution):
    monkeypatch.setattr(hosts, "_registry", parse_registry("local=auto, prod=ssh://h(ro)"))
    assert hosts.labels() == ["local", "prod"]
    assert hosts.default().label == "local"
    assert hosts.is_multi() is True
    assert hosts.resolve("prod").read_only is True
    assert hosts.resolve(None).label == "local"  # None -> default (first entry)
    assert hosts.is_read_only("prod") is True
    assert hosts.is_read_only("local") is False
    assert hosts.registry() == hosts._registry and hosts.registry() is not hosts._registry


def test_single_host_is_not_multi(monkeypatch, stub_resolution):
    monkeypatch.setattr(hosts, "_registry", parse_registry("auto"))
    assert hosts.is_multi() is False


def test_unknown_label_raises_listing_known(monkeypatch, stub_resolution):
    monkeypatch.setattr(hosts, "_registry", parse_registry("local=auto, prod=ssh://h"))
    with pytest.raises(KeyError, match="unknown host 'staging'"):
        hosts.resolve("staging")


def test_default_is_not_a_selectable_label(monkeypatch, stub_resolution):
    # The internal "default" fallback is never a user label in multi-host mode.
    monkeypatch.setattr(hosts, "_registry", parse_registry("local=auto, prod=ssh://h"))
    assert "default" not in hosts.labels()
    with pytest.raises(KeyError):
        hosts.resolve("default")


# --- load(): scrub ordering, DOCKER_HOST-ignored notice, fail-fast ------------------------------


def test_load_scrubs_placeholder_hosts_var(monkeypatch, stub_resolution):
    # mcpb blank-field case: a literal ${...} must be scrubbed, not parsed (which would fail-fast).
    monkeypatch.setenv("DOCKER_MCP_SERVER_HOSTS", "${user_config.docker_hosts}")
    hosts.load()
    assert list(hosts._registry) == ["default"]
    assert hosts._registry["default"].url == "unix:///auto.sock"


def test_load_ignores_docker_host_and_warns_when_hosts_set(monkeypatch, stub_resolution, capsys):
    monkeypatch.setenv("DOCKER_MCP_SERVER_HOSTS", "prod=ssh://ops@prod")
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.5:2375")
    hosts.load()
    assert list(hosts._registry) == ["prod"]  # DOCKER_HOST ignored
    assert "ignoring DOCKER_HOST" in capsys.readouterr().err


def test_load_whitespace_hosts_honors_docker_host_without_warning(monkeypatch, capsys):
    # A whitespace-only HOSTS value parses as unset, so DOCKER_HOST is honored — no "ignoring" notice.
    monkeypatch.setenv("DOCKER_MCP_SERVER_HOSTS", "   ")
    monkeypatch.setenv("DOCKER_HOST", "tcp://10.0.0.5:2375")
    hosts.load()
    assert hosts._registry["default"].url == "tcp://10.0.0.5:2375"
    assert "ignoring DOCKER_HOST" not in capsys.readouterr().err


def test_load_fail_fast_exits_nonzero(monkeypatch):
    monkeypatch.setenv("DOCKER_MCP_SERVER_HOSTS", "a=auto, a=local")
    with pytest.raises(SystemExit) as exc:
        hosts.load()
    assert exc.value.code == 1


# --- auto/local resolution (relocated from test_client.py, retargeted to _hosts) -----------------


def test_active_context_name_prefers_env(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    assert hosts._active_context_name() == "colima"


def test_active_context_name_reads_current_context(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"currentContext": "desktop-linux"}), encoding="utf-8")
    assert hosts._active_context_name() == "desktop-linux"


def test_active_context_name_none_when_no_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))  # empty dir, no config.json
    assert hosts._active_context_name() is None


def test_context_host_reads_meta_json(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    name = "desktop-linux"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    meta_dir = tmp_path / "contexts" / "meta" / digest
    meta_dir.mkdir(parents=True)
    host = "unix:///Users/gavin/.docker/run/docker.sock"
    (meta_dir / "meta.json").write_text(
        json.dumps({"Name": name, "Endpoints": {"docker": {"Host": host}}}), encoding="utf-8"
    )
    assert hosts._context_host(name) == host


def test_context_host_none_when_meta_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    assert hosts._context_host("nonexistent") is None


def test_resolve_auto_follows_context():
    with (
        patch("docker_mcp._hosts._active_context_name", return_value="desktop-linux"),
        patch("docker_mcp._hosts._context_host", return_value="unix:///home/u/.docker/run/docker.sock") as ch,
        patch("docker_mcp._hosts.resolve_local") as probe,
    ):
        assert hosts.resolve_auto() == "unix:///home/u/.docker/run/docker.sock"
    ch.assert_called_once_with("desktop-linux")
    probe.assert_not_called()  # a resolved context short-circuits the probe


def test_resolve_auto_skips_default_context_and_probes():
    with (
        patch("docker_mcp._hosts._active_context_name", return_value="default"),
        patch("docker_mcp._hosts._context_host") as ch,
        patch("docker_mcp._hosts.resolve_local", return_value="unix:///var/run/docker.sock"),
    ):
        assert hosts.resolve_auto() == "unix:///var/run/docker.sock"
    ch.assert_not_called()  # the "default" context means the classic socket — no meta.json to read


def test_resolve_auto_probes_when_context_has_no_host():
    with (
        patch("docker_mcp._hosts._active_context_name", return_value="broken"),
        patch("docker_mcp._hosts._context_host", return_value=None),
        patch("docker_mcp._hosts.resolve_local", return_value="unix:///run/user/1000/docker.sock"),
    ):
        assert hosts.resolve_auto() == "unix:///run/user/1000/docker.sock"


@pytest.mark.skipif(sys.platform == "win32", reason="socket probe is POSIX-only; Windows uses npipe via from_env()")
def test_resolve_local_returns_first_existing(monkeypatch):
    # Only /var/run/docker.sock "exists"; the home-dir candidates ahead of it do not.
    monkeypatch.setattr(hosts.Path, "is_socket", lambda self: str(self) == "/var/run/docker.sock")
    assert hosts.resolve_local() == "unix:///var/run/docker.sock"


def test_resolve_local_none_on_windows(monkeypatch):
    monkeypatch.setattr(hosts.sys, "platform", "win32")
    assert hosts.resolve_local() is None


def test_resolve_local_none_when_nothing_exists(monkeypatch):
    monkeypatch.setattr(hosts.Path, "is_socket", lambda _self: False)
    assert hosts.resolve_local() is None
