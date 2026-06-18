import docker_mcp._env as _env
from docker_mcp._env import env_flag, read_env


def _reset_warned(monkeypatch):
    """Give each test a fresh, isolated 'already warned' set so deprecation notices are observable."""
    monkeypatch.setattr(_env, "_warned_aliases", set())


# ---------- read_env ----------


def test_read_env_prefers_canonical(monkeypatch):
    monkeypatch.setenv("DOCKER_MCP_SERVER_THING", "new")
    monkeypatch.setenv("DOCKER_MCP_THING", "old")
    assert read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING") == "new"


def test_read_env_falls_back_to_alias(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_SERVER_THING", raising=False)
    monkeypatch.setenv("DOCKER_MCP_THING", "old")
    assert read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING") == "old"


def test_read_env_checks_aliases_in_order(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_SERVER_THING", raising=False)
    monkeypatch.delenv("DOCKER_MCP_FIRST", raising=False)
    monkeypatch.setenv("DOCKER_MCP_SECOND", "second")
    assert read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_FIRST", "DOCKER_MCP_SECOND") == "second"


def test_read_env_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_SERVER_THING", raising=False)
    monkeypatch.delenv("DOCKER_MCP_THING", raising=False)
    assert read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING", default="fallback") == "fallback"
    assert read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING") is None


def test_read_env_empty_string_is_a_value(monkeypatch):
    # An explicitly empty canonical value still wins; we don't fall through to the alias for it.
    monkeypatch.setenv("DOCKER_MCP_SERVER_THING", "")
    monkeypatch.setenv("DOCKER_MCP_THING", "old")
    assert read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING") == ""


# ---------- deprecation warning ----------


def test_alias_read_warns_once_to_stderr(monkeypatch, capsys):
    _reset_warned(monkeypatch)
    monkeypatch.delenv("DOCKER_MCP_SERVER_THING", raising=False)
    monkeypatch.setenv("DOCKER_MCP_THING", "old")
    read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING")
    read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING")
    err = capsys.readouterr().err
    assert err.count("DOCKER_MCP_THING is deprecated") == 1
    assert "DOCKER_MCP_SERVER_THING" in err


def test_canonical_read_does_not_warn(monkeypatch, capsys):
    _reset_warned(monkeypatch)
    monkeypatch.setenv("DOCKER_MCP_SERVER_THING", "new")
    read_env("DOCKER_MCP_SERVER_THING", "DOCKER_MCP_THING")
    assert capsys.readouterr().err == ""


# ---------- env_flag ----------


def test_env_flag_truthy_canonical(monkeypatch):
    for value in ["1", "true", "TRUE", "Yes", "on", "  on  "]:
        monkeypatch.setenv("DOCKER_MCP_SERVER_FLAG", value)
        assert env_flag("DOCKER_MCP_SERVER_FLAG") is True


def test_env_flag_falsy_and_unset(monkeypatch):
    for value in ["0", "false", "no", "off", "", "maybe"]:
        monkeypatch.setenv("DOCKER_MCP_SERVER_FLAG", value)
        assert env_flag("DOCKER_MCP_SERVER_FLAG") is False
    monkeypatch.delenv("DOCKER_MCP_SERVER_FLAG", raising=False)
    assert env_flag("DOCKER_MCP_SERVER_FLAG") is False


def test_env_flag_honors_alias(monkeypatch):
    monkeypatch.delenv("DOCKER_MCP_SERVER_FLAG", raising=False)
    monkeypatch.setenv("DOCKER_MCP_FLAG", "1")
    assert env_flag("DOCKER_MCP_SERVER_FLAG", "DOCKER_MCP_FLAG") is True
