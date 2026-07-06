import inspect
import json
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import docker_mcp  # noqa: F401 — imported for its side effect of registering every tool
import docker_mcp._hosts as _hosts
from docker_mcp._hosts import parse_registry
from docker_mcp.server import (
    TOOL_CATEGORIES,
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
    _prompt_registry,
    _seen_tool_names,
    _should_register,
    _slim_schema,
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


# ---------- host param: call-time guard ----------


def test_guard_allows_read_only_without_host(monkeypatch):
    _set_multi_host(monkeypatch)
    _enforce_host_guard("container_list", ToolCategory.READ_ONLY, None)  # no raise


def test_guard_requires_host_for_write(monkeypatch):
    _set_multi_host(monkeypatch)
    with pytest.raises(RuntimeError, match="'host' is required"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, None)


def test_guard_rejects_unknown_host(monkeypatch):
    _set_multi_host(monkeypatch)
    with pytest.raises(RuntimeError, match="unknown host 'staging'"):
        _enforce_host_guard("container_list", ToolCategory.READ_ONLY, "staging")


def test_guard_rejects_write_to_read_only_host(monkeypatch):
    _set_multi_host(monkeypatch)  # prod is (ro)
    with pytest.raises(RuntimeError, match="read-only"):
        _enforce_host_guard("container_remove", ToolCategory.DESTRUCTIVE, "prod")


def test_guard_allows_connection_control_on_read_only_host(monkeypatch):
    _set_multi_host(monkeypatch)
    _enforce_host_guard("system_close", ToolCategory.MUTATING, "prod")  # conn-control exempt from ro-refusal


def test_guard_checks_unknown_host_even_for_connection_control(monkeypatch):
    _set_multi_host(monkeypatch)
    with pytest.raises(RuntimeError, match="unknown host"):
        _enforce_host_guard("system_close", ToolCategory.MUTATING, "typo")


# ---------- host param: single-host (ro) enforcement ----------


def test_guard_refuses_write_to_single_read_only_host(monkeypatch):
    # One (ro) host: the schema carries no host param to pass, but writes must still be refused.
    _set_single_host(monkeypatch, "ssh://h(ro)")
    with pytest.raises(RuntimeError, match="read-only"):
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


def test_host_guard_needed_matrix(monkeypatch):
    _set_single_host(monkeypatch, "ssh://h(ro)")
    assert _host_guard_needed() is True  # single (ro): wrap to refuse writes
    monkeypatch.setattr(_hosts, "_registry", parse_registry("ssh://h"))
    assert _host_guard_needed() is False  # single writable: footprint-neutral, no wrap
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
    with pytest.raises(RuntimeError, match="'host' is required"):
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
    with pytest.raises(RuntimeError, match="'host' is required"):
        asyncio.run(wrapped(container_id="abc"))  # write without host
    assert asyncio.run(wrapped(container_id="abc", host="local")) == "removed abc on local"


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
        assert ann.readOnlyHint is (category is ToolCategory.READ_ONLY), name
        assert ann.destructiveHint is (category is ToolCategory.DESTRUCTIVE), name


def test_annotations_for_read_only():
    ann = _annotations_for("container_list", ToolCategory.READ_ONLY)
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False


def test_annotations_for_mutating():
    ann = _annotations_for("container_run", ToolCategory.MUTATING)
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False


def test_annotations_for_destructive_prune_is_idempotent():
    ann = _annotations_for("image_prune", ToolCategory.DESTRUCTIVE)
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True
    assert ann.idempotentHint is True


def test_annotations_for_destructive_non_prune_not_marked_idempotent():
    ann = _annotations_for("container_remove", ToolCategory.DESTRUCTIVE)
    assert ann.destructiveHint is True
    assert ann.idempotentHint is None


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
    assert "- containers —" in text
    assert "- images —" in text
    # A domain that didn't register must not be advertised — the whole point of building it dynamically.
    assert "- swarm —" not in text
    assert "- compose —" not in text


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


def test_instructions_default_to_the_live_registered_surface():
    # No argument -> reads _tool_registry; with everything registered, every domain blurb appears.
    # _NO_DOMAIN_TOOLS (domain=None) are excluded — they never get a per-domain router line.
    text = build_instructions()
    present = {rec.domain for rec in _tool_registry.values() if rec.registered and rec.domain is not None}
    for domain in present:
        assert f"- {domain} —" in text


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
    """The domains listed in the router's 'Domains' block. Scoped to that block so the caveat bullets —
    which also start with '- ' and contain an em-dash (e.g. the `*_to_file` line) — aren't mistaken for
    domain lines."""
    lines = instructions.splitlines()
    start = lines.index("Domains (and the words that find them):") + 1
    domains = set()
    for line in lines[start:]:
        if not line.strip():
            break
        domains.add(line[2:].split(" — ", 1)[0])
    return domains


def test_live_instructions_exclude_a_disabled_domain_end_to_end():
    # finalize_instructions() runs at package import, so a disabled domain must be gone from the string
    # the client actually receives — not just from the registered tool set.
    text = _live_instructions(["DOCKER_MCP_SERVER_DISABLE=swarm,services,nodes,secrets,configs"])
    assert "- swarm —" not in text
    assert "- services —" not in text
    assert "Swarm-family tools require" not in text
    assert "- containers —" in text  # untouched domains survive


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


def test_single_read_only_host_refuses_write_end_to_end():
    # Proves the guard is actually wrapped onto write tools at import time for a single (ro) host.
    code = (
        "from docker_mcp.tools import containers\n"
        "try:\n"
        "    containers.container_stop('x')\n"
        "    print('NOGUARD')\n"
        "except RuntimeError as e:\n"
        "    print('REFUSED' if 'read-only' in str(e) else 'OTHER')\n"
    )
    env = _env_with(["DOCKER_MCP_SERVER_HOSTS=ssh://h(ro)"])
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

    monkeypatch.setattr(stack, "run_docker", fake_run_docker)
    stack.stack_list(host="prod")
    assert captured.get("host") == "prod"
