# Design note: dynamic tool loading via `tools/list_changed`

**Status:** investigation / parked. Not committed. This note exists so the idea and its
open questions survive; it is deliberately *not* a decision to build.

## Problem

The advertised tool surface is large — 161 tools ≈ **40.7k tokens** of schema that a client
pays for on every session before doing anything. (Measured: 46% description prose, 37% param
JSON schema, ~17% structural. The cost is *broad*, not concentrated — it takes 40 tools to
reach half the surface, so trimming a few fat tools barely helps.)

We can disable whole domains today with `DOCKER_MCP_DISABLE`, but that is a **static, operator-set**
switch: enabling a domain you turned off means editing `mcp.json` and restarting the AI client.
That is a poor user experience — functionality the AI *could* use is invisible until a human
reconfigures the server.

## Idea

Start the server with a **lean default surface** (core domains only) and let the **AI itself**
pull in additional domains at runtime, exactly when it decides it needs them — no human, no
restart. The mechanism is MCP's `notifications/tools/list_changed`:

- Register only the core tools at startup (containers, images, client basics, networks, volumes —
  the genuinely-always-used set).
- Expose one always-on meta-tool, `enable_domain(domain)`, whose **description is the menu** —
  it enumerates the gated domains (swarm, services, nodes, configs, secrets, stack, scout,
  plugins, context, buildx, registry, …) so the model knows what it can activate and when.
- When the AI calls `enable_domain("swarm")`, the server registers that domain's tools and emits
  `notifications/tools/list_changed`. A conforming client re-fetches `tools/list` and the swarm
  tools become callable.

This keeps the **native, per-tool, fully-typed** schemas (no freeform dispatcher that throws away
validation/discoverability) while making the *resident* surface small until the session actually
needs more.

### Layering with the existing switches

- `DOCKER_MCP_DISABLE` stays the **hard ceiling** (operator policy: "never expose swarm here").
  Dynamic enable operates strictly *within* what is not hard-disabled — it can never re-enable a
  domain the operator disabled.
- `enable_domain` manages the **floor**: the session's resident surface grows as needed.
- Enable-only / **monotonic** within a session — no re-disable bookkeeping; context grows as the
  work demands, which matches the natural arc of a session.

## SDK feasibility (verified against the installed `mcp` package)

All present:

- `FastMCP.add_tool()` / `remove_tool()` mutate the live tool set.
- `ServerSession.send_tool_list_changed()` sends the notification.
- The server advertises the `tools.listChanged` capability.

One wrinkle: **`add_tool` does not auto-emit** the notification — `enable_domain` must send it
itself (grab the `Context` → `session.send_tool_list_changed()` after registering the domain's
tools). Mechanically straightforward; the existing decorator/registry in `server.py` is most of
the way there (it already records every tool's domain and whether it registered).

## Why this is parked, not committed — the load-bearing risks

1. **No clean feature-detection for client support.** The server can emit `list_changed`, but
   nothing forces a client to re-call `tools/list`. The MCP spec has no client capability flag
   for "I refresh tool lists." A non-refreshing client therefore degrades to **broken** (the
   enabled tools never become callable), not to "full surface." → If built, it must be **opt-in**
   (`DOCKER_MCP_DYNAMIC=1`) with the static full surface remaining the default, plus a documented
   list of known-good clients.

2. **Our best client may already solve this client-side.** Claude Code lists MCP tools by *name
   only* and defers their schemas (loaded on demand via its own tool-search), so for Claude Code
   users the 40.7k surface is largely not resident anyway. Server-side `list_changed` therefore
   mainly benefits clients that **don't** defer (Claude Desktop, Cursor, …) — which are also the
   clients **least likely to honor `list_changed`**. The clients that need it most may support it
   least. Confirm where actual users are before investing.

3. **Discovery gap.** If a user asks for swarm work and the swarm tools aren't resident, the model
   must *know to enable them first*. That responsibility lives entirely in the `enable_domain`
   description (the menu). Needs real-world testing that models reliably reach for it rather than
   reporting "I don't have a tool for that."

4. **Multi-session / transport state.** `add_tool` mutates the **process-global** tool manager.
   For stdio (one client per process — docker-mcp's primary mode) that's exactly right: enabled
   domains persist for the session and only grow. For the HTTP transport, one client enabling a
   domain would leak it to all connected clients. → Gate dynamic mode to stdio, or scope per
   session.

## If we build it — suggested shape

- Opt-in `DOCKER_MCP_DYNAMIC=1`; static full surface stays the default.
- stdio only (Risk 4).
- Enable-only / monotonic (no re-disable).
- Default resident core: containers, images, client basics, networks, volumes (probably compose);
  gate the long tail + buildx + registry.
- `enable_domain(domain)` as the always-on menu tool; `DOCKER_MCP_DISABLE` remains the hard cap.
- Reuse the existing `_tool_registry` / domain tagging in `server.py`; hold gated tools'
  registrations back at import and replay them on `enable_domain`.

## Open questions to resolve before committing

- Which real-world clients honor `tools/list_changed` today? (Claude Desktop, Cursor, Continue,
  Cline, …) — empirical test matrix needed.
- Given Claude Code already defers client-side, what fraction of our users would actually benefit?
- Does the model reliably discover and call `enable_domain` from its description alone?
- Is the per-session footprint win (after Track 1: title-strip + description compression already
  cut ~25%) still large enough to justify the protocol-dependent complexity?

## Relationship to the committed work (Track 1)

Track 1 — strip per-property `title` keys from schemas (−10%, zero semantic loss, client-independent)
and compress the verbose docstrings (~−15%) — is unconditional, benefits *every* client, and needs
no protocol support. It is being done first and de-risks the whole effort: even if dynamic loading
never ships, the surface is already ~25% smaller. This note is the *second*, conditional track.
