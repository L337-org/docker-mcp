"""A budget on the advertised surface, because AC.1.2 makes its size a tracked metric.

Every byte here is paid for by every client on every session, before it has asked for anything.
This server advertises 164 tools, and at that size the surface is the dominant cost of using it
at all - which is exactly why it needs a number attached rather than an intention.

WHAT IS MEASURED IS THE WIRE FORM, not the docstring. A tool costs its name, its description and
its whole input schema, and the schema is usually the larger half: `buildx_build` is 5,091 bytes
on the wire against 3,398 of description. Measuring docstrings alone would have missed the
schema-slimming work entirely, and would go green on a change that added twenty parameters.

Bytes rather than tokens, deliberately. Tokens are what a model actually pays, but they need a
tokenizer pinned to a model that changes under us, and the figure then moves without the surface
moving - which is why `scripts/measure-comparison-figures.py` reports tokens and is explicitly
not a gate. Bytes are deterministic, need no dependency, and move when and only when the surface
does. This is a ratchet, not an accounting system.

RAISING A BUDGET IS A DELIBERATE DECISION, not a way to make a red test green. The number is the
point: it forces the question "is this new parameter worth what every session will pay for it?"
to be asked once, in a pull request, rather than never.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import docker_mcp.tools  # noqa: F401 - importing registers the surface
from docker_mcp.server import mcp

# Measured at the time of writing, with roughly two per cent of headroom: enough that ordinary
# rewording does not trip the gate, little enough that real growth does.
MAX_SINGLE_TOOL_WIRE_BYTES = 5_300  # buildx_build, 5,091
MAX_TOOL_WIRE_BYTES = 231_000  # 226,529
MAX_PROMPT_WIRE_BYTES = 7_900  # 7,646
MAX_RESOURCE_WIRE_BYTES = 6_400  # 6,180, resources and templates together
MAX_INSTRUCTIONS_BYTES = 2_900  # 2,749
MAX_TOTAL_WIRE_BYTES = 248_000  # 243,104

# Registration is gated at import time, so a switch set in the environment shrinks the surface
# and every budget below would pass while measuring something else entirely.
REGISTRATION_SWITCHES = (
    "DOCKER_MCP_SERVER_READONLY",
    "DOCKER_MCP_SERVER_NO_DESTRUCTIVE",
    "DOCKER_MCP_SERVER_DISABLE",
    "DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE",
)

# Floors, not exact counts. An exact count would fail on every new tool, which the byte budgets
# already price; these only answer "am I looking at the full surface?".
MIN_TOOLS = 150
MIN_PROMPTS = 25
MIN_RESOURCES = 5


@dataclass(frozen=True)
class Surface:
    """The advertised surface, measured in bytes as a client would receive it."""

    per_tool: dict[str, int]
    tools: int
    prompts: int
    resources: int
    instructions: int
    counts: tuple[int, int, int]


def _surface() -> Surface:
    """Collect the advertised surface as a client would receive it.

    Returns:
        Surface: Wire sizes and counts for tools, prompts, resources and the router instructions.
    """

    def wire(item: Any) -> int:
        return len(json.dumps(item.model_dump(mode="json"), separators=(",", ":")).encode())

    async def collect() -> Surface:
        tools = await mcp.list_tools()
        prompts = await mcp.list_prompts()
        resources = await mcp.list_resources()
        templates = await mcp.list_resource_templates()
        per_tool = {t.name: wire(t) for t in tools}
        return Surface(
            per_tool=per_tool,
            tools=sum(per_tool.values()),
            prompts=sum(wire(p) for p in prompts),
            resources=sum(wire(r) for r in resources) + sum(wire(r) for r in templates),
            instructions=len((mcp.instructions or "").encode()),
            counts=(len(tools), len(prompts), len(resources) + len(templates)),
        )

    return asyncio.run(collect())


def test_the_measured_surface_is_the_whole_surface() -> None:
    """Guards every budget below: they mean nothing if the surface was cut before measuring.

    A registration switch produces a smaller surface that passes every ceiling comfortably - 76
    tools instead of 164 under READONLY - so a run would report the budget holding when it had
    not been tested at all.

    This asserts conftest is still doing its job rather than duplicating it. conftest pops these
    variables at import time, before `docker_mcp.server` is imported, precisely so a developer
    with one exported does not get a quietly different suite. If that scrubbing is ever removed,
    the failure surfaces here with the reason attached instead of as an inexplicably comfortable
    set of byte counts.
    """
    set_switches = [name for name in REGISTRATION_SWITCHES if os.environ.get(name)]
    assert not set_switches, (
        f"{', '.join(set_switches)} is set at test time, which conftest is supposed to have "
        f"cleared before docker_mcp.server was imported. The surface is now smaller than the "
        f"one these ceilings describe, so every budget below is measuring a fragment"
    )

    tools, prompts, resources = _surface().counts
    assert tools >= MIN_TOOLS and prompts >= MIN_PROMPTS and resources >= MIN_RESOURCES, (
        f"found {tools} tools, {prompts} prompts and {resources} resources, fewer than the "
        f"{MIN_TOOLS}/{MIN_PROMPTS}/{MIN_RESOURCES} floor - the surface did not register fully, "
        f"so the budgets below would be measuring a fragment"
    )


def test_no_single_tool_exceeds_its_share_of_the_surface() -> None:
    """One tool should not be able to grow without anyone noticing which one it was.

    The per-tool ceiling is what makes a total-only budget honest: without it, one tool can take
    a thousand bytes while another gives them back, and the total says nothing happened.
    """
    per_tool = _surface().per_tool
    over = {name: size for name, size in per_tool.items() if size > MAX_SINGLE_TOOL_WIRE_BYTES}
    assert not over, (
        f"over the {MAX_SINGLE_TOOL_WIRE_BYTES}-byte per-tool ceiling: "
        + ", ".join(f"{n} at {s}" for n, s in sorted(over.items(), key=lambda kv: -kv[1]))
        + ". Either trim it, or raise the ceiling deliberately and say in the commit message "
        "what the extra bytes buy on every session"
    )


def test_the_advertised_surface_stays_within_budget() -> None:
    """Each component and the whole, so a regression names the part that grew.

    Reported together rather than one assertion at a time: a change that moves bytes from tools
    into prompts should be legible as that, not as two separate runs.
    """
    surface = _surface()
    budgets = {
        "tools": (surface.tools, MAX_TOOL_WIRE_BYTES),
        "prompts": (surface.prompts, MAX_PROMPT_WIRE_BYTES),
        "resources": (surface.resources, MAX_RESOURCE_WIRE_BYTES),
        "instructions": (surface.instructions, MAX_INSTRUCTIONS_BYTES),
    }
    total = sum(actual for actual, _ in budgets.values())
    budgets["TOTAL"] = (total, MAX_TOTAL_WIRE_BYTES)

    over = {part: (actual, cap) for part, (actual, cap) in budgets.items() if actual > cap}
    assert not over, (
        "the advertised surface is over budget:\n  "
        + "\n  ".join(f"{part}: {actual} bytes, over {cap} by {actual - cap}" for part, (actual, cap) in over.items())
        + "\n\nRaising a budget is a deliberate decision, not a way to make this green. "
        "Every byte is paid for by every client on every session before it asks for anything."
    )
