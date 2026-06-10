import subprocess
import sys

import docker_mcp  # noqa: F401 — imported for its side effect of registering every tool
from docker_mcp.server import (
    TOOL_CATEGORIES,
    ToolCategory,
    _annotations_for,
    _is_truthy,
    _seen_tool_names,
    _should_register,
    mcp,
)


def _registered_tools() -> dict:
    return mcp._tool_manager._tools


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


def test_is_truthy():
    for v in ["1", "true", "TRUE", "Yes", "on", " on "]:
        assert _is_truthy(v) is True
    for v in [None, "", "0", "false", "no", "off", "nope"]:
        assert _is_truthy(v) is False


# ---------- end-to-end registration under the env switches (separate processes) ----------


def _count_registered(env_var: str | None) -> int:
    code = "import docker_mcp; from docker_mcp.server import mcp; print(len(mcp._tool_manager._tools))"
    env_args = [env_var] if env_var else []
    result = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell; trusted test input
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env_with(env_args),
        check=True,
    )
    return int(result.stdout.strip())


def _env_with(assignments: list[str]) -> dict:
    import os

    env = dict(os.environ)
    # Clear both switches first so the parent environment can't leak into the child.
    env.pop("DOCKER_MCP_READONLY", None)
    env.pop("DOCKER_MCP_NO_DESTRUCTIVE", None)
    for assignment in assignments:
        key, _, value = assignment.partition("=")
        env[key] = value
    return env


def test_readonly_env_registers_only_read_only_tools():
    expected = sum(1 for c in TOOL_CATEGORIES.values() if c is ToolCategory.READ_ONLY)
    assert _count_registered("DOCKER_MCP_READONLY=1") == expected


def test_no_destructive_env_drops_destructive_tools():
    destructive = sum(1 for c in TOOL_CATEGORIES.values() if c is ToolCategory.DESTRUCTIVE)
    assert _count_registered("DOCKER_MCP_NO_DESTRUCTIVE=1") == len(TOOL_CATEGORIES) - destructive


def test_default_env_registers_all_tools():
    assert _count_registered(None) == len(TOOL_CATEGORIES)
