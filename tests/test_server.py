import ast
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import docker.errors
import httpx
import pytest

import docker_mcp  # noqa: F401 — imported for its side effect of registering every tool
import docker_mcp._hosts as _hosts
from docker_mcp._hosts import parse_registry
from docker_mcp.exceptions import HostGuardError
from docker_mcp.server import (
    TOOL_CATEGORIES,
    TRANSLATES_FAILURES,
    _NO_DOMAIN_TOOLS,
    _SCHEMA_NAME_MAPS,
    ToolCategory,
    _annotations_for,
    _apply_host_schema,
    build_instructions,
    _domain_enabled,
    _domain_for,
    _enforce_host_guard,
    _host_guard_needed,
    _parse_domains,
    query_catalog,
    _prompt_registry,
    _seen_tool_names,
    _should_register,
    _slim_schema,
    _title_for,
    _tool_registry,
    _wrap_with_host_guard,
    mcp,
    tool_catalog,
)


def _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(ro)"):
    """Pin a deterministic 2-host registry so the host machinery sees multi-host mode."""
    monkeypatch.setattr(_hosts, "resolve_auto", lambda: "unix:///auto.sock")
    monkeypatch.setattr(_hosts, "resolve_local", lambda: "unix:///local.sock")
    monkeypatch.setattr(_hosts, "_registry", parse_registry(spec))


def _set_single_host(monkeypatch, spec="ssh://h(ro)"):
    """Pin a single-host registry so the host machinery sees single-host mode (default (ro) by default)."""
    monkeypatch.setattr(_hosts, "resolve_auto", lambda: "unix:///auto.sock")
    monkeypatch.setattr(_hosts, "resolve_local", lambda: "unix:///local.sock")
    monkeypatch.setattr(_hosts, "_registry", parse_registry(spec))


def _host_schema(*, default=True, required=("name",)):
    """A freshly-slimmed schema for a tool with `host: str | None = None` plus a required `name`."""
    host: dict[str, object] = {"type": "string"}
    if default:
        host["default"] = None
    return {"type": "object", "properties": {"host": host, "name": {"type": "string"}}, "required": list(required)}


# ---------- host param: schema surgery ----------


def test_apply_host_schema_strips_host_in_single_host_mode(monkeypatch):
    monkeypatch.setattr(_hosts, "resolve_auto", lambda: "unix:///auto.sock")
    monkeypatch.setattr(_hosts, "_registry", parse_registry(None))  # one synthesized host
    schema = _host_schema()
    _apply_host_schema(schema, "container_list", ToolCategory.READ_ONLY)
    assert "host" not in schema["properties"]


def test_apply_host_schema_strips_host_from_required(monkeypatch):
    monkeypatch.setattr(_hosts, "resolve_auto", lambda: "unix:///auto.sock")
    monkeypatch.setattr(_hosts, "_registry", parse_registry(None))
    schema = {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]}
    _apply_host_schema(schema, "container_remove", ToolCategory.DESTRUCTIVE)
    assert "host" not in schema["properties"]
    assert "required" not in schema  # emptied -> dropped


def test_apply_host_schema_injects_enum_for_read_only(monkeypatch):
    _set_multi_host(monkeypatch)
    schema = _host_schema()
    _apply_host_schema(schema, "container_list", ToolCategory.READ_ONLY)
    assert schema["properties"]["host"]["enum"] == ["local", "prod"]
    assert "host" not in schema.get("required", [])  # optional for reads
    assert schema["properties"]["host"]["default"] is None  # retained
    assert "local" in schema["properties"]["host"]["description"]  # names the default


def test_apply_host_schema_requires_host_for_writes(monkeypatch):
    _set_multi_host(monkeypatch)
    schema = _host_schema()
    _apply_host_schema(schema, "container_remove", ToolCategory.DESTRUCTIVE)
    assert schema["properties"]["host"]["enum"] == ["local", "prod"]
    assert "host" in schema["required"]
    assert "default" not in schema["properties"]["host"]  # required field carries no default


def test_apply_host_schema_connection_control_stays_optional(monkeypatch):
    _set_multi_host(monkeypatch)
    schema = _host_schema()
    _apply_host_schema(schema, "system_close", ToolCategory.MUTATING)  # MUTATING but connection-control
    assert schema["properties"]["host"]["enum"] == ["local", "prod"]
    assert "host" not in schema.get("required", [])


def test_apply_host_schema_noop_without_host_property(monkeypatch):
    _set_multi_host(monkeypatch)
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    _apply_host_schema(schema, "host_list", ToolCategory.READ_ONLY)
    assert schema == {"type": "object", "properties": {"name": {"type": "string"}}}


def test_apply_host_schema_unaffected_by_non_destructive_marker(monkeypatch):
    # (nd) changes call-time enforcement only — the advertised schema is identical to a plain host.
    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(nd)")
    schema = _host_schema()
    _apply_host_schema(schema, "container_remove", ToolCategory.DESTRUCTIVE)
    assert schema["properties"]["host"]["enum"] == ["local", "prod"]
    assert "host" in schema["required"]


# ---------- host param: call-time guard ----------


def test_guard_allows_read_only_without_host(monkeypatch):
    _set_multi_host(monkeypatch)
    _enforce_host_guard("container_list", ToolCategory.READ_ONLY, None)  # no raise


def test_guard_requires_host_for_write(monkeypatch):
    _set_multi_host(monkeypatch)
    with pytest.raises(HostGuardError, match="'host' is required"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, None)


def test_guard_rejects_unknown_host(monkeypatch):
    _set_multi_host(monkeypatch)
    with pytest.raises(HostGuardError, match="unknown host 'staging'"):
        _enforce_host_guard("container_list", ToolCategory.READ_ONLY, "staging")


def test_guard_rejects_write_to_read_only_host(monkeypatch):
    _set_multi_host(monkeypatch)  # prod is (ro)
    with pytest.raises(HostGuardError, match="read-only"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, "prod")


def test_guard_allows_connection_control_on_read_only_host(monkeypatch):
    _set_multi_host(monkeypatch)
    _enforce_host_guard("system_close", ToolCategory.MUTATING, "prod")  # conn-control exempt from ro-refusal


def test_guard_checks_unknown_host_even_for_connection_control(monkeypatch):
    _set_multi_host(monkeypatch)
    with pytest.raises(HostGuardError, match="unknown host"):
        _enforce_host_guard("system_close", ToolCategory.MUTATING, "typo")


def test_guard_allows_mutating_write_to_non_destructive_host(monkeypatch):
    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(nd)")
    _enforce_host_guard("container_start", ToolCategory.MUTATING, "prod")  # no raise


def test_guard_rejects_destructive_write_to_non_destructive_host(monkeypatch):
    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(nd)")
    with pytest.raises(HostGuardError, match="non-destructive"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, "prod")


def test_guard_allows_connection_control_on_non_destructive_host(monkeypatch):
    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(nd)")
    _enforce_host_guard("system_close", ToolCategory.MUTATING, "prod")  # conn-control always exempt


def test_guard_rejects_destructive_write_to_read_only_and_non_destructive_host(monkeypatch):
    # A host with both markers is refused by (ro) first — it's strictly stronger.
    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(ro)(nd)")
    with pytest.raises(HostGuardError, match="read-only"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, "prod")


# ---------- host param: single-host (ro) enforcement ----------


def test_guard_refuses_write_to_single_read_only_host(monkeypatch):
    # One (ro) host: the schema carries no host param to pass, but writes must still be refused.
    _set_single_host(monkeypatch, "ssh://h(ro)")
    with pytest.raises(HostGuardError, match="read-only"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, None)


