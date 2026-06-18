import subprocess
import sys

import docker_mcp  # noqa: F401 — imported for its side effect of registering every tool
from docker_mcp.server import (
    TOOL_CATEGORIES,
    _SCHEMA_NAME_MAPS,
    ToolCategory,
    _annotations_for,
    build_instructions,
    _domain_enabled,
    _domain_for,
    _parse_domains,
    _prompt_registry,
    _seen_tool_names,
    _should_register,
    _strip_schema_titles,
    _tool_registry,
    mcp,
    tool_catalog,
)


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
    ann = _annotations_for("list_containers", ToolCategory.READ_ONLY)
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False


def test_annotations_for_mutating():
    ann = _annotations_for("run_container", ToolCategory.MUTATING)
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False


def test_annotations_for_destructive_prune_is_idempotent():
    ann = _annotations_for("prune_images", ToolCategory.DESTRUCTIVE)
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True
    assert ann.idempotentHint is True


def test_annotations_for_destructive_non_prune_not_marked_idempotent():
    ann = _annotations_for("remove_container", ToolCategory.DESTRUCTIVE)
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
    # The registry records a domain for every tool, derived from its defining module.
    assert set(_tool_registry) == set(TOOL_CATEGORIES)
    assert all(rec.domain for rec in _tool_registry.values())
    # Sanity-check a couple of known module -> domain mappings.
    assert _tool_registry["list_containers"].domain == "containers"
    assert _tool_registry["compose_up"].domain == "compose"


# ---------- tool catalog ----------


def test_tool_catalog_lists_every_tool_with_taxonomy():
    catalog = tool_catalog()
    names = {t["name"] for t in catalog["tools"]}
    assert names == set(TOOL_CATEGORIES)
    for entry in catalog["tools"]:
        assert entry["category"] == TOOL_CATEGORIES[entry["name"]].value
        assert entry["domain"]
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
    # Clear every switch (canonical + deprecated alias) first so the parent environment can't leak
    # into the child.
    for switch in ("READONLY", "NO_DESTRUCTIVE", "DISABLE"):
        env.pop(f"DOCKER_MCP_SERVER_{switch}", None)
        env.pop(f"DOCKER_MCP_{switch}", None)
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


def test_deprecated_disable_alias_still_drops_domains_end_to_end():
    # The pre-rename DOCKER_MCP_DISABLE spelling is still honored as a deprecated alias, so existing
    # client configs keep working after the rename to DOCKER_MCP_SERVER_DISABLE.
    dropped = _names_by_domain("swarm", "plugins")
    names = _registered_names(["DOCKER_MCP_DISABLE=swarm,plugins"])
    assert names == set(TOOL_CATEGORIES) - dropped


def test_canonical_disable_wins_over_deprecated_alias_end_to_end():
    # When both spellings are set, the canonical DOCKER_MCP_SERVER_DISABLE takes precedence.
    names = _registered_names(["DOCKER_MCP_SERVER_DISABLE=swarm", "DOCKER_MCP_DISABLE=compose"])
    assert names == set(TOOL_CATEGORIES) - _names_by_domain("swarm")


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
    text = build_instructions()
    present = {rec.domain for rec in _tool_registry.values() if rec.registered}
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
        expected = {_tool_registry[name].domain for name in _registered_names(env)}
        assert _router_domain_lines(_live_instructions(env)) == expected, env


# ---------- typed parameter schemas ----------


def test_run_container_restart_policy_schema_is_typed():
    # The RestartPolicy TypedDict must surface as a structured schema (enum'd Name field),
    # not an opaque dict, so the agent knows the valid keys/values without guessing.
    schema = _registered_tools()["run_container"].parameters
    assert "RestartPolicy" in schema.get("$defs", {})
    rp = schema["$defs"]["RestartPolicy"]["properties"]
    assert set(rp) == {"Name", "MaximumRetryCount"}
    assert set(rp["Name"]["enum"]) == {"no", "always", "on-failure", "unless-stopped"}


def _has_title_anywhere(node) -> bool:
    # Mirror _strip_schema_titles' traversal: a `title` *key* inside a name-map (e.g. a property
    # literally named "title") is a name, not an annotation, and must not count as a leftover.
    if isinstance(node, dict):
        if "title" in node:
            return True
        for key, value in node.items():
            if key in _SCHEMA_NAME_MAPS and isinstance(value, dict):
                if any(_has_title_anywhere(sub) for sub in value.values()):
                    return True
            elif _has_title_anywhere(value):
                return True
        return False
    if isinstance(node, list):
        return any(_has_title_anywhere(item) for item in node)
    return False


def test_no_registered_tool_schema_carries_title_annotations():
    # pydantic stamps an information-free `title` on every property/$def and the top-level schema;
    # the decorator strips them (~10% of the advertised tool surface). Assert none survive.
    offenders = [name for name, t in _registered_tools().items() if _has_title_anywhere(t.parameters)]
    assert not offenders, f"tools still advertising `title` annotations: {offenders}"


def test_strip_schema_titles_preserves_a_param_named_title():
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
    _strip_schema_titles(schema)
    assert "title" not in schema  # top-level annotation gone
    assert set(schema["properties"]) == {"title", "count"}  # the param NAMED title survives
    assert "title" not in schema["properties"]["title"]  # its own annotation is gone
    assert schema["properties"]["title"]["type"] == "string"  # type preserved


# ---------- prompt + doc-resource disabling (DOCKER_MCP_SERVER_DISABLE covers more than tools) ----------


def test_every_prompt_recorded_in_registry():
    # Every registered prompt has a record; by default (no switches) all of them register.
    registered = set(_registered_prompts())
    assert registered, "fixture sanity: expected prompts to exist"
    assert registered <= set(_prompt_registry)
    assert all(r.registered for r in _prompt_registry.values())


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
    # No switches in this process, so everything is registered and nothing is hidden.
    assert all(p["registered"] for p in catalog["prompts"])
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
    # Disabling scout removes exactly the scout prompts and leaves every other prompt registered.
    scout_prompts = _prompt_names_by_domain("scout")
    assert scout_prompts, "fixture sanity: expected scout prompts to exist"
    names = _registered_prompt_names(["DOCKER_MCP_SERVER_DISABLE=scout"])
    assert names == _all_prompt_names() - scout_prompts


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
