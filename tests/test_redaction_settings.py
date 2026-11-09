import re
from pathlib import Path

import pytest

import docker_mcp._hosts as hosts
from docker_mcp._hosts import parse_registry
from docker_mcp.exceptions import RedactionConfigError
from docker_mcp.redaction import settings
from docker_mcp.redaction.settings import NameRules, parse_names


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Each test starts with redaction off, no names, and two unmarked hosts."""
    for var in (settings.REDACT_VAR, settings.NAMES_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(settings, "_global", False)
    monkeypatch.setattr(settings, "_names", NameRules())
    monkeypatch.setattr(settings, "_names_set", False)
    monkeypatch.setattr(hosts, "_registry", parse_registry("dev=ssh://dev, prod=ssh://prod"))


def _hosts(monkeypatch, value):
    monkeypatch.setattr(hosts, "_registry", parse_registry(value))


# --- the names setting ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", " , ,"])
def test_an_unset_or_empty_names_setting_adds_and_excludes_nothing(raw):
    assert parse_names(raw) == NameRules()


def test_names_are_split_into_additions_and_exclusions():
    rules = parse_names(" UPSTREAM_*, *_REF ,!SORT_KEY, ! GPG_KEY ")
    assert rules == NameRules(added=("UPSTREAM_*", "*_REF"), excluded=("SORT_KEY", "GPG_KEY"))


def test_a_name_value_pair_is_refused_without_repeating_the_value():
    with pytest.raises(RedactionConfigError) as caught:
        parse_names("DB_USER, DB_PASS=hunter2-not-real")
    message = str(caught.value)
    assert "entry 2 contains '='" in message
    assert "names only, never values" in message
    assert "hunter2" not in message and "DB_PASS" not in message


def test_an_entry_that_is_not_a_name_is_refused_by_position_only():
    with pytest.raises(RedactionConfigError) as caught:
        parse_names("A, B, c0rrect horse battery")
    assert "entry 3 is not a variable name or glob" in str(caught.value)
    assert "horse" not in str(caught.value)


def test_a_bare_exclamation_mark_is_refused():
    with pytest.raises(RedactionConfigError, match=r"entry 1 is a bare '!'"):
        parse_names("!, A")


@pytest.mark.parametrize(
    "name",
    [
        "DB_PASSWORD",
        "db_password",
        "MYSQL_ROOT_PASSWD",
        "DB_PASS",
        "DB_PASS_FILE",
        "SSH_PASSPHRASE",
        "CLIENT_SECRET",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "API_KEY",
        "TLS_CERT",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ],
)
def test_the_built_in_patterns_harvest_credential_names(name):
    assert NameRules().matches(name)


@pytest.mark.parametrize("name", ["PATH", "HOME", "PASSTHROUGH", "BYPASS_CACHE", "COMPASS_URL", "PWD", "LANG"])
def test_the_built_in_patterns_leave_ordinary_names_alone(name):
    assert not NameRules().matches(name)


@pytest.mark.parametrize(
    "name", ["SSH_PUBLIC_KEY", "JWT_PUBLIC_KEY", "DEPLOY_PUBKEY", "SIGNING_PUB_KEY", "GPG_KEY", "gpg_keys"]
)
def test_public_keys_are_left_alone_by_default(name):
    assert not NameRules().matches(name)


@pytest.mark.parametrize("name", ["GPG_KEY_PASSPHRASE", "GPG_PRIVATE_KEY", "SSH_PRIVATE_KEY", "PUBLISH_KEY"])
def test_the_public_key_exclusions_leave_private_material_harvested(name):
    assert NameRules().matches(name)


def test_an_addition_overrides_a_built_in_exclusion_and_a_users_exclusion_still_wins():
    assert parse_names("GPG_KEY").matches("GPG_KEY")
    assert not parse_names("GPG_KEY, !GPG_KEY").matches("GPG_KEY")


def test_an_addition_extends_the_built_in_patterns_case_insensitively():
    rules = parse_names("upstream_*")
    assert rules.matches("UPSTREAM_URL")
    assert rules.matches("DB_PASSWORD")


def test_an_exclusion_wins_over_a_built_in_pattern():
    rules = parse_names("!sort_key")
    assert not rules.matches("SORT_KEY")
    assert rules.matches("API_KEY")


def test_an_exclusion_wins_over_an_addition():
    assert not parse_names("BUILD_*, !BUILD_ID").matches("BUILD_ID")


def test_the_resolved_name_list_is_reported_in_full():
    assert parse_names("UPSTREAM_*, !SORT_KEY").report() == {
        "builtin": list(settings.BUILTIN_NAME_PATTERNS),
        "builtin_excluded": list(settings.BUILTIN_EXCLUDED_PATTERNS),
        "added": ["UPSTREAM_*"],
        "excluded": ["SORT_KEY"],
    }


# --- load ----------------------------------------------------------------------------------------


def test_load_pins_the_switch_and_the_names(monkeypatch):
    monkeypatch.setenv(settings.REDACT_VAR, "1")
    monkeypatch.setenv(settings.NAMES_VAR, "UPSTREAM_*")
    settings.load()
    assert settings.enabled_globally() is True
    assert settings.names().added == ("UPSTREAM_*",)


@pytest.mark.parametrize("value", ["0", "", "no", "off"])
def test_the_switch_is_off_unless_set_truthy(monkeypatch, value):
    monkeypatch.setenv(settings.REDACT_VAR, value)
    settings.load()
    assert settings.enabled_globally() is False


def test_load_exits_on_a_malformed_names_setting_without_printing_the_value(monkeypatch, capsys):
    monkeypatch.setenv(settings.NAMES_VAR, "TOKEN=abc123-not-real")
    with pytest.raises(SystemExit) as caught:
        settings.load()
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith(f"docker-mcp-server: invalid {settings.NAMES_VAR} - ")
    assert "abc123" not in err


# --- which hosts are redacted --------------------------------------------------------------------


def test_nothing_is_redacted_by_default():
    assert settings.redacted_hosts() == []
    assert settings.any_redacted() is False
    assert settings.host_redacted("prod") is False


def test_the_global_switch_redacts_every_host(monkeypatch):
    monkeypatch.setattr(settings, "_global", True)
    assert settings.redacted_hosts() == ["dev", "prod"]
    assert settings.host_redacted() is True


def test_the_host_option_redacts_only_that_host(monkeypatch):
    _hosts(monkeypatch, "dev=ssh://dev, prod=ssh://prod(ro,redact)")
    assert settings.redacted_hosts() == ["prod"]
    assert settings.host_redacted("prod") is True
    assert settings.host_redacted("dev") is False
    assert settings.host_redacted() is False  # the default is the first entry, dev


def test_the_report_carries_the_switch_the_names_and_the_redacted_hosts(monkeypatch):
    _hosts(monkeypatch, "dev=ssh://dev, prod=ssh://prod(redact)")
    monkeypatch.setattr(settings, "_names", parse_names("!SORT_KEY"))
    report = settings.report()
    assert report[settings.REDACT_VAR] is False
    assert report[settings.NAMES_VAR]["excluded"] == ["SORT_KEY"]
    assert report["redacted_hosts"] == ["prod"]


# --- startup warnings ----------------------------------------------------------------------------


def test_no_warning_when_nothing_is_configured():
    assert settings.startup_warnings(readonly=False) == []


def test_the_global_switch_without_readonly_warns_for_each_host_that_is_not_ro(monkeypatch):
    _hosts(monkeypatch, "dev=ssh://dev, prod=ssh://prod(ro), stage=ssh://stage")
    monkeypatch.setattr(settings, "_global", True)
    warnings = settings.startup_warnings(readonly=False)
    assert [w.split(" is redacted")[0] for w in warnings] == ["host 'dev'", "host 'stage'"]
    assert all("Mark it (ro,redact) or set DOCKER_MCP_SERVER_READONLY=1" in w for w in warnings)
    assert all("REDACTION.md" in w for w in warnings)


def test_the_global_switch_does_not_warn_when_every_host_is_ro(monkeypatch):
    # Exec and the file-writing tools are writes, so per-host ro removes them as READONLY does.
    _hosts(monkeypatch, "dev=ssh://dev(ro), prod=ssh://prod(ro)")
    monkeypatch.setattr(settings, "_global", True)
    assert settings.startup_warnings(readonly=False) == []


def test_the_global_switch_with_readonly_does_not_warn(monkeypatch):
    monkeypatch.setattr(settings, "_global", True)
    assert settings.startup_warnings(readonly=True) == []


def test_each_host_marked_redact_without_ro_warns_by_name(monkeypatch):
    _hosts(monkeypatch, "a=ssh://a(redact), b=ssh://b(ro,redact), c=ssh://c(redact)(nd), d=ssh://d")
    warnings = settings.startup_warnings(readonly=False)
    assert [w.split(" is redacted")[0] for w in warnings] == ["host 'a'", "host 'c'"]


def test_readonly_covers_a_host_marked_redact_without_ro(monkeypatch):
    _hosts(monkeypatch, "a=ssh://a(redact)")
    assert settings.startup_warnings(readonly=True) == []


def test_names_without_any_redaction_warn_that_they_do_nothing(monkeypatch):
    monkeypatch.setattr(settings, "_names", parse_names("UPSTREAM_*"))
    monkeypatch.setattr(settings, "_names_set", True)
    assert settings.startup_warnings(readonly=True) == [
        f"{settings.NAMES_VAR} is set but redaction is not on for any host, so it does nothing. Set "
        f"{settings.REDACT_VAR}=1 or mark a host redact in DOCKER_MCP_SERVER_HOSTS."
    ]


def test_names_with_a_redacted_host_do_not_warn(monkeypatch):
    _hosts(monkeypatch, "a=ssh://a(ro,redact)")
    monkeypatch.setattr(settings, "_names_set", True)
    assert settings.startup_warnings(readonly=False) == []


# --- documentation -------------------------------------------------------------------------------


def test_redaction_md_lists_exactly_the_built_in_patterns():
    text = (Path(__file__).parent.parent / "REDACTION.md").read_text(encoding="utf-8")
    section = text.split("## Which names are treated as credentials", 1)[1]
    pattern_list = section.split("globs:", 1)[1].split("Some names those patterns match", 1)[0]
    assert re.findall(r"`([^`]+)`", pattern_list) == list(settings.BUILTIN_NAME_PATTERNS)


def test_redaction_md_lists_exactly_the_built_in_exclusions():
    text = (Path(__file__).parent.parent / "REDACTION.md").read_text(encoding="utf-8")
    section = text.split("## Which names are treated as credentials", 1)[1]
    exclusions = section.split("under `GPG_KEY`:", 1)[1].split("A value that is an absolute path", 1)[0]
    assert re.findall(r"`([^`]+)`", exclusions) == list(settings.BUILTIN_EXCLUDED_PATTERNS)
