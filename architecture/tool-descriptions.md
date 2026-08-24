<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Tool docstrings: format and quality standard

Deep detail behind the summaries in [../CLAUDE.md](../CLAUDE.md).
Read this before changing any `@tool()` docstring.

## Tool function format

All `@tool()` functions must follow this exact docstring format:

```python
from docker_mcp.server import tool


@tool()
def mcp_example(name: str):
    """
    Say hello to someone by name.

    Use it for a single greeting; use `mcp_example_bulk` to greet many names in one call.
    Read-only, no side effects.

    args: name - The name to say hello to (any non-empty string)
    returns: str - The greeting
    """
    return f"Hello, {name}!"
```

(`mcp_example` and `mcp_example_bulk` are illustrative only and exist nowhere in the repo - in a
real docstring the discriminator must name an actually-registered sibling tool.)

- One-line summary sentence, then a blank line
- `args:` section lists each parameter as `name - description`. Do **not** repeat the parameter's
  type - the type annotation already lands in the tool's `inputSchema`, which the client sees
  alongside the description, so a `name: type - ...` form just duplicates it as prose tokens. (The
  `returns:` line keeps its type, since the return shape is not in the input schema.)
- `returns:` line documents the return type and what it contains
- Keep descriptions terse: state every functional fact (defaults, accepted formats/values, return
  keys, important caveats) but cut redundancy and verbose phrasing. The docstring is the entire
  tool `description` the client pays tokens for on every session.

### Docstring quality standard

Tool descriptions are scored externally on Glama's six-dimension Tool Definition Quality rubric
(<https://glama.ai/mcp/servers/L337-org/docker-mcp/score>): Purpose Clarity 25%, Usage Guidelines
20%, Behavioral Transparency 20%, Parameter Semantics 15%, Conciseness & Structure 10%, Contextual
Completeness 10%. Those six are Glama's own dimension names, quoted verbatim - "Behavioral
Transparency" keeps its American spelling because renaming an external rubric's dimension would
misname it. Four rounds of cleanup (#97, the 2.0 rename, #129, and the 2026-07 bottom-20
pass - see [[project_glama_docstring_quality]] in memory) all chased the same failure: docstrings
that state *what* the tool does but never *when to use it over its neighbours*, plus `args:` /
`returns:` lines that merely restate the schema. The standard below exists to prevent a fifth
round. It applies to **every `@tool()` docstring added or modified in a PR** (a ratchet - untouched
legacy docstrings are cleaned opportunistically, not churned):

1. **Summary = specific verb + resource**, with the distinguishing trait up front when a sibling
   could be confused ("Send a signal to a running container (default SIGKILL - immediate, no
   graceful shutdown)").
2. **A usage-guidance paragraph (1-5 sentences between the summary and `args:`) is required for
   every tool, not just complex ones.** Every tool in a 150+-tool server has neighbours. It must
   carry:
   - at least one *discriminator* naming the sibling tool(s) an agent could reach for instead and
     when to prefer which (`container_stop` vs `container_kill` vs `container_restart`;
     `service_ps` vs `stack_ps` vs the `service-tasks://` resource);
   - preconditions in prose (swarm manager only, plugin required, container must be
     running/paused);
   - side effects and destructive/irreversible behaviour in prose - the scorer explicitly discounts
     `readOnlyHint`/`destructiveHint` annotations as a substitute for description text;
   - for CLI-backed tools, the error style ("does not raise on a non-zero CLI exit - inspect
     `returncode`/`stderr`" vs "raises `RuntimeError` on CLI failure"). Don't overpromise "never
     raises" - a missing binary/plugin or a subprocess timeout still raises even in action tools.
   Scale it to the tool: a trivial read-only tool needs one discriminator sentence, not five.
3. **Every `args:` line adds semantics the schema cannot carry**: format, accepted values/ranges,
   defaults, units, and interactions with other parameters. A line that echoes the parameter name
   ("name - The volume name") scores 2/5 on the rubric - say what makes a value valid or how it
   behaves ("name - The volume name (volumes have no separate id)"). Canonical shared-param
   prefixes in `tests/test_naming.py` still apply - append tool-specific detail after the
   canonical prefix rather than rewording it.
4. **`returns:` names the shape, not just the type.** There is no output schema, so this line is
   all an agent gets. For computed or partial returns, name the load-bearing keys (`{"Titles",
   "Processes"}`; `{"LayersSize", "Images", "Containers", "Volumes", "BuildCache"}`). For a full
   engine inspect document, do NOT enumerate an arbitrary subset of its hundreds of keys - say
   what document it is ("full inspect payload, as `docker inspect`"), optionally plus the one or
   two keys a caller typically wants from it. What stays banned is the shapeless "dict - The X's
   attrs", which identifies neither form.
5. **Front-load and stay terse** - the description is paid for in every session's context; every
   sentence must earn its place.
6. **Verify every factual claim** against the live docker-py docs / Engine API spec per the Docker
   SDK Policy below - an unverified claim about identifier semantics (e.g. "name or id" for a
   resource actually addressed by name only) is exactly the kind of thing PR review catches late.

**Division of labor across the three discovery layers.** For a lazy-loading client (e.g. Claude
Code), tool schemas load on demand; what is always in context is only (1) the **tool names** and
(2) the **`instructions` router**. Docstrings are layer (3): a deferred tool cannot be invoked
without fetching its definition, so the docstring is guaranteed to be read at the moment of
choice - typically side by side with the sibling definitions the same search returned, which is
where the item-2 discriminators do their work. Consequences: pre-fetch discoverability belongs to
the naming convention and the router, not the docstring (don't pad docstrings with search
keywords); a *cross-domain* selection caveat that must be visible before any schema is fetched
(e.g. "prefer `dest_path` for large output") goes in the router's caveat list (`_DOMAIN_BLURBS` /
`build_instructions()` - see "Server singleton" above), while *sibling-level* discriminators stay
in docstrings and are never duplicated into the router; and sibling references must use the exact
tool name (`container_kill`, never "the kill tool") - lazy clients keyword-search descriptions, so
exact names double as retrieval anchors that surface the right alternative even when the agent
searched for the wrong one.

Self-check before opening the PR: read the docstring as an agent holding 150+ tool names and
nothing else - could you pick this tool over its neighbours and call it correctly on the first try?
**Write it this way the first time a tool is added or its behaviour changes** - don't wait for a
future Glama pass to catch it.
