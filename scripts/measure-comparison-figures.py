#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["tiktoken==0.13.0", "docker-mcp-server"]
#
# [tool.uv.sources]
# docker-mcp-server = { path = "../", editable = true }
# ///
"""Regenerate every measured figure quoted in `MCP_VS_SKILLS.md`.

Run it from anywhere with `uv run scripts/measure-comparison-figures.py` (add `--json` for the
machine-readable form).  `uv` builds a throwaway environment from the header above, installing the
working tree editable, so what gets measured is always the checked-out code and never a published
release.  `tiktoken` is pinned here rather than in `pyproject.toml`'s dev group deliberately: it is
needed by this script alone, so a developer who never runs it pays nothing for it, and a tokenizer
bump becomes a visible one-line edit here rather than a silent lockfile change that moves every
figure at once.

REPORT ONLY, BY DESIGN.  This script never edits `MCP_VS_SKILLS.md`.  A tool that can rewrite the
figures is a tool that can quietly launder a wrong number into the document, so correcting the
prose stays a reviewed human (or agent) edit, informed by this output.

NEVER A CI GATE, ALSO BY DESIGN.  Token figures move by a handful of tokens on any docstring edit,
so asserting them on every PR would fail constantly while telling nobody anything - noise, not
signal.  Drift is watched on a schedule instead, by the "MCP vs skills figure drift" routine in
`L337-org/claude-routines`, which judges whether a movement is worth republishing.  Please do not
promote this to a merge gate; the one thing here worth asserting mechanically (that the script and
the document describe the same configurations) already is, in `tests/test_skill.py`.

THE METHOD
==========
Counted with `tiktoken`'s `cl100k_base`, which is not Claude's tokenizer: absolute numbers carry
roughly +/-10-15%, and the ratios between them are the finding.

Each item is measured as a client receives it, meaning the wire form: `model_dump_json` with
`by_alias=True, exclude_none=True`, the same flags the `mcp` package itself uses when serialising a
response.  That matters for exactly one field today - a client is sent `inputSchema`, never the
`input_schema` spelling the Python attribute uses - and it accounts for the whole 111-token
difference between this script's tools figure and the one published before it existed.  Prompts and
resources are unaffected by the alias.

- `tools`               sum over `list_tools()` of the wire form of each tool
- `tool_names`          sum over `list_tools()` of `name + "\\n"`, the lazy-client always-in-context cost
- `prompts`             sum over `list_prompts()` of the wire form
- `resources`           sum over `list_resources()` of the wire form
- `resource_templates`  sum over `list_resource_templates()` of the wire form
- `router`              the served `instructions` string
- `eager_idle`          tools + prompts + resources + router
- `lazy_idle`           router + tool_names

`eager_idle` follows the document's own definition, which omits `resource_templates`.  A client that
loads eagerly does receive that list too, so `eager_idle_with_templates` is reported alongside it;
the two differ by whatever the templates cost.  Which of them the document should quote is a
question for a human, not something this script decides by picking one.

MEASURED VERSUS CHOSEN
======================
Everything above is measured.  The per-task figures are measured arithmetic over a *chosen* input:
which tools an agent fetches for a task, and which skill files it loads, are judgement calls, not
properties of the code.  They are named explicitly in `TASKS` below so a reader can disagree with
the composition rather than reverse-engineer it from a total.  Two prose counts over the skill
(`prefer_passages`, `command_lines`) are heuristics, flagged as such in the output: treat a movement
in them as a prompt to look, never as a fact.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_DIR = _REPO_ROOT / "skills" / "l337-docker"
_ENCODING = "cl100k_base"

# Every configuration the document quotes, keyed by the name used in its tables.  A config is
# defined by the domains it KEEPS: the `DOCKER_MCP_SERVER_DISABLE` value is derived from the live
# domain list, so a new server domain lands in these configs automatically instead of silently
# staying enabled in a config that was meant to exclude it.
_CONFIGS: dict[str, dict[str, Any]] = {
    "full": {"doc_row": "Full whack: everything enabled", "keep": None, "readonly": False},
    "readonly": {"doc_row": "Read-only, all domains", "keep": None, "readonly": True},
    "triage": {
        "doc_row": "Triage config",
        "keep": {"containers", "images", "networks", "volumes", "system"},
        "readonly": False,
    },
    "core": {"doc_row": "Core only: `containers` + `system`", "keep": {"containers", "system"}, "readonly": False},
    "floor": {"doc_row": "Floor: core, read-only", "keep": {"containers", "system"}, "readonly": True},
}

# The per-task compositions.  CHOSEN, not measured - see MEASURED VERSUS CHOSEN above.
#
# `tools` is what an agent on a lazy client would fetch definitions for; the cost is those
# definitions on top of that config's lazy idle.  On an eager client every definition is already
# loaded, so the task costs exactly the eager idle, whatever the task is.
#
# `skill_files` is what the host would load: the router always, plus the reference and workflow
# files the task needs.  These three sets reproduce the published skill figures exactly, so they are
# the compositions the original hand measurement used.
_TASKS: dict[str, dict[str, Any]] = {
    "List containers (one-off)": {
        "tools": ["container_list"],
        "skill_files": ["SKILL.md", "reference/observability.md"],
    },
    "Triage a crashed container": {
        # Find it, learn why it exited (State.ExitCode / OOMKilled), read what it said, check whether
        # the host was under pressure, put it back.
        "tools": [
            "container_list",
            "container_inspect",
            "container_logs",
            "container_stats",
            "container_restart",
        ],
        "skill_files": ["SKILL.md", "reference/observability.md", "workflows/troubleshoot.md"],
    },
    "Bring up a Compose project": {
        # Validate the merged config, bring it up, confirm what came up, read the first logs.
        "tools": ["compose_config", "compose_up", "compose_ps", "compose_logs"],
        "skill_files": ["SKILL.md", "reference/compose.md", "workflows/deploy.md"],
    },
}

# Heuristics over the skill's prose.  Advisory: they reproduce the published counts exactly, which
# is why these definitions are the ones kept, but neither has a single defensible definition.
_PREFER_PATTERN = re.compile(r"(?i)\b(prefer|instead of|rather than)\b")
_FENCE_PATTERN = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def _encoder():
    """The tokenizer, imported lazily so `--help` works without touching the network.

    `tiktoken` downloads the `cl100k_base` table on first use and caches it, so the first run of
    this script (or any run in a fresh environment) needs `openaipublic.blob.core.windows.net`.
    """
    import tiktoken

    return tiktoken.get_encoding(_ENCODING)


# --------------------------------------------------------------------------------------------
# Worker: measures one configuration.  Runs in its own process because the switches it varies are
# read at import time, so a single process could only ever measure one of them.
# --------------------------------------------------------------------------------------------


def _measure_current_surface() -> dict[str, Any]:
    """Measure the surface this process has registered, returning every server-side figure."""
    import asyncio

    import docker_mcp  # noqa: F401  (side-effect import: registers tools, prompts and resources)
    from docker_mcp.server import mcp, tool_catalog

    enc = _encoder()

    def count(text: str) -> int:
        return len(enc.encode(text))

    def wire(item: Any) -> int:
        return count(item.model_dump_json(by_alias=True, exclude_none=True))

    async def collect() -> dict[str, Any]:
        tools = await mcp.list_tools()
        prompts = await mcp.list_prompts()
        resources = await mcp.list_resources()
        templates = await mcp.list_resource_templates()

        per_tool = {t.name: wire(t) for t in tools}
        tools_tokens = sum(per_tool.values())
        names_tokens = sum(count(t.name + "\n") for t in tools)
        prompts_tokens = sum(wire(p) for p in prompts)
        resources_tokens = sum(wire(r) for r in resources)
        templates_tokens = sum(wire(r) for r in templates)
        router_tokens = count(mcp.instructions or "")

        catalog = tool_catalog()
        domains: dict[str, int] = {}
        for entry in catalog["tools"]:
            key = entry.get("domain") or "uncategorised"
            domains[key] = domains.get(key, 0) + 1

        return {
            "counts": {
                "tools": len(tools),
                "prompts": len(prompts),
                "resources": len(resources),
                "resource_templates": len(templates),
            },
            "tokens": {
                "tools": tools_tokens,
                "tool_names": names_tokens,
                "prompts": prompts_tokens,
                "resources": resources_tokens,
                "resource_templates": templates_tokens,
                "router": router_tokens,
                "eager_idle": tools_tokens + prompts_tokens + resources_tokens + router_tokens,
                "eager_idle_with_templates": (
                    tools_tokens + prompts_tokens + resources_tokens + templates_tokens + router_tokens
                ),
                "lazy_idle": router_tokens + names_tokens,
            },
            "tool_definition_tokens": {
                "median": statistics.median(per_tool.values()) if per_tool else 0,
                "min": min(per_tool.values()) if per_tool else 0,
                "max": max(per_tool.values()) if per_tool else 0,
            },
            "domains": dict(sorted(domains.items())),
            "per_tool_tokens": per_tool,
            "parameters": _parameter_stats(tools),
            "discriminators": _discriminator_stats(tools),
        }

    return asyncio.run(collect())


def _constrains_values(spec: Any) -> bool:
    """Whether a parameter's schema closes its value set anywhere within it.

    An `enum` reaches a parameter three ways: directly on the property, inside an `anyOf` branch (a
    nullable enum), or on an array's `items` (a list of allowed values, which is how
    `scout_cves`/`scout_compare` constrain `only_severity`).  All three constrain what a caller may
    send, so all three count.  The walk looks for the `enum` key rather than the string, so a
    description that happens to use the word does not register.
    """
    if isinstance(spec, dict):
        return "enum" in spec or any(_constrains_values(value) for value in spec.values())
    if isinstance(spec, list):
        return any(_constrains_values(item) for item in spec)
    return False


def _parameter_stats(tools: list[Any]) -> dict[str, int]:
    """Count declared parameters and how many carry a default, a requirement or a closed value set.

    Every parameter is typed by construction, so the interesting figures are the ones a schema does
    not have to have: an explicit default, presence in `required`, and a constrained set of legal
    values.
    """
    total = with_default = required = with_enum = 0
    for tool in tools:
        schema = tool.model_dump(by_alias=True, exclude_none=True).get("inputSchema") or {}
        properties: dict[str, Any] = schema.get("properties") or {}
        required_names = set(schema.get("required") or ())
        total += len(properties)
        required += len(required_names & properties.keys())
        for spec in properties.values():
            if "default" in spec:
                with_default += 1
            if _constrains_values(spec):
                with_enum += 1
    return {
        "total": total,
        "with_default": with_default,
        "required": required,
        "with_enum": with_enum,
    }


def _discriminator_stats(tools: list[Any]) -> dict[str, Any]:
    """Count how many tool descriptions name a sibling tool by its exact registered name.

    The docstring standard asks every description to say which neighbouring tool to prefer and
    when, so this is the mechanical half of that: an exact-name match, since that is also what a
    lazy client's keyword search matches on.  A tool naming itself does not count.
    """
    names = [t.name for t in tools]
    naming: dict[str, int] = {}
    for tool in tools:
        description = tool.description or ""
        siblings = {
            other for other in names if other != tool.name and re.search(rf"\b{re.escape(other)}\b", description)
        }
        naming[tool.name] = len(siblings)
    with_sibling = sum(1 for n in naming.values() if n)
    return {
        "tools": len(names),
        "naming_a_sibling": with_sibling,
        "mean_siblings_named": round(sum(naming.values()) / len(names), 2) if names else 0.0,
        "naming_none": sorted(name for name, n in naming.items() if not n),
    }


# --------------------------------------------------------------------------------------------
# Parent: drives one worker per configuration, then measures the skill and assembles the report.
# --------------------------------------------------------------------------------------------


def _run_worker(env_overrides: dict[str, str]) -> dict[str, Any]:
    """Measure one configuration in a fresh process, with the given switches in its environment."""
    env = {**os.environ, **env_overrides}
    # Keep the parent's own switches out of the child: an inherited DOCKER_MCP_SERVER_DISABLE would
    # silently narrow every configuration measured here.
    for key in ("DOCKER_MCP_SERVER_DISABLE", "DOCKER_MCP_SERVER_READONLY", "DOCKER_MCP_SERVER_NO_DESTRUCTIVE"):
        if key not in env_overrides:
            env.pop(key, None)
    proc = subprocess.run(  # noqa: S603  (fixed argv, no shell, interpreter resolved from sys.executable)
        [sys.executable, str(Path(__file__).resolve()), "--measure-current-surface"],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"measuring the surface failed (exit {proc.returncode}) with "
            f"{env_overrides or 'no switches'}:\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _disable_value(all_domains: set[str], keep: set[str] | None) -> str | None:
    """The `DOCKER_MCP_SERVER_DISABLE` value that keeps exactly `keep`, or None to keep everything."""
    if keep is None:
        return None
    unknown = keep - all_domains
    if unknown:
        raise RuntimeError(
            f"config keeps domains that no longer exist: {sorted(unknown)}. "
            "A domain has been renamed or removed - update _CONFIGS."
        )
    return ",".join(sorted(all_domains - keep))


def _measure_server() -> dict[str, Any]:
    """Measure every configuration the document quotes, deriving domain lists from the live surface."""
    full = _run_worker({})
    all_domains = {d for d in full["domains"] if d != "uncategorised"}

    measured: dict[str, Any] = {}
    for name, config in _CONFIGS.items():
        disable = _disable_value(all_domains, config["keep"])
        env: dict[str, str] = {}
        if disable:
            env["DOCKER_MCP_SERVER_DISABLE"] = disable
        if config["readonly"]:
            env["DOCKER_MCP_SERVER_READONLY"] = "1"
        result = full if name == "full" else _run_worker(env)
        measured[name] = {
            "doc_row": config["doc_row"],
            "switches": env or {"(none)": "everything enabled"},
            **result,
        }
    return measured


def _measure_skill() -> dict[str, Any]:
    """Measure the skill: its always-in-context frontmatter, every file, and the two prose counts."""
    enc = _encoder()
    files = sorted(_SKILL_DIR.rglob("*.md"))
    if not files:
        raise RuntimeError(f"no skill markdown found under {_SKILL_DIR}")

    per_file = {str(p.relative_to(_SKILL_DIR)): len(enc.encode(p.read_text(encoding="utf-8"))) for p in files}

    # What the host keeps in context when the skill is installed but untriggered: the `name` and
    # `description` frontmatter fields, nothing else.  `license` is in the block but is metadata the
    # model is never shown.
    frontmatter = _SKILL_DIR.joinpath("SKILL.md").read_text(encoding="utf-8").split("---")[1]
    advertised = "\n".join(
        line for line in frontmatter.strip().splitlines() if line.startswith(("name:", "description:"))
    )

    prefer = command_lines = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        prefer += len(_PREFER_PATTERN.findall(text))
        command_lines += sum(
            1
            for block in _FENCE_PATTERN.findall(text)
            for line in block.splitlines()
            if line.strip().startswith("docker ")
        )

    return {
        "tokens": {
            "advertised": len(enc.encode(advertised)),
            "router": per_file["SKILL.md"],
            "all_files": sum(per_file.values()),
        },
        "per_file_tokens": per_file,
        "heuristics": {
            "prefer_passages": prefer,
            "command_lines": command_lines,
            "definitions": {
                "prefer_passages": "case-insensitive matches of 'prefer', 'instead of' or 'rather than'",
                "command_lines": "lines inside fenced blocks that start with 'docker '",
            },
        },
    }


def _measure_tasks(server: dict[str, Any], skill: dict[str, Any]) -> dict[str, Any]:
    """Cost each task on every configuration, and on the skill.

    A task whose tools are not all registered in a configuration is reported as unavailable rather
    than as a number, which is what the document's "n/a" footnote says in prose: a trimmed surface
    is trimmed for a purpose, and a task outside it needs a different one.
    """
    tasks: dict[str, Any] = {}
    for label, composition in _TASKS.items():
        entry: dict[str, Any] = {
            "tools": composition["tools"],
            "skill_files": composition["skill_files"],
            "server": {},
        }
        for config_name, config in server.items():
            per_tool = config["per_tool_tokens"]
            missing = [t for t in composition["tools"] if t not in per_tool]
            if missing:
                entry["server"][config_name] = {"available": False, "missing_tools": missing}
                continue
            fetched = sum(per_tool[t] for t in composition["tools"])
            entry["server"][config_name] = {
                "available": True,
                "lazy": config["tokens"]["lazy_idle"] + fetched,
                "eager": config["tokens"]["eager_idle"],
                "definitions_fetched": fetched,
            }
        unknown = [f for f in composition["skill_files"] if f not in skill["per_file_tokens"]]
        if unknown:
            raise RuntimeError(f"task {label!r} names skill files that do not exist: {unknown}")
        entry["skill"] = sum(skill["per_file_tokens"][f] for f in composition["skill_files"])
        tasks[label] = entry
    return tasks


def _build_report() -> dict[str, Any]:
    """Measure everything and return the full structured report."""
    import tiktoken

    server = _measure_server()
    skill = _measure_skill()
    return {
        "method": {
            "tokenizer": f"tiktoken {tiktoken.__version__} ({_ENCODING})",
            "serialisation": "model_dump_json(by_alias=True, exclude_none=True) - the wire form a client receives",
            "eager_idle": "tools + prompts + resources + router (the document's definition, omitting templates)",
            "lazy_idle": "router + tool names",
            "caveat": "cl100k_base is not Claude's tokenizer: absolute values +/-10-15%, ratios are the finding",
            "task_compositions": "chosen, not measured - see the script's docstring",
        },
        "server": server,
        "skill": skill,
        "tasks": _measure_tasks(server, skill),
    }


def _render(report: dict[str, Any]) -> str:
    """Render the report as text for a human reader."""
    out: list[str] = []
    out.append("Method")
    for key, value in report["method"].items():
        out.append(f"  {key:<18} {value}")

    out.append("")
    out.append(f"{'Configuration':<34} {'Tools':>6} {'Eager idle':>11} {'Lazy idle':>10} {'Router':>7} {'Names':>6}")
    for config in report["server"].values():
        tokens = config["tokens"]
        out.append(
            f"{config['doc_row'][:33]:<34} {config['counts']['tools']:>6} {tokens['eager_idle']:>11,} "
            f"{tokens['lazy_idle']:>10,} {tokens['router']:>7,} {tokens['tool_names']:>6,}"
        )

    full = report["server"]["full"]["tokens"]
    counts = report["server"]["full"]["counts"]
    out.append("")
    out.append("Full surface, eager idle breakdown")
    out.append(f"  {full['tools']:>7,}  tools ({counts['tools']})")
    out.append(f"  {full['prompts']:>7,}  prompts ({counts['prompts']})")
    out.append(f"  {full['resources']:>7,}  resources ({counts['resources']})")
    out.append(f"  {full['router']:>7,}  router")
    out.append(f"  {full['eager_idle']:>7,}  TOTAL as the document defines it")
    out.append(
        f"  {full['resource_templates']:>7,}  resource templates ({counts['resource_templates']}), which the"
        " document's total omits"
    )
    out.append(f"  {full['eager_idle_with_templates']:>7,}  TOTAL including them")

    definition = report["server"]["full"]["tool_definition_tokens"]
    params = report["server"]["full"]["parameters"]
    disc = report["server"]["full"]["discriminators"]
    pct = lambda part, whole: f"{round(100 * part / whole)}%" if whole else "n/a"  # noqa: E731
    out.append("")
    out.append("Full surface, description and schema statistics")
    out.append(
        f"  tool definition tokens: median {definition['median']:,.0f}, "
        f"range {definition['min']:,}-{definition['max']:,}"
    )
    out.append(
        f"  parameters: {params['total']:,} total, {params['with_default']:,} with a default "
        f"({pct(params['with_default'], params['total'])}), {params['required']:,} required, "
        f"{params['with_enum']:,} with an enum ({pct(params['with_enum'], params['total'])})"
    )
    out.append(
        f"  descriptions naming a sibling: {disc['naming_a_sibling']} of {disc['tools']} "
        f"({pct(disc['naming_a_sibling'], disc['tools'])}), {disc['mean_siblings_named']} on average"
    )
    if disc["naming_none"]:
        out.append(f"  naming none: {', '.join(disc['naming_none'])}")

    skill = report["skill"]
    out.append("")
    out.append("Skill")
    out.append(f"  {skill['tokens']['advertised']:>7,}  advertised (name + description frontmatter)")
    out.append(f"  {skill['tokens']['router']:>7,}  router (SKILL.md)")
    out.append(f"  {skill['tokens']['all_files']:>7,}  every file at once (the worst case)")
    for name, tokens in skill["per_file_tokens"].items():
        if name != "SKILL.md":
            out.append(f"  {tokens:>7,}  {name}")
    out.append(
        f"  heuristics: {skill['heuristics']['prefer_passages']} prefer-this passages, "
        f"{skill['heuristics']['command_lines']} worked command lines (advisory - see --json for definitions)"
    )

    out.append("")
    out.append("Per task (chosen compositions)")
    for label, task in report["tasks"].items():
        out.append(f"  {label}")
        out.append(f"    tools: {', '.join(task['tools'])}")
        out.append(f"    skill files: {', '.join(task['skill_files'])}  ->  {task['skill']:,}")
        for config_name, result in task["server"].items():
            if not result["available"]:
                out.append(f"    {config_name:<10} n/a - not registered: {', '.join(result['missing_tools'])}")
            else:
                out.append(
                    f"    {config_name:<10} lazy {result['lazy']:>7,}   eager {result['eager']:>7,}"
                    f"   (definitions fetched: {result['definitions_fetched']:,})"
                )
    return "\n".join(out)


def main() -> int:
    """Parse arguments, measure, and print the report."""
    parser = argparse.ArgumentParser(
        description="Regenerate the measured figures quoted in MCP_VS_SKILLS.md.  Reports only; never edits.",
    )
    parser.add_argument("--json", action="store_true", help="emit the structured report instead of a table")
    parser.add_argument(
        "--measure-current-surface",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: one worker process per configuration
    )
    args = parser.parse_args()

    if args.measure_current_surface:
        json.dump(_measure_current_surface(), sys.stdout)
        return 0

    report = _build_report()
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=False)
        print()
    else:
        print(_render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