def test_guard_allows_read_on_single_read_only_host(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(ro)")
    _enforce_host_guard("container_list", ToolCategory.READ_ONLY, None)  # no raise


def test_guard_allows_connection_control_on_single_read_only_host(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(ro)")
    _enforce_host_guard("system_close", ToolCategory.MUTATING, None)  # conn-control exempt from ro-refusal


def test_guard_allows_write_on_single_writable_host(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h")  # no (ro) marker
    _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, None)  # no raise


def test_guard_refuses_destructive_write_to_single_non_destructive_host(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(nd)")
    with pytest.raises(HostGuardError, match="non-destructive"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, None)


def test_guard_allows_mutating_write_on_single_non_destructive_host(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(nd)")
    _enforce_host_guard("container_start", ToolCategory.MUTATING, None)  # no raise


def test_guard_allows_read_on_single_non_destructive_host(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(nd)")
    _enforce_host_guard("container_list", ToolCategory.READ_ONLY, None)  # no raise


def test_host_guard_needed_matrix(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(ro)")
    assert _host_guard_needed() is True  # single (ro): wrap to refuse writes
    monkeypatch.setattr(_hosts, "_registry", parse_registry("ssh://h(nd)"))
    assert _host_guard_needed() is True  # single (nd): wrap to refuse destructive calls
    monkeypatch.setattr(_hosts, "_registry", parse_registry("ssh://h"))
    assert _host_guard_needed() is False  # single unrestricted: footprint-neutral, no wrap
    monkeypatch.setattr(_hosts, "_registry", parse_registry("a=ssh://x, b=ssh://y"))
    assert _host_guard_needed() is True  # multi-host


# ---------- host param: wrapper ----------


def test_wrap_preserves_signature_and_name_and_enforces_guard(monkeypatch):
    _set_multi_host(monkeypatch)

    def container_remove(container_id: str, host: str | None = None) -> str:
        return f"removed {container_id} on {host}"

    wrapped = _wrap_with_host_guard(container_remove, "container_remove", ToolCategory.DESTRUCTIVE)
    assert wrapped.__name__ == "container_remove"
    assert inspect.signature(wrapped) == inspect.signature(container_remove)
    with pytest.raises(HostGuardError, match="'host' is required"):
        wrapped(container_id="abc")  # write without host
    assert wrapped(container_id="abc", host="local") == "removed abc on local"


def test_wrap_guards_an_async_tool(monkeypatch):
    # No registered tool is async today, but FastMCP supports async tools, so the wrapper has an async
    # branch — exercise it directly so the guard provably fires on a coroutine function too.
    import asyncio

    _set_multi_host(monkeypatch)

    async def container_remove(container_id: str, host: str | None = None) -> str:
        return f"removed {container_id} on {host}"

    wrapped = _wrap_with_host_guard(container_remove, "container_remove", ToolCategory.DESTRUCTIVE)
    assert inspect.iscoroutinefunction(wrapped)
    assert inspect.signature(wrapped) == inspect.signature(container_remove)
    with pytest.raises(HostGuardError, match="'host' is required"):
        asyncio.run(wrapped(container_id="abc"))  # write without host
    assert asyncio.run(wrapped(container_id="abc", host="local")) == "removed abc on local"


def test_wrap_enforces_non_destructive_guard(monkeypatch):
    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(nd)")

    def container_remove(container_id: str, host: str | None = None) -> str:
        return f"removed {container_id} on {host}"

    wrapped = _wrap_with_host_guard(container_remove, "container_remove", ToolCategory.DESTRUCTIVE)
    with pytest.raises(HostGuardError, match="non-destructive"):
        wrapped(container_id="abc", host="prod")
    assert wrapped(container_id="abc", host="local") == "removed abc on local"


# ---------- host_list ----------


def test_list_hosts_is_read_only_with_no_host_param():
    from docker_mcp.tools.system import host_list

    assert TOOL_CATEGORIES["host_list"] is ToolCategory.READ_ONLY
    assert "host" not in inspect.signature(host_list).parameters


def test_list_hosts_reports_registry(monkeypatch):
    from docker_mcp.tools.system import host_list

    _set_multi_host(monkeypatch)
    rows = host_list()
    assert [r["name"] for r in rows] == ["local", "prod"]
    assert rows[0]["default"] is True and rows[1]["default"] is False
    assert rows[1]["read_only"] is True


def test_list_hosts_reports_non_destructive(monkeypatch):
    from docker_mcp.tools.system import host_list

    _set_multi_host(monkeypatch, spec="local=auto, prod=ssh://h(nd)")
    rows = host_list()
    assert rows[1]["non_destructive"] is True
    assert rows[0]["non_destructive"] is False


def _registered_tools() -> dict:
    return mcp._tool_manager._tools


def _registered_prompts() -> dict:
    return mcp._prompt_manager._prompts


# ---------- classification stays in sync with the registered tools ----------


def test_every_registered_tool_is_classified():
    # Decorating a tool records its name in _seen_tool_names regardless of registration, so this
    # catches both a new tool missing from TOOL_CATEGORIES and a stale entry for a removed tool.
    assert _seen_tool_names == set(TOOL_CATEGORIES)


def test_all_classified_tools_are_registered_by_default():
    # With no env switches set (the test environment), every classified tool is actually registered.
    assert set(_registered_tools()) == set(TOOL_CATEGORIES)


# ---------- annotations ----------


def test_registered_tools_carry_annotations_matching_their_category():
    for name, registered in _registered_tools().items():
        ann = registered.annotations
        assert ann is not None, f"{name} has no ToolAnnotations"
        category = TOOL_CATEGORIES[name]
        assert ann.read_only_hint is (category is ToolCategory.READ_ONLY), name
        assert ann.destructive_hint is (category is ToolCategory.DESTRUCTIVE), name


def test_registered_tools_carry_a_title_distinct_from_their_name():
    # Directory review (e.g. the Claude Connectors Directory) reads `title` mechanically, independent
    # of description quality — a tool missing one fails review however good its docstring is.
    for name, registered in _registered_tools().items():
        ann = registered.annotations
        assert ann is not None and ann.title, f"{name} has no title annotation"
        assert ann.title != name, f"{name}'s title is just its own name, not a human-readable label"


def test_title_for_title_cases_and_despaces_the_name():
    assert _title_for("container_list") == "Container List"
    assert _title_for("buildx_imagetools_create") == "Buildx Imagetools Create"


def test_title_for_fixes_known_acronyms():
    # A naive .title() gets these wrong ("Scout Cves"/"Scout Sbom") since they're not real words.
    assert _title_for("scout_cves") == "Scout CVEs"
    assert _title_for("scout_sbom") == "Scout SBOM"


def test_annotations_for_read_only():
    ann = _annotations_for("container_list", ToolCategory.READ_ONLY)
    assert ann.title == "Container List"
    assert ann.read_only_hint is True
    assert ann.destructive_hint is False


def test_annotations_for_mutating():
    ann = _annotations_for("container_run", ToolCategory.MUTATING)
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False


def test_annotations_for_destructive_prune_is_idempotent():
    ann = _annotations_for("image_prune", ToolCategory.DESTRUCTIVE)
    assert ann.read_only_hint is False
    assert ann.destructive_hint is True
    assert ann.idempotent_hint is True


def test_annotations_for_destructive_non_prune_not_marked_idempotent():
    ann = _annotations_for("container_remove", ToolCategory.DESTRUCTIVE)
    assert ann.destructive_hint is True
    assert ann.idempotent_hint is None


# ---------- env-switch logic ----------


def test_should_register_default_registers_everything():
    for category in ToolCategory:
        assert _should_register(category, readonly=False, no_destructive=False) is True


def test_should_register_readonly_keeps_only_read_only():
    assert _should_register(ToolCategory.READ_ONLY, readonly=True, no_destructive=False) is True
    assert _should_register(ToolCategory.MUTATING, readonly=True, no_destructive=False) is False
    assert _should_register(ToolCategory.DESTRUCTIVE, readonly=True, no_destructive=False) is False


def test_should_register_no_destructive_drops_only_destructive():
    assert _should_register(ToolCategory.READ_ONLY, readonly=False, no_destructive=True) is True
    assert _should_register(ToolCategory.MUTATING, readonly=False, no_destructive=True) is True
    assert _should_register(ToolCategory.DESTRUCTIVE, readonly=False, no_destructive=True) is False


def test_should_register_readonly_wins_when_both_set():
    # READONLY is the stricter switch, so a mutating tool is dropped even though NO_DESTRUCTIVE alone
    # would keep it.
    assert _should_register(ToolCategory.MUTATING, readonly=True, no_destructive=True) is False


# ---------- domain switch (DOCKER_MCP_SERVER_DISABLE) ----------


def test_parse_domains_splits_normalizes_and_drops_blanks():
    assert _parse_domains("swarm, Plugins ,, SCOUT") == frozenset({"swarm", "plugins", "scout"})
    assert _parse_domains(None) == frozenset()
    assert _parse_domains("") == frozenset()


def test_domain_for_derives_module_leaf():
    assert _domain_for(test_domain_for_derives_module_leaf) == "test_server"


def test_domain_enabled_respects_disabled_set():
    assert _domain_enabled("compose", frozenset()) is True
    assert _domain_enabled("swarm", frozenset({"swarm", "plugins"})) is False
    assert _domain_enabled("compose", frozenset({"swarm"})) is True


def test_every_registered_tool_has_a_domain():
    # The registry records a domain for every tool, derived from its defining module — except the
    # handful of intentionally domain-less _NO_DOMAIN_TOOLS (e.g. docs_lookup), which never gate on
    # DOCKER_MCP_SERVER_DISABLE at all.
    assert set(_tool_registry) == set(TOOL_CATEGORIES)
    assert all(rec.domain or rec.name in _NO_DOMAIN_TOOLS for rec in _tool_registry.values())
    # Sanity-check a couple of known module -> domain mappings.
    assert _tool_registry["container_list"].domain == "containers"
    assert _tool_registry["compose_up"].domain == "compose"


def test_no_domain_tools_have_no_domain_and_always_register():
    for name in _NO_DOMAIN_TOOLS:
        assert _tool_registry[name].domain is None
        assert _tool_registry[name].registered is True


# ---------- tool catalog ----------


def test_tool_catalog_lists_every_tool_with_taxonomy():
    catalog = tool_catalog()
    names = {t["name"] for t in catalog["tools"]}
    assert names == set(TOOL_CATEGORIES)
    for entry in catalog["tools"]:
        assert entry["category"] == TOOL_CATEGORIES[entry["name"]].value
        assert entry["domain"] or entry["name"] in _NO_DOMAIN_TOOLS
        # No switches set in the test environment, so every tool registered.
        assert entry["registered"] is True


def test_tool_catalog_reports_switch_state_and_domain_counts():
    catalog = tool_catalog()
    assert set(catalog["switches"]) == {
        "DOCKER_MCP_SERVER_READONLY",
        "DOCKER_MCP_SERVER_NO_DESTRUCTIVE",
        "DOCKER_MCP_SERVER_DISABLE",
    }
    # Per-domain counts sum to the full tool surface, and by default registered == total.
    assert sum(d["total"] for d in catalog["domains"]) == len(TOOL_CATEGORIES)
    assert all(d["registered"] == d["total"] for d in catalog["domains"])
    assert catalog["unknown_disabled_domains"] == []


# ---------- catalog queries (query_catalog) ----------


def test_query_catalog_filters_by_domain():
    result = query_catalog(domain="scout")
    assert result["matched"] == len(result["tools"]) > 0
    assert {row["domain"] for row in result["tools"]} == {"scout"}
    assert result["filters"]["domain"] == "scout"


def test_query_catalog_filters_by_category():
    result = query_catalog(category="destructive")
    assert result["matched"] > 0
    assert {row["category"] for row in result["tools"]} == {"destructive"}
    # Cross-checked against the central map, so this cannot pass by echoing its own filter back.
    expected = {name for name, cat in TOOL_CATEGORIES.items() if cat is ToolCategory.DESTRUCTIVE}
    assert {row["name"] for row in result["tools"]} == expected


def test_query_catalog_keyword_matches_a_tool_name():
    assert "container_prune" in {row["name"] for row in query_catalog(keyword="prune")["tools"]}


def test_query_catalog_keyword_matches_a_parameter_name():
    # `host` is a parameter on most tools and appears in no tool name, so a match proves the
    # parameter names are searched rather than the name alone.
    matched = {row["name"] for row in query_catalog(keyword="host")["tools"]}
    assert "container_list" in matched
    assert "host" not in "container_list"


def test_query_catalog_keyword_matches_summary_text():
    # "vulnerabilit" appears in one summary (scout_cves) and in no tool name or parameter name, so a
    # match can only have come from the summary. Anchored on a term with that property rather than
    # one that also occurs in a name, or the assertion would pass without the summary being searched.
    assert not any("vulnerabilit" in row["name"] for row in query_catalog()["tools"])
    matched = {row["name"] for row in query_catalog(keyword="vulnerabilit")["tools"]}
    assert matched == {"scout_cves"}


def test_query_catalog_keyword_is_case_insensitive():
    assert query_catalog(keyword="PRUNE")["matched"] == query_catalog(keyword="prune")["matched"] > 0


def test_query_catalog_combines_filters():
    result = query_catalog(domain="containers", category="destructive")
    assert result["matched"] > 0
    assert all(r["domain"] == "containers" and r["category"] == "destructive" for r in result["tools"])


def test_query_catalog_domain_keys_are_all_usable_as_filters():
    # Every advertised domain has to be a value `domain=` accepts, returning exactly that count.
    # Domain-less tools are counted separately rather than under "", which would look selectable and
    # match nothing, costing a caller a wasted call to find out.
    catalog = query_catalog()
    assert "" not in catalog["domains"]
    for name, count in catalog["domains"].items():
        assert query_catalog(domain=name)["matched"] == count, f"domain {name!r} is not a usable filter"
    assert catalog["no_domain"] == len(_NO_DOMAIN_TOOLS)
    assert catalog["matched"] == sum(catalog["domains"].values()) + catalog["no_domain"]


def test_query_catalog_returns_an_explicit_empty_result_rather_than_raising():
    # A definitive negative is the point: "nothing matches" has to be distinguishable from a failure,
    # which is what a client's fuzzy description search can never assert.
    result = query_catalog(keyword="zzz-no-such-capability")
    assert result["matched"] == 0
    assert result["tools"] == []
    # Still tells the caller what does exist, so an empty result is recoverable without a second guess.
    assert result["domains"]


def test_query_catalog_rows_carry_a_one_line_summary_not_the_full_description():
    rows = query_catalog(domain="volumes")["tools"]
    assert rows
    for row in rows:
        assert row["summary"], f"{row['name']} has no summary"
        assert "\n" not in row["summary"]
        # The docstring's first line, not the whole thing: a full definition would be far longer.
        assert len(row["summary"]) < 200


def test_query_catalog_summary_matches_the_docstring_first_line():
    summary = {row["name"]: row["summary"] for row in query_catalog(domain="volumes")["tools"]}["volume_create"]
    from docker_mcp.tools.volumes import volume_create

    assert summary == (volume_create.__doc__ or "").strip().splitlines()[0].strip()


# ---------- end-to-end registration under the env switches (separate processes) ----------


def _registered_names(env_vars: list[str]) -> set[str]:
    """Import the package in a child process with the given env assignments; return the tool names."""
    code = "import docker_mcp; from docker_mcp.server import mcp; print('\\n'.join(mcp._tool_manager._tools))"
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env_with(env_vars),
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _env_with(assignments: list[str]) -> dict:
    import os

    env = dict(os.environ)
    # Clear every switch first so the parent environment can't leak into the child.
    for switch in ("READONLY", "NO_DESTRUCTIVE", "DISABLE"):
        env.pop(f"DOCKER_MCP_SERVER_{switch}", None)
    for assignment in assignments:
        key, _, value = assignment.partition("=")
        env[key] = value
    return env


def _catalog_in_child(env_vars: list[str]) -> dict:
    """Run query_catalog() in a child process under the given env switches."""
    code = "import json, docker_mcp; from docker_mcp.server import query_catalog; print(json.dumps(query_catalog()))"
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env_with(env_vars),
        check=True,
    )
    return json.loads(result.stdout)


def test_catalog_never_lists_a_tool_the_configuration_dropped():
    # The catalog reports what registered, so a disabled domain's tools are absent rather than
    # listed-and-flagged. Advertising a capability the server will refuse leaks its existence.
    catalog = _catalog_in_child(["DOCKER_MCP_SERVER_DISABLE=scout,swarm"])
    assert "scout" not in catalog["domains"] and "swarm" not in catalog["domains"]
    assert not [row for row in catalog["tools"] if row["domain"] in {"scout", "swarm"}]
    # But the configuration stays auditable in aggregate: the counts say how many are hidden.
    assert catalog["hidden_by_configuration"]["scout"] > 0
    assert catalog["hidden_by_configuration"]["swarm"] > 0


def test_catalog_excludes_tools_dropped_by_the_read_only_switch():
    catalog = _catalog_in_child(["DOCKER_MCP_SERVER_READONLY=1"])
    assert {row["category"] for row in catalog["tools"]} == {"read_only"}
    assert catalog["switches"]["DOCKER_MCP_SERVER_READONLY"] is True


def test_tool_list_survives_every_domain_being_disabled():
    # It is in _NO_DOMAIN_TOOLS precisely so a client can still ask what is left; a catalog that
    # disappears exactly when the surface is most reduced would be useless.
    every_domain = ",".join(sorted({r.domain for r in _tool_registry.values() if r.domain}))
    names = _registered_names([f"DOCKER_MCP_SERVER_DISABLE={every_domain}"])
    assert "tool_list" in names
    assert names == set(_NO_DOMAIN_TOOLS)


def _names_by_category(*categories: ToolCategory) -> set[str]:
    return {name for name, c in TOOL_CATEGORIES.items() if c in categories}


def test_readonly_env_registers_exactly_the_read_only_tools():
    # Exact set comparison, not a count: registering the right number of wrong tools must fail.
    assert _registered_names(["DOCKER_MCP_SERVER_READONLY=1"]) == _names_by_category(ToolCategory.READ_ONLY)


def test_no_destructive_env_registers_exactly_the_non_destructive_tools():
    expected = _names_by_category(ToolCategory.READ_ONLY, ToolCategory.MUTATING)
    assert _registered_names(["DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1"]) == expected


def test_default_env_registers_all_tools():
    assert _registered_names([]) == set(TOOL_CATEGORIES)


def test_both_switches_set_readonly_wins_end_to_end():
    # The precedence rule (_should_register unit-tests it) must hold through real registration too.
    names = _registered_names(["DOCKER_MCP_SERVER_READONLY=1", "DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1"])
    assert names == _names_by_category(ToolCategory.READ_ONLY)


def test_truthy_spelling_accepted_end_to_end():
    # The switches accept "true"/"yes"/"on" spellings, not just "1".
    assert _registered_names(["DOCKER_MCP_SERVER_READONLY=true"]) == _names_by_category(ToolCategory.READ_ONLY)


def _names_by_domain(*domains: str) -> set[str]:
    wanted = set(domains)
    return {rec.name for rec in _tool_registry.values() if rec.domain in wanted}


def test_disable_env_drops_whole_domains_end_to_end():
    # Disabling swarm + plugins removes exactly those domains' tools and nothing else.
    dropped = _names_by_domain("swarm", "plugins")
    assert dropped, "fixture sanity: expected swarm/plugins tools to exist"
    names = _registered_names(["DOCKER_MCP_SERVER_DISABLE=swarm,plugins"])
    assert names == set(TOOL_CATEGORIES) - dropped


def test_disable_env_cannot_drop_a_no_domain_tool_end_to_end():
    # docs_lookup has no domain at all — not even an (obviously wrong) attempt to disable it by
    # its own name, or every real domain at once, removes it.
    assert "docs_lookup" in _registered_names(["DOCKER_MCP_SERVER_DISABLE=docs"])
    all_domains = ",".join(sorted({rec.domain for rec in _tool_registry.values() if rec.domain is not None}))
    assert "docs_lookup" in _registered_names([f"DOCKER_MCP_SERVER_DISABLE={all_domains}"])


def test_disable_env_normalizes_whitespace_and_case_end_to_end():
    names = _registered_names(["DOCKER_MCP_SERVER_DISABLE= Compose , SCOUT "])
    assert names == set(TOOL_CATEGORIES) - _names_by_domain("compose", "scout")


def test_disable_env_combines_with_readonly_end_to_end():
    # The domain switch and the category switch stack: read-only AND not in a disabled domain.
    names = _registered_names(["DOCKER_MCP_SERVER_READONLY=1", "DOCKER_MCP_SERVER_DISABLE=registry"])
    expected = _names_by_category(ToolCategory.READ_ONLY) - _names_by_domain("registry")
    assert names == expected


def test_unknown_disabled_domain_is_a_no_op_end_to_end():
    # A typo'd domain disables nothing (and is surfaced via the catalog's unknown_disabled_domains).
    assert _registered_names(["DOCKER_MCP_SERVER_DISABLE=swrm"]) == set(TOOL_CATEGORIES)


# ---------- instructions router stays in sync with the registered surface ----------


def test_instructions_emit_a_line_only_for_present_domains():
    text = build_instructions(registered_domains={"containers", "images"})
    assert "- containers -" in text
    assert "- images -" in text
    # A domain that didn't register must not be advertised — the whole point of building it dynamically.
    assert "- swarm -" not in text
    assert "- compose -" not in text


def test_instructions_drop_cli_and_swarm_caveats_when_those_domains_are_absent():
    # No CLI-backed or swarm domains present -> neither caveat should appear, and the CLI caveat must not
    # name a domain that isn't registered.
    text = build_instructions(registered_domains={"containers", "networks"})
    assert "CLI-backed domains" not in text
    assert "swarm manager node" not in text
    # The CLI caveat lists only the CLI domains that survived.
    text = build_instructions(registered_domains={"compose", "buildx"})
    assert "CLI-backed domains (compose, buildx)" in text
    assert "scout" not in text


def test_instructions_mention_the_remote_exec_fallback_only_for_domains_that_have_it():
    """
    The fallback changes which host a call runs on - its credentials, its filesystem - so it belongs in
    the always-in-context router. `context` is the one CLI-backed domain without it (its tools manage
    *this* host's context registry), so a surface of only `context` must not advertise it.
    """
    text = build_instructions(registered_domains={"compose", "context"})
    assert "Applies to compose;" in text
    several = build_instructions(registered_domains={"compose", "buildx", "context"})
    assert "Applies to compose, buildx;" in several
    # The condition is both halves — the CLI/plugin missing *and* an ssh:// target — because a client
    # reading only the router would otherwise expect every ssh:// call to execute remotely.
    assert "With the CLI or a required plugin missing locally" in text
    # The blanket "those calls raise" is gone where a fallback exists — it contradicted the sentence
    # after it — and the domain without one is named instead.
    assert "those calls raise" not in text
    assert "no fallback for context, which raises instead." in text
    assert "reached over `ssh://`" in text
    assert "a usable local CLI always wins" in text
    assert "Applies to context" not in text  # named only where the fallback exists

    context_only = build_instructions(registered_domains={"context", "containers"})
    assert "CLI-backed domains (context)" in context_only
    assert "ssh://" not in context_only


def _calls_function(source: str, name: str) -> bool:
    """
    Whether `source` contains a real call to `name`, by AST rather than substring.

    A substring search would count a mention in a docstring or comment - and modules legitimately
    document helpers they do not call - so the scan below looks for actual `Call` nodes, matching both
    the bare `name(...)` and the `module.name(...)` attribute form.

    args:
        source - Python source text
        name - the function name to look for
    returns: bool - True if the source calls it
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                return True
    return False


def _tool_modules_calling(name: str) -> set[str]:
    """Leaf tool-module names whose source actually calls `name` (private `_*` helpers excluded)."""
    import pkgutil

    import docker_mcp.tools as tools_package

    root = Path(tools_package.__path__[0])
    found = set()
    for module in pkgutil.iter_modules(tools_package.__path__):
        path = root / f"{module.name}.py"
        # Skip subpackages and anything without a plain module file; `_cli.py` and friends define the
        # helpers rather than consuming them and are not domains.
        if module.ispkg or not path.is_file() or module.name.startswith("_"):
            continue
        if _calls_function(path.read_text(encoding="utf-8"), name):
            found.add(module.name)
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("should_remote_exec(host, plugin='compose')", True),
        ("_cli.should_remote_exec(host)", True),
        ('"""Docs mentioning should_remote_exec(host) without calling it."""', False),
        ("# see should_remote_exec(host) in _cli.py" + chr(10) + "x = 1", False),
        ("x = should_remote_exec", False),  # referenced, not called
    ],
)
def test_the_call_scan_distinguishes_calls_from_mentions(source, expected):
    """The guard below is only as good as this scan: a docstring mention must not count as wiring."""
    assert _calls_function(source, "should_remote_exec") is expected


def test_remote_exec_domains_match_the_modules_that_implement_the_fallback():
    """
    `_REMOTE_EXEC_DOMAINS` drives what the router advertises, and it is hand-maintained - so a domain
    that wires the fallback without being added here would run remotely while the router still promised
    a hard failure. Derived from the modules that actually call `should_remote_exec`, so the tuple cannot
    drift from the code either way.
    """
    from docker_mcp.server import _REMOTE_EXEC_DOMAINS

    implementing = _tool_modules_calling("should_remote_exec")
    assert implementing == set(_REMOTE_EXEC_DOMAINS), (
        f"modules calling should_remote_exec: {sorted(implementing)}; "
        f"_REMOTE_EXEC_DOMAINS: {sorted(_REMOTE_EXEC_DOMAINS)} - the router advertises the latter"
    )


def test_instructions_default_to_the_live_registered_surface():
    # No argument -> reads _tool_registry; with everything registered, every domain blurb appears.
    # _NO_DOMAIN_TOOLS (domain=None) are excluded — they never get a per-domain router line.
    text = build_instructions()
    present = {rec.domain for rec in _tool_registry.values() if rec.registered and rec.domain is not None}
    for domain in present:
        assert f"- {domain} -" in text


def _live_instructions(env_vars: list[str]) -> str:
    """Import the package in a child process with the given env and return the server's `instructions`
    string the client would actually receive (built by finalize_instructions() at import)."""
    code = "import docker_mcp; print(docker_mcp.mcp.instructions)"
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env_with(env_vars),
        check=True,
    )
    return result.stdout


def _router_domain_lines(instructions: str) -> set[str]:
    """The domains listed in the router's 'Domains' block. Scoped to that block so the caveat bullets -
    which also start with '- ' and contain an em-dash (e.g. the `*_to_file` line) - aren't mistaken for
    domain lines."""
    lines = instructions.splitlines()
    start = lines.index("Domains (and the words that find them):") + 1
    domains = set()
    for line in lines[start:]:
        if not line.strip():
            break
        domains.add(line[2:].split(" - ", 1)[0])
    return domains


def test_live_instructions_exclude_a_disabled_domain_end_to_end():
    # finalize_instructions() runs at package import, so a disabled domain must be gone from the string
    # the client actually receives — not just from the registered tool set.
    text = _live_instructions(["DOCKER_MCP_SERVER_DISABLE=swarm,services,nodes,secrets,configs"])
    assert "- swarm -" not in text
    assert "- services -" not in text
    assert "Swarm-family tools require" not in text
    assert "- containers -" in text  # untouched domains survive


def test_router_domain_lines_track_registered_domains_under_every_switch():
    # The invariant that makes the router safe for lazy-loading clients: it advertises a domain iff that
    # domain actually has a registered tool — under READONLY and NO_DESTRUCTIVE too, not just DISABLE.
    # (No domain loses *all* its tools to the category switches today — every domain keeps a read-only and
    # a non-destructive tool — so this proves the router doesn't wrongly drop a still-present domain.)
    for env in (
        [],
        ["DOCKER_MCP_SERVER_READONLY=1"],
        ["DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1"],
        ["DOCKER_MCP_SERVER_DISABLE=swarm,scout"],
        ["DOCKER_MCP_SERVER_READONLY=1", "DOCKER_MCP_SERVER_DISABLE=registry"],
    ):
        # _NO_DOMAIN_TOOLS (domain=None) are excluded — they never get a per-domain router line.
        expected = {_tool_registry[name].domain for name in _registered_names(env)} - {None}
        assert _router_domain_lines(_live_instructions(env)) == expected, env


# ---------- typed parameter schemas ----------


def test_run_container_restart_policy_schema_is_typed():
    # The RestartPolicy TypedDict must surface as a structured schema (enum'd Name field),
    # not an opaque dict, so the agent knows the valid keys/values without guessing.
    schema = _registered_tools()["container_run"].parameters
    assert "RestartPolicy" in schema.get("$defs", {})
    rp = schema["$defs"]["RestartPolicy"]["properties"]
    assert set(rp) == {"Name", "MaximumRetryCount"}
    assert set(rp["Name"]["enum"]) == {"no", "always", "on-failure", "unless-stopped"}


def _key_anywhere(node, target: str, *, match=lambda v: True) -> bool:
    # Mirror _slim_schema's traversal: a schema keyword used as a *property name* inside a name-map
    # (e.g. a param literally named "title"/"anyOf") is a name, not an annotation, so don't count it.
    if isinstance(node, dict):
        if target in node and match(node[target]):
            return True
        for key, value in node.items():
            if key in _SCHEMA_NAME_MAPS and isinstance(value, dict):
                if any(_key_anywhere(sub, target, match=match) for sub in value.values()):
                    return True
            elif _key_anywhere(value, target, match=match):
                return True
        return False
    if isinstance(node, list):
        return any(_key_anywhere(item, target, match=match) for item in node)
    return False


# Parameters whose legal values are a genuinely closed set, each verified against a primary source
# (the subcommand's own `--help`, or docker-py's documented value list) rather than inferred. Keyed
# tool -> param -> expected enum. Guards against a Literal being loosened back to a bare `str`,
# which would silently restore the failure modes below.
_EXPECTED_ENUMS = {
    "compose_config": {"format": ["yaml", "json"]},
    "compose_up": {"pull": ["always", "missing", "never"]},
    "network_create": {"scope": ["local", "global", "swarm"]},
    "stack_deploy": {"resolve_image": ["always", "changed", "never"]},
    "scout_cves": {
        "format": ["packages", "sarif", "spdx", "gitlab", "markdown", "sbom"],
        "only_severity": ["critical", "high", "medium", "low", "unspecified"],
    },
    "scout_compare": {
        "format": ["json", "markdown", "text"],
        "only_severity": ["critical", "high", "medium", "low", "unspecified"],
    },
    "scout_sbom": {"format": ["list", "json", "spdx", "cyclonedx"]},
}


def test_closed_value_sets_are_advertised_as_enums():
    # Two of these convert a *silent wrong answer* into a validation error, which is why they are
    # asserted rather than left to the docstring: `docker scout cves --only-severity CRITICAL`
    # exits 0 reporting "No vulnerable packages detected" on an image whose lowercase `critical`
    # run reports three, and the daemon records an unrecognised `--scope` verbatim on the network.
    tools = _registered_tools()
    for tool_name, params in _EXPECTED_ENUMS.items():
        assert tool_name in tools, f"{tool_name} is not registered"
        properties = tools[tool_name].parameters["properties"]
        for param, expected in params.items():
            assert param in properties, f"{tool_name}.{param} missing from the advertised schema"
            schema = properties[param]
            # A list-valued param carries its enum on `items`; a scalar carries it directly.
            found = schema.get("enum") or schema.get("items", {}).get("enum")
            assert found is not None, f"{tool_name}.{param} advertises no enum, expected {expected!r}"
            # Compared as a sorted multiset: order carries no meaning in JSON Schema, so pinning
            # pydantic's emission order would assert an implementation detail. Sorting still catches
            # a missing, extra or duplicated value, which is the whole promise here.
            assert sorted(found) == sorted(expected), (
                f"{tool_name}.{param} advertises enum {found!r}, expected {expected!r}"
            )


def test_pyright_still_checks_arguments_at_tool_call_sites(tmp_path):
    # The @tool() decorator used to be typed `Callable[[Callable], Callable]`. A bare `Callable`
    # carries no parameter list, so pyright -- a required CI gate that covers `tests` as well as
    # `docker_mcp` -- silently checked nothing about how any of the 159 tools were called, and
    # these three deliberate errors all passed it.
    #
    # This asserts the property rather than the mechanism: a structural check (that the decorator
    # is still generic) would keep passing if someone reannotated the return type while leaving
    # the type parameter in place, which is exactly the regression worth catching. Costs about a
    # second, because pyright reuses the project's own configuration and environment.
    pyright = shutil.which("pyright")
    if pyright is None:
        pytest.skip("pyright is not installed; it is a dev dependency, so the CI gate always has it")
    # One deliberate error per line, so each can be asserted independently. Keep the line numbers
    # in `expected` below in step with this source.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from docker_mcp.tools.stack import stack_deploy\n"
        'stack_deploy("web", compose_files=["c.yml"], resolve_image="sometimes")\n'
        'stack_deploy(123, compose_files=["c.yml"])\n'
        'stack_deploy("web", compose_files=["c.yml"], no_such_kwarg=True)\n'
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, resolved binary, no shell; trusted test input
        [pyright, "--outputjson", str(probe)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=300,
    )
    rules_by_line: dict[int, set[str]] = {}
    for diagnostic in json.loads(result.stdout)["generalDiagnostics"]:
        rules_by_line.setdefault(diagnostic["range"]["start"]["line"] + 1, set()).add(diagnostic.get("rule", ""))
    # Keyed on line and rule id rather than message text: pyright's prose is human-readable output
    # and can be reworded between versions, whereas the rule a diagnostic carries and the line it
    # lands on are stable. Per-line also proves each error is caught individually, where matching
    # substrings across the pooled output could let one diagnostic satisfy two assertions.
    expected = {
        2: ("reportArgumentType", "a value outside a Literal's set"),
        3: ("reportArgumentType", "an int for a str parameter"),
        4: ("reportCallIssue", "an unknown keyword argument"),
    }
    for line, (rule, what) in expected.items():
        assert rule in rules_by_line.get(line, set()), (
            f"{what} went unreported on line {line}: pyright is no longer checking arguments at tool "
            f"call sites. Rules reported per line: {rules_by_line}"
        )


def test_no_registered_tool_schema_carries_title_annotations():
    # pydantic stamps an information-free `title` on every property/$def and the top-level schema;
    # _slim_schema drops them (~10% of the advertised tool surface). Assert none survive.
    offenders = [name for name, t in _registered_tools().items() if _key_anywhere(t.parameters, "title")]
    assert not offenders, f"tools still advertising `title` annotations: {offenders}"


def test_no_registered_tool_schema_carries_nullable_anyof_or_redundant_additional_properties():
    # _slim_schema drops the `{"type": "null"}` branch of nullable anyOf and the default-valued
    # `additionalProperties: true`. Assert neither pattern survives on any registered tool.
    null_offenders = [
        name
        for name, t in _registered_tools().items()
        if _key_anywhere(t.parameters, "anyOf", match=lambda v: {"type": "null"} in v)
    ]
    assert not null_offenders, f"tools still advertising nullable anyOf: {null_offenders}"
    ap_offenders = [
        name
        for name, t in _registered_tools().items()
        if _key_anywhere(t.parameters, "additionalProperties", match=lambda v: v is True)
    ]
    assert not ap_offenders, f"tools still advertising `additionalProperties: true`: {ap_offenders}"


def test_slim_schema_preserves_a_param_named_title():
    # Defensive: a parameter (or $def) literally named "title" is a name, not an annotation —
    # its schema's own title is dropped, but the property key itself is preserved.
    schema = {
        "title": "DropMe",
        "type": "object",
        "properties": {
            "title": {"title": "Drop This Too", "type": "string"},
            "count": {"title": "Count", "type": "integer"},
        },
    }
    _slim_schema(schema)
    assert "title" not in schema  # top-level annotation gone
    assert set(schema["properties"]) == {"title", "count"}  # the param NAMED title survives
    assert "title" not in schema["properties"]["title"]  # its own annotation is gone
    assert schema["properties"]["title"]["type"] == "string"  # type preserved


def test_slim_schema_collapses_nullable_anyof_hoisting_the_non_null_branch():
    schema = {
        "type": "object",
        "properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "tags": {
                "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}],
                "default": None,
            },
        },
    }
    _slim_schema(schema)
    name = schema["properties"]["name"]
    assert "anyOf" not in name and name["type"] == "string" and name["default"] is None
    tags = schema["properties"]["tags"]
    # Hoisted object branch keeps its type; its redundant additionalProperties:true is dropped too.
    assert "anyOf" not in tags and tags["type"] == "object" and "additionalProperties" not in tags


def test_slim_schema_keeps_multi_branch_anyof_minus_null():
    # int | str | None -> drop only the null branch, keep the two real branches.
    schema = {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}], "default": None}
    _slim_schema(schema)
    assert schema["anyOf"] == [{"type": "integer"}, {"type": "string"}]


def test_slim_schema_keeps_nullable_anyof_without_a_default():
    # The default-gate: a nullable union with no `default` is left intact (could be a required
    # nullable field, where dropping null would misrepresent it as non-nullable).
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    _slim_schema(schema)
    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_slim_schema_keeps_schema_valued_additional_properties():
    # Only `additionalProperties: true` is redundant; a schema value (dict[str, str]) is meaningful.
    schema = {"type": "object", "additionalProperties": {"type": "string"}}
    _slim_schema(schema)
    assert schema["additionalProperties"] == {"type": "string"}


def test_slim_schema_preserves_enum_including_through_a_nullable_collapse():
    # The slim exists to delete information-free noise, and an enum is the opposite of that: it is
    # the only thing in the advertised schema that stops an out-of-set value. A `Literal[...] | None`
    # param arrives as a nullable anyOf, so the enum has to survive the hoist as well as the walk.
    schema = {
        "type": "object",
        "properties": {
            "plain": {"enum": ["yaml", "json"], "type": "string", "default": "yaml"},
            "nullable": {
                "anyOf": [{"enum": ["always", "missing", "never"], "type": "string"}, {"type": "null"}],
                "default": None,
            },
            "in_items": {"items": {"enum": ["critical", "high"], "type": "string"}, "type": "array"},
        },
    }
    _slim_schema(schema)
    props = schema["properties"]
    assert props["plain"]["enum"] == ["yaml", "json"]
    assert "anyOf" not in props["nullable"] and props["nullable"]["enum"] == ["always", "missing", "never"]
    assert props["in_items"]["items"]["enum"] == ["critical", "high"]


# ---------- prompt + doc-resource disabling (DOCKER_MCP_SERVER_DISABLE covers more than tools) ----------


def test_every_prompt_recorded_in_registry():
    # Every registered prompt has a record; by default (no switches) all of them register, except the
    # multi-host-gated prompts (e.g. survey_hosts), which are hidden in the single-host test environment.
    registered = set(_registered_prompts())
    assert registered, "fixture sanity: expected prompts to exist"
    assert registered <= set(_prompt_registry)
    assert all(r.registered for r in _prompt_registry.values() if not r.multi_host)
    assert any(r.multi_host and not r.registered for r in _prompt_registry.values())  # the gate works


def test_scout_prompts_are_tagged_scout():
    scout = {name for name, r in _prompt_registry.items() if r.domain == "scout"}
    assert {"audit_image_cves", "compare_image_versions", "recommend_base_image"} <= scout


def test_general_prompts_have_no_domain():
    # Cross-domain / advisory prompts are domain=None so they always register.
    assert _prompt_registry["lookup_docker_docs"].domain is None
    assert _prompt_registry["investigate_disk_usage"].domain is None


def test_tool_catalog_includes_prompts_and_doc_sections():
    catalog = tool_catalog()
    assert {p["name"] for p in catalog["prompts"]} == set(_prompt_registry)
    # No switches in this process, so everything registers except the multi-host-gated prompts.
    assert all(p["registered"] for p in catalog["prompts"] if not p["multi_host"])
    assert catalog["disabled_doc_sections"] == []


def _registered_prompt_names(env_vars: list[str]) -> set[str]:
    code = "import docker_mcp; from docker_mcp.server import mcp; print('\\n'.join(mcp._prompt_manager._prompts))"
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env_with(env_vars),
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _all_prompt_names() -> set[str]:
    return set(_prompt_registry)


def _prompt_names_by_domain(*domains: str) -> set[str]:
    wanted = set(domains)
    return {name for name, r in _prompt_registry.items() if r.domain in wanted}


def test_disable_env_drops_matching_prompts_end_to_end():
    # Disabling scout removes exactly the scout prompts and leaves every other prompt registered —
    # except the multi-host-gated prompts, which stay hidden in this single-host subprocess.
    scout_prompts = _prompt_names_by_domain("scout")
    assert scout_prompts, "fixture sanity: expected scout prompts to exist"
    multi_host_prompts = {name for name, r in _prompt_registry.items() if r.multi_host}
    names = _registered_prompt_names(["DOCKER_MCP_SERVER_DISABLE=scout"])
    assert names == _all_prompt_names() - scout_prompts - multi_host_prompts


def test_disable_env_keeps_general_prompts_end_to_end():
    # General (domain=None) prompts survive even when several domains are disabled.
    names = _registered_prompt_names(["DOCKER_MCP_SERVER_DISABLE=scout,buildx,compose,swarm"])
    assert "lookup_docker_docs" in names
    assert "investigate_disk_usage" in names


def test_disable_env_reports_hidden_doc_sections_in_catalog_end_to_end():
    code = (
        "import json, docker_mcp; from docker_mcp.server import tool_catalog; "
        "print(json.dumps(tool_catalog()['disabled_doc_sections']))"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env_with(["DOCKER_MCP_SERVER_DISABLE=scout"]),
        check=True,
    )
    import json

    assert json.loads(result.stdout) == ["scout", "scout-cli"]


# ---------- slice 4: host threaded through tools; end-to-end schema + routing ----------


def _tool_schema_in(env_vars: list[str], tool_name: str) -> dict:
    """Import the package in a child process with the given env; return one tool's advertised schema."""
    code = (
        "import json, docker_mcp; from docker_mcp.server import mcp; "
        f"print(json.dumps(mcp._tool_manager.get_tool({tool_name!r}).parameters))"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code], capture_output=True, text=True, env=_env_with(env_vars), check=True
    )
    return json.loads(result.stdout)


def test_multi_host_injects_host_enum_into_tool_schemas_end_to_end():
    env = ["DOCKER_MCP_SERVER_HOSTS=local=ssh://a, prod=ssh://b(ro)"]
    read = _tool_schema_in(env, "container_list")  # READ_ONLY: enum + optional
    assert read["properties"]["host"]["enum"] == ["local", "prod"]
    assert "host" not in read.get("required", [])
    dest = _tool_schema_in(env, "container_remove")  # DESTRUCTIVE: enum + required
    assert dest["properties"]["host"]["enum"] == ["local", "prod"]
    assert "host" in dest["required"]
    reg = _tool_schema_in(env, "registry_tags")  # daemon-agnostic: no host param
    assert "host" not in reg.get("properties", {})


def test_single_host_strips_host_from_tool_schemas_end_to_end():
    assert "host" not in _tool_schema_in([], "container_list").get("properties", {})


def test_single_read_only_host_still_strips_host_param_end_to_end():
    # A single (ro) host is footprint-neutral: no host param surfaces (there's only one daemon to choose),
    # the (ro) marker is enforced by the call-time guard, not the schema.
    schema = _tool_schema_in(["DOCKER_MCP_SERVER_HOSTS=ssh://h(ro)"], "container_remove")
    assert "host" not in schema.get("properties", {})


def test_single_non_destructive_host_still_strips_host_param_end_to_end():
    schema = _tool_schema_in(["DOCKER_MCP_SERVER_HOSTS=ssh://h(nd)"], "container_remove")
    assert "host" not in schema.get("properties", {})


def test_single_read_only_host_refuses_write_end_to_end():
    # Proves the guard is actually wrapped onto write tools at import time for a single (ro) host.
    code = (
        "from docker_mcp.exceptions import HostGuardError\n"
        "from docker_mcp.tools import containers\n"
        "try:\n"
        "    containers.container_stop('x')\n"
        "    print('NOGUARD')\n"
        "except HostGuardError as e:\n"
        "    print('REFUSED' if 'read-only' in str(e) else 'OTHER')\n"
    )
    env = _env_with(["DOCKER_MCP_SERVER_HOSTS=ssh://h(ro)"])
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout
    assert "REFUSED" in out


def test_single_non_destructive_host_refuses_destructive_write_end_to_end():
    # Proves the guard is actually wrapped onto destructive tools at import time for a single (nd) host.
    code = (
        "from docker_mcp.exceptions import HostGuardError\n"
        "from docker_mcp.tools import containers\n"
        "try:\n"
        "    containers.container_remove('x')\n"
        "    print('NOGUARD')\n"
        "except HostGuardError as e:\n"
        "    print('REFUSED' if 'non-destructive' in str(e) else 'OTHER')\n"
    )
    env = _env_with(["DOCKER_MCP_SERVER_HOSTS=ssh://h(nd)"])
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout
    assert "REFUSED" in out


def test_multi_host_router_caveat_present_end_to_end():
    code = "import docker_mcp; print(docker_mcp.mcp.instructions)"
    env = _env_with(["DOCKER_MCP_SERVER_HOSTS=local=ssh://a, prod=ssh://b(ro)"])
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout
    assert "Multiple hosts are configured" in out
    assert "['local', 'prod']" in out


def test_multi_host_router_caveat_mentions_non_destructive_end_to_end():
    code = "import docker_mcp; print(docker_mcp.mcp.instructions)"
    env = _env_with(["DOCKER_MCP_SERVER_HOSTS=local=ssh://a, prod=ssh://b(nd)"])
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout
    assert "(nd)" in out


def test_sdk_tool_threads_host_to_get_client(monkeypatch):
    from docker_mcp.tools import containers

    captured = {}

    def fake_get_client(host=None):
        captured["host"] = host
        client = MagicMock()
        client.containers.list.return_value = []
        return client

    monkeypatch.setattr(containers, "_get_client", fake_get_client)
    containers.container_list(host="prod")
    assert captured["host"] == "prod"


def test_cli_tool_threads_host_to_run_docker(monkeypatch):
    from docker_mcp.tools import stack
    from docker_mcp.tools._cli import CliResult

    captured = {}

    def fake_run_docker(args, **kwargs):
        captured.update(kwargs)
        return CliResult(returncode=0, stdout="", stderr="", truncated=False)

    # A *non-ssh* second host on purpose: `_run_stack` now asks `should_remote_exec` first, and only a
    # non-ssh transport makes that answer False regardless of whether this machine has a docker binary.
    _set_multi_host(monkeypatch, spec="local=auto, prod=tcp://prod:2376")
    monkeypatch.setattr(stack, "run_docker", fake_run_docker)
    stack.stack_list(host="prod")
    assert captured.get("host") == "prod"


# ---------- what a failure says on the wire ----------
#
# Everything else in this file calls a tool function directly, which asserts a message no client
# ever sees: the SDK decides what reaches the caller from the exception's *type*, and only
# ToolError/ResourceError keep their text. These go through `call_tool`, the path a client uses.


def _call_tool_in_child(hosts: str, tool: str, arguments: dict) -> str:
    """Drive `call_tool` in a child process with `hosts` configured.

    The host guard is wired onto tools at import time from the environment, so an in-process
    monkeypatch cannot reach an already-registered tool - the same reason the two end-to-end guard
    tests below shell out. Prints `<exception class>|<message>` for the caller to assert on.
    """
    code = (
        "import anyio, json\n"
        "from docker_mcp.server import mcp\n"
        "import docker_mcp\n"
        "try:\n"
        f"    anyio.run(mcp.call_tool, {tool!r}, {arguments!r})\n"
        "    print('NORAISE|')\n"
        "except Exception as e:\n"
        "    print(f'{type(e).__name__}|{e}')\n"
    )
    env = _env_with([f"DOCKER_MCP_SERVER_HOSTS={hosts}"])
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout.strip()


def test_a_host_guard_refusal_reaches_the_caller_with_its_reason():
    """A refusal the model cannot read is one it will retry against the same host.

    Asserts the type as well as the text: an `UnexpectedToolError` is logged at ERROR with a
    traceback and its message withheld, a plain `ToolError` at INFO with the message kept. Matching
    on the message alone would still pass if the SDK began reporting crashes verbosely, and the log
    level would silently be wrong.
    """
    kind, _, message = _call_tool_in_child(
        "local=auto, prod=ssh://h(ro)", "container_stop", {"id_or_name": "x", "host": "prod"}
    ).partition("|")
    assert kind == "ToolError", f"refusal arrived as {kind}: {message}"
    assert "read-only" in message and "prod" in message


def test_an_unknown_host_names_the_configured_hosts_on_the_wire():
    kind, _, message = _call_tool_in_child("local=auto, prod=ssh://h", "container_list", {"host": "staging"}).partition(
        "|"
    )
    assert kind == "ToolError", f"refusal arrived as {kind}: {message}"
    assert "unknown host 'staging'" in message


def test_a_bug_stays_a_crash_with_its_text_withheld(monkeypatch, on_the_wire):
    """The other half of the contract, and why the translation names DockerMcpError and not Exception.

    A bug dressed as a deliberate refusal is neither logged with its traceback nor kept off the
    wire, which is exactly what the SDK's classification exists to guarantee.

    `monkeypatch.setattr` is deliberately left at `raising=True`: an earlier version of this test
    patched `_client_for`, a name `containers.py` does not have, with `raising=False`. That planted
    nothing, and the test passed only because the real code happened to raise something untranslated
    - so it asserted the right outcome for the wrong reason and would have kept passing after the
    behaviour it guards was gone.
    """
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    monkeypatch.setattr(
        "docker_mcp.tools.containers._get_client",
        MagicMock(side_effect=AttributeError("'NoneType' object has no attribute 'containers'")),
    )
    with pytest.raises(UnexpectedToolError) as excinfo:
        on_the_wire("container_list", {})
    assert "NoneType" not in str(excinfo.value)


def test_every_registered_tool_translates_its_anticipated_failures():
    """The guard that makes a bypass impossible to miss.

    A tool registered with a bare `@mcp.tool` returns the right payload and passes every behaviour
    test in this file; it only stops explaining itself when something fails, which no payload
    assertion can see. So the check is on the built server rather than on the source of the modules
    that happen to register tools today, and it reaches a module that does not exist yet.
    """
    unwrapped = sorted(
        name for name, tool in _registered_tools().items() if not getattr(tool.fn, TRANSLATES_FAILURES, False)
    )
    assert not unwrapped, (
        f"tools {unwrapped} do not translate DockerMcpError - register through @tool() so a refusal "
        f"reaches the caller with its reason instead of a bare 'Error executing tool <name>'"
    )


# Every bare `raise ValueError`/`RuntimeError` left in `docker_mcp`, with why it stays one. These
# say this server is broken or in a state it did not expect: text for the log, not for the model, so
# the SDK withholding it and logging the traceback is the wanted behaviour. Keyed by function rather
# than line so ordinary edits do not churn it, and exact rather than a ceiling so removing one is
# noticed too.
DELIBERATE_CRASHES = {
    # An internal guard, not an answer to a caller: the comment at the site reads "no consumer needs
    # it today", so reaching it means an internal caller passed something unforwardable.
    ("tools/_cli.py", "_reject_unforwardable"): 2,
    # The docker CLI emitted something this server cannot parse - our bug or a format change.
    ("tools/_cli.py", "parse_ndjson"): 1,
    # Empty argv, negative max_output_bytes: internal misuse of the helper.
    ("tools/_ssh_proxy.py", "_validate_exec_args"): 2,
    # "SSH transport is not connected" in three places: a torn-down session or a missing connect.
    # Two: the transport check above, plus a `TimeoutError` when the probe never reports an exit
    # status. That one is caught two lines later by this function's own `except OSError` arm
    # (TimeoutError subclasses OSError) and falls through to WINDOWS, which is the documented
    # "no POSIX shell here" answer - it never leaves the function.
    ("tools/_ssh_proxy.py", "detect_remote_dialect"): 2,
    ("tools/_ssh_proxy.py", "exec_remote"): 1,
    ("tools/_ssh_proxy.py", "factory"): 1,
    # `_hosts` validated the URL at startup, so reaching these means a caller skipped `is_ssh_url`.
    ("tools/_ssh_proxy.py", "parse_ssh_url"): 2,
    # Negative max_bytes: internal misuse.
    ("tools/_utils.py", "join_bounded"): 1,
    # `SystemExit` on a malformed DOCKER_MCP_SERVER_HOSTS, raised at import before any tool is
    # registered. It ends the process rather than answering a caller; there is no client to tell.
    ("_hosts.py", "load"): 1,
    # Every resolved address failed to connect. `connect_ssh_client` catches OSError from this
    # helper and re-raises it as a RemoteFailureError carrying actionable guidance, so this text
    # never reaches a client on its own.
    ("tools/_ssh_proxy.py", "connect_socket_with_family_fallback"): 1,
}


def _raised_builtin(node: ast.AST) -> str | None:
    """The builtin name a `raise` statement raises, or None.

    Covers both spellings, because they are equivalent at runtime and only one of them was matched
    to begin with: `raise ValueError("...")` instantiates, `raise ValueError` lets Python do it. A
    guard that sees only the first is one a future bare `raise ValueError` walks straight past -
    which is the whole failure mode this check exists to prevent. Attribute forms
    (`builtins.ValueError`) count too.

    And every builtin exception, not a chosen few: the message withheld from the model is withheld
    whatever the class is called.
    """
    import builtins

    if not isinstance(node, ast.Raise) or node.exc is None:
        return None
    target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    if isinstance(target, ast.Name):
        name = target.id
    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "builtins":
        # Only a `builtins.`-qualified attribute. Matching on the attribute name alone counted
        # `somelib.TimeoutError` as a builtin raise, over-reporting a third-party class that merely
        # shares a name and forcing a DELIBERATE_CRASHES entry for something this rule never meant.
        name = target.attr
    else:
        return None
    # Any builtin exception, not a hand-written pair of names. The pair was a copied list and it had
    # already gone stale: `raise FileNotFoundError("pass from_url instead")` is exactly as invisible
    # to the model as a bare ValueError, and walked straight past a check written to catch it.
    # `builtins` is the enumeration, so there is nothing left to keep current.
    candidate = getattr(builtins, name, None)
    return name if isinstance(candidate, type) and issubclass(candidate, BaseException) else None


def _builtin_raise_sites() -> dict:
    """Every bare raise of a builtin exception in `docker_mcp`, counted per enclosing function.

    Any builtin, not the two names this once looked for - see `_raised_builtin`.
    """
    import ast
    import collections
    import pathlib as _pathlib

    root = _pathlib.Path(__file__).resolve().parent.parent / "docker_mcp"
    counts: collections.Counter = collections.Counter()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        spans = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    spans[line] = node.name
        for node in ast.walk(tree):
            # The isinstance check stays here as well as inside the helper: it is what narrows the
            # type for `node.lineno` below, which `ast.walk`'s `AST` does not carry.
            if isinstance(node, ast.Raise) and _raised_builtin(node):
                counts[(path.relative_to(root).as_posix(), spans.get(node.lineno, "?"))] += 1
    return dict(counts)


def test_the_builtin_raise_scan_sees_both_spellings():
    """The scan itself, on both forms plus the ones it must not count.

    Tested directly rather than by planting a raise in the package, because the guard below reports
    a count and a miscount looks identical to a clean tree from outside it.
    """
    import ast

    def scan(source: str) -> list:
        return [name for node in ast.walk(ast.parse(source)) if (name := _raised_builtin(node))]

    assert scan("raise ValueError('x')") == ["ValueError"]
    assert scan("raise ValueError") == ["ValueError"], "a parenless raise slips past the guard"
    assert scan("raise RuntimeError") == ["RuntimeError"]
    assert scan("import builtins\nraise builtins.ValueError('x')") == ["ValueError"]
    assert scan("import somelib\nraise somelib.TimeoutError('x')") == [], (
        "a third-party class sharing a builtin name is not a builtin raise"
    )
    assert scan("raise ToolInputError('x')") == []
    assert scan("try:\n    pass\nexcept Exception:\n    raise") == [], "a bare re-raise is not a site"


# ---------- library failures on the wire ----------


def test_the_library_failure_table_orders_narrow_before_broad():
    """`NotFound` subclasses `APIError` subclasses `DockerException`.

    Ordering is the whole correctness of the table: put the broad entry first and every missing
    container is classified as a daemon failure instead of a fixable argument. Asserted against the
    installed docker SDK rather than from memory, so a hierarchy change fails here.
    """
    import docker.errors

    from docker_mcp.server import _LIBRARY_FAILURES

    assert issubclass(docker.errors.NotFound, docker.errors.DockerException), "hierarchy assumption is stale"
    types = [library_type for library_type, _ in _LIBRARY_FAILURES]
    for index, library_type in enumerate(types):
        for later in types[index + 1 :]:
            assert not issubclass(later, library_type), (
                f"{later.__name__} subclasses {library_type.__name__} but is listed after it, so it "
                f"can never match - move the narrower entry first"
            )


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (docker.errors.NotFound("No such container: x"), "ToolInputError"),
        (docker.errors.APIError("500 Server Error: something broke"), "RemoteFailureError"),
        (subprocess.TimeoutExpired(cmd=["docker", "ps"], timeout=60), "RemoteFailureError"),
        # Transport failures never reach `raise_for_status()`, so listing only `HTTPStatusError`
        # left every registry timeout and connection refusal arriving as a generic crash.
        (httpx.InvalidURL("not a usable URL"), "ToolInputError"),
        (httpx.ConnectError("connection refused"), "RemoteFailureError"),
        (httpx.ConnectTimeout("timed out"), "RemoteFailureError"),
        (
            httpx.HTTPStatusError("404", request=httpx.Request("GET", "https://x"), response=httpx.Response(404)),
            "RemoteFailureError",
        ),
    ],
    ids=["NotFound", "APIError", "TimeoutExpired", "InvalidURL", "ConnectError", "ConnectTimeout", "HTTPStatusError"],
)
def test_a_library_failure_is_classified_by_the_table(exception, expected):
    from docker_mcp.server import _as_project_failure

    assert type(_as_project_failure(exception)).__name__ == expected


def test_a_daemon_rejection_reaches_the_caller_with_its_own_text(monkeypatch, on_the_wire):
    """The daemon's message is the useful part, and before the table none of it arrived.

    106 of the tools reach the daemon through docker-py, and every one of them reported
    `Error executing tool <name>` with the daemon's explanation withheld and a traceback logged at
    ERROR - the same failure the project-exception translation was written to prevent, left standing
    for the exceptions this server does not raise itself.
    """
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("404 Client Error: No such container: nope")
    monkeypatch.setattr("docker_mcp.tools.containers._get_client", MagicMock(return_value=client))

    with pytest.raises(ToolError) as excinfo:
        on_the_wire("container_inspect", {"id_or_name": "nope"})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "No such container: nope" in str(excinfo.value)


def test_a_missing_docker_binary_says_how_to_fix_it(monkeypatch, on_the_wire):
    """The most likely first-run failure there is, and it used to say nothing."""
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    monkeypatch.setattr("docker_mcp.tools._cli.shutil.which", lambda _binary: None)
    with pytest.raises(ToolError) as excinfo:
        on_the_wire("context_list", {})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "was not found on PATH" in str(excinfo.value)


def test_a_bare_builtin_raise_is_a_deliberate_crash_or_a_mistake():
    """A new bare `raise ValueError`/`RuntimeError` is almost always the wrong choice now.

    The SDK reports anything that is not a `DockerMcpError` as `Error executing tool <name>` with the
    text withheld, so a refusal or a bad-argument message written as a builtin reaches the model
    saying nothing it can act on - and nothing else fails when that happens, which is why this is a
    test rather than a note. Adding a site means either raising the right `DockerMcpError` subclass
    or adding it here with the reason it is genuinely a crash.
    """
    assert _builtin_raise_sites() == DELIBERATE_CRASHES, (
        "the bare builtin raises in docker_mcp no longer match DELIBERATE_CRASHES - raise a "
        "DockerMcpError subclass (see docker_mcp/exceptions.py) so the caller is told why, or add "
        "the site above with the reason its text belongs only in the log"
    )


def test_every_registered_resource_translates_its_anticipated_failures():
    """The resource half of the same guard.

    A resource registered with a bare `@mcp.resource` serves the right payload and passes every
    behaviour test; it only stops explaining itself when a read fails, which no payload assertion
    can see. Templates are checked alongside static resources because the SDK routes both through
    `read_resource` and classifies a template's own creation failure the same way.
    """
    registry = mcp._resource_manager
    # getattr, not `.fn`: the SDK's `Resource` base does not declare it - only the function-backed
    # subclasses do. `test_the_resource_guard_is_reading_a_full_surface` asserts every entry has one,
    # so a missing `fn` is caught there rather than being silently skipped here.
    entries = [(str(r.uri), getattr(r, "fn", None)) for r in registry._resources.values()]
    entries += [(uri, getattr(template, "fn", None)) for uri, template in registry._templates.items()]
    unwrapped = sorted(uri for uri, fn in entries if not getattr(fn, TRANSLATES_FAILURES, False))
    assert not unwrapped, (
        f"resources {unwrapped} do not translate DockerMcpError - register through @resource() so a "
        f"failed read says why instead of a bare 'Error reading resource <uri>'"
    )


def test_the_resource_guard_is_reading_a_full_surface():
    """As above: "no bad entries found" is also what an empty registry says."""
    registry = mcp._resource_manager
    assert len(registry._resources) >= 3, f"enumerated only {len(registry._resources)} static resources"
    assert len(registry._templates) >= 1, f"enumerated only {len(registry._templates)} templates"
    assert all(callable(getattr(r, "fn", None)) for r in registry._resources.values()), "Resource.fn moved"
    assert all(callable(getattr(t, "fn", None)) for t in registry._templates.values()), "ResourceTemplate.fn moved"


def test_the_translation_guard_is_reading_a_full_surface():
    """ "No bad entries found" is also what an empty list says.

    If the SDK renames `_tool_manager` or stops holding the callable on `.fn`, the check above goes
    green while checking nothing. This fails instead - the difference between a check that skipped
    and one that passed.
    """
    tools = _registered_tools()
    assert len(tools) > 100, f"enumerated only {len(tools)} tools; the surface is far larger"
    assert all(callable(getattr(tool, "fn", None)) for tool in tools.values()), "Tool.fn is no longer the callable"


def test_the_docstring_exemption_names_the_decorators_in_use() -> None:
    """The docstring rules must keep skipping the registrations that advertise a schema.

    CS.6.14 hands an advertised docstring to the AI-consumer rules rather than to the docstring
    convention, on the grounds that an `Args:` block duplicates what the schema already carries
    and is paid for on every session that loads the surface. That argument holds only where
    there is a schema to duplicate:

    - a **tool** advertises `input_schema` carrying every parameter's type, so it is exempt
    - a **prompt** advertises `arguments` carrying each name and whether it is required, so it
      is exempt
    - a **resource** advertises `uri_template`, `name`, `description` and `mime_type`, and
      nothing about its parameters at all - so there is no duplication to avoid, and its
      docstrings follow the ordinary convention like any other code

    `resource` is therefore asserted *absent* below. It was exempt once, which cost four `noqa`
    markers on the functions ruff happened to notice, and those four were not a category: a
    one-parameter resource fits `args: name - desc` on one line, which ruff never reads as a
    section, while a two-parameter one does not. Having two parameters is what earned the marker.

    pyproject.toml exempts by fully-qualified decorator path, and a path goes stale silently:
    move or rename `tool` and every advertised description falls under D417 with nothing saying
    so. Asserts both directions, plus a floor so a scan finding nothing cannot pass.
    """
    import ast
    import tomllib

    root = Path(__file__).resolve().parent.parent
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    exempt = config["tool"]["ruff"]["lint"]["pydocstyle"]["ignore-decorators"]
    # ruff matches the fully-qualified path; the call site uses the bare name it imported.
    bare = {path.rsplit(".", 1)[-1] for path in exempt}

    used: set[str] = set()
    # The whole package, not just tools/. A registration added anywhere else would otherwise
    # drift out of the exemption with nothing saying so - which is the failure this test is for.
    for module in sorted((root / "docker_mcp").rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                source = ast.unparse(decorator)
                name = source.split("(")[0].split(".")[-1].lstrip("@")
                if name in {"tool", "prompt", "resource"}:
                    used.add(name)

    assert len(used) >= 3, f"only found {sorted(used)} in use, so this check is not seeing the surface"
    schema_carrying = {"tool", "prompt"} & used
    missing = sorted(schema_carrying - bare)
    assert not missing, (
        f"pyproject.toml's ignore-decorators does not cover {missing}, so those advertised "
        f"docstrings would fall under the docstring convention. It names {sorted(bare)}."
    )
    assert "resource" not in bare, (
        "ignore-decorators names `resource`, but a resource advertises no schema for its "
        "parameters, so there is nothing for a docstring to duplicate and the exemption buys "
        "nothing. Resource docstrings follow the ordinary convention."
    )

    # And the qualified paths must actually resolve, or ruff silently matches nothing.
    import importlib

    for path in exempt:
        module_name, _, attribute = path.rpartition(".")
        assert hasattr(importlib.import_module(module_name), attribute), (
            f"ignore-decorators names {path!r}, which does not exist - ruff would match nothing "
            f"and every advertised docstring would fall under the convention"
        )
