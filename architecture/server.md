<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Server internals: registration, tools package, resources and prompts

Deep detail behind the summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing `docker_mcp/server.py` or anything under `docker_mcp/tools/`.

## Entry point

The `docker_mcp` package is the entry point. `docker_mcp/__init__.py` defines `main()` and side-effect-imports the `server` and `tools` submodules (which registers all `@tool()` decorators). `docker_mcp/__main__.py` calls `main()` so `python -m docker_mcp` works; the installed `docker-mcp` console script also targets `docker_mcp:main`.

## Server singleton (`docker_mcp/server.py`)

Instantiates `MCPServer` (from `mcp.server.mcpserver`), exports the `mcp` object, and exports the `tool` and `prompt` registration helpers. **Tool modules import `tool`, prompt modules `prompt`, resource modules `resource`** - never `mcp` itself, which would be a circular import in a tool or prompt module and skips the failure translation everywhere. `tool` and `prompt` gate on `DOCKER_MCP_SERVER_DISABLE`; resource modules take `is_domain_disabled` / `register_resource_domains` alongside, for section gating.

```python
from docker_mcp.server import tool  # tool modules
from docker_mcp.server import prompt  # prompt modules (with domain=...)
from docker_mcp.server import resource  # resource modules
```

`server.py` also owns the central **`TOOL_CATEGORIES`** map (every tool name -> `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`). The `@tool()` decorator uses it to (a) attach `ToolAnnotations` (`title` - mechanically derived from the tool name by `_title_for`, e.g. `container_list` -> "Container List", with a small `_TITLE_ACRONYMS` fixup list so names like `scout_cves`/`scout_sbom` title-case to "Scout CVEs"/"Scout SBOM" rather than "Cves"/"Sbom"; plus `readOnlyHint` / `destructiveHint`, and `idempotentHint` for the prune family) and (b) skip registration entirely under the read-only env switches `DOCKER_MCP_SERVER_READONLY` (only read-only tools) and `DOCKER_MCP_SERVER_NO_DESTRUCTIVE` (everything except destructive). Every registered tool must have a `TOOL_CATEGORIES` entry - `tests/test_server.py` fails if the map and the registered set drift. The `title` annotation exists because some external directories (e.g. the Claude Connectors Directory) mechanically require one on every tool, independent of description quality - see the docstring quality standard in [tool-descriptions.md](tool-descriptions.md), point 2's "annotations don't substitute for prose" is the opposite failure mode, not a contradiction.

**Env-var naming.** All server tunables are namespaced `DOCKER_MCP_SERVER_*` (matching the published package/image name `docker-mcp-server`); the pre-rename `DOCKER_MCP_*` alias spellings were removed in 2.0. Read env vars through `docker_mcp/_env.py` - `read_env("DOCKER_MCP_SERVER_NAME")` or `env_flag(...)`. The helper still supports alias fallbacks (`read_env(canonical, *aliases)` with a one-time stderr deprecation notice) for any future rename; no alias is currently registered. `_env.py` lives at the package root (not under `tools/`) so `server.py` can import it without pulling in `docker_mcp.tools`, which would be a circular import at registration time; `_utils.py` re-exports `env_flag` / `read_env` for tool modules. A new tunable adds a canonical `DOCKER_MCP_SERVER_*` name.

After registering each tool the decorator also calls `_slim_schema` on the tool's advertised `inputSchema` to delete three information-free patterns - together ~18% of the advertised schema tokens: (a) pydantic's `title` annotations (the title-cased field name on every property/`$def`, plus the top-level `<tool>Arguments` title); (b) the `{"type": "null"}` branch of a nullable `anyOf` (an `X | None` param - redundant with the field's optionality, dropped only when a sibling `default` is present so a required nullable can't be misrepresented); and (c) `additionalProperties: true` (the JSON Schema default; a schema-valued `additionalProperties` is kept). It's display-only: call-time validation runs off the tool's separate `fn_metadata`, so the slim never changes behaviour. `tests/test_server.py` asserts none of the three survive on any registered tool.

The decorator also records each tool's **domain** - the leaf of its defining module (`docker_mcp.tools.containers` -> `containers`) - so the orthogonal `DOCKER_MCP_SERVER_DISABLE=<domains>` switch can drop a whole feature area (e.g. `swarm,plugins`) from the registered surface regardless of category. A tool registers only if its category survives the read-only switches *and* its domain is not disabled. `DOCKER_MCP_SERVER_DISABLE` reaches beyond tools: the `prompt(domain=...)` helper skips a disabled domain's prompts, and `resources.py` hides a disabled domain's doc sections - so disabling e.g. `scout` drops its tools, its prompts, and its `docker-docs://scout` sections together. The full picture (every tool's domain/category, plus the `prompts` list and `disabled_doc_sections`) is exposed via `tool_catalog()` and the `docker-mcp://tool-catalog` resource, so the classification is auditable at runtime, not just in the source map.

A handful of tools have **no domain at all** - `_NO_DOMAIN_TOOLS` (today `docs_lookup` and `tool_list`) - because their value isn't tied to any single Docker feature area being enabled or disabled. `_domain_for` returns `None` for these, and `None` short-circuits the `_domain_enabled` check entirely, so `DOCKER_MCP_SERVER_DISABLE` can never drop them (not even by their own name). This mirrors `@prompt(domain=None)`'s identical "cross-cutting, always available" semantics for prompts. They still register/deregister normally under `DOCKER_MCP_SERVER_READONLY`/`_NO_DESTRUCTIVE` based on their own category (a domain-less tool should still be `READ_ONLY` for this to matter in practice).

**Server `instructions` router.** `server.py` also builds the MCPServer `instructions` string - the text a client pre-loads into context alongside the server name and tool names, *before* any per-tool schema. For a lazy-loading client (e.g. Claude Code, which fetches tool schemas on demand) that's the main always-in-context surface we control, so it's written as a **router**, not docs: a per-domain one-liner mapping user vocabulary onto the domain keyword a tool search will hit, plus a few tool-selection caveats. It deliberately does not enumerate tools (that's the `docker-mcp://tool-catalog` resource). It's built dynamically by `build_instructions()` from `_DOMAIN_BLURBS`, emitting a domain's line **only when that domain has a registered tool** - so `DOCKER_MCP_SERVER_DISABLE` / `_READONLY` / `_NO_DESTRUCTIVE` are all honoured through the one registration flag, and the router never advertises a domain whose tools didn't register. `finalize_instructions()` (called from `docker_mcp/__init__.py` *after* every tool module imports) writes the result through to `mcp._lowlevel_server.instructions` - MCPServer's `instructions` is a read-only property whose value is read at `run()` time, so a late write propagates to the MCP initialize handshake; the `_lowlevel_server` reach-in is guarded like `_slim_schema`. **A new tool *domain* needs a `_DOMAIN_BLURBS` entry** or the router silently omits it (`tests/test_server.py` checks the router tracks the registered domain set).

**Failure translation.** The SDK classifies a failure by exception type: a `ToolError`/`ResourceError`
keeps its message and logs at INFO, and anything else is a crash - the client gets `Error executing
tool <name>` or `Error reading resource <uri>`, the original text is withheld, and a traceback is
logged at ERROR. So `@tool()` wraps each registration in `_translate_failures`, which re-raises a
`DockerMcpError` (`docker_mcp/exceptions.py`) as `ToolError` and lets everything else through as the
crash it is. The pair is the whole contract: a refusal the model cannot read is one it will retry,
and a bug dressed as a refusal loses its traceback while putting internals on the wire.

The wrapper is registered but **not** returned - `@tool()` hands the module back the untranslated
callable, because translation is a wire concern. An internal caller wants the project type it can
branch on; only a client needs the SDK's. It composes outside `_wrap_with_host_guard`, so a `(ro)`
refusal is carried across too, and it preserves the signature and sync/async-ness for the reasons
that wrapper already does. `mcp>=2.1.0` is a floor rather than a preference: 2.0.0 flattened even a
deliberately raised `ResourceError`, so the translation cannot work there.

`@resource()` is the same wrapper with `ResourceError`, and covers templates as well as static
resources: the SDK routes both through `read_resource` and classifies a template's own creation
failure identically. It is a floor rather than a preference that this needs `mcp>=2.1.0` - 2.0.0
flattened even a deliberately raised `ResourceError`, so a resource could not explain itself at all.

`TRANSLATES_FAILURES` marks each wrapper so `tests/test_server.py` can walk the built server and fail
any registration that bypassed `@tool()`/`@resource()`. That check is on the server rather than on
the source of the modules that register things today, which is what lets it reach a module nobody
has written yet.

## Tools package (`docker_mcp/tools/`)

Each file maps to one Docker SDK domain (or, for CLI-only and registry-only features, one Docker feature area) and contains `@tool()` decorated functions. `docker_mcp/tools/__init__.py` imports all public modules with `*` so `docker_mcp/__init__.py` only needs `from docker_mcp import tools`. Underscore-prefixed modules (`_cli.py`, `_utils.py`) are private helpers and stay out of the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `docker_mcp/tools/_cli.py` | Cross-platform subprocess helper (private) | - |
| `docker_mcp/tools/_ssh_proxy.py` | Per-call paramiko proxy (dial-stdio) plus the remote-exec and file-staging primitives, so CLI-backed tools reach `ssh://` daemons without a system `ssh` binary (private) | - |
| `docker_mcp/tools/_utils.py` | Shared helpers (private) | - |
| `docker_mcp/tools/_labels.py` | Provenance labels stamped on created resources (private) | - |
| `docker_mcp/tools/system.py` | `DockerClient` - connection and low-level client | docker-py |
| `docker_mcp/tools/containers.py` | Container lifecycle and management | docker-py |
| `docker_mcp/tools/images.py` | Image pull, build, push, inspect | docker-py |
| `docker_mcp/tools/networks.py` | Network create, connect, inspect | docker-py |
| `docker_mcp/tools/volumes.py` | Volume create, list, remove | docker-py |
| `docker_mcp/tools/configs.py` | Swarm configs | docker-py |
| `docker_mcp/tools/nodes.py` | Swarm nodes | docker-py |
| `docker_mcp/tools/plugins.py` | Plugin install and management | docker-py |
| `docker_mcp/tools/secrets.py` | Swarm secrets | docker-py |
| `docker_mcp/tools/services.py` | Swarm services | docker-py |
| `docker_mcp/tools/swarm.py` | Swarm init, join, leave | docker-py |
| `docker_mcp/tools/compose.py` | Docker Compose v2 | `docker compose` CLI via `_cli.py` |
| `docker_mcp/tools/stack.py` | Docker stacks (Compose-on-Swarm) | `docker stack` CLI via `_cli.py` (core CLI, no plugin probe) |
| `docker_mcp/tools/context.py` | Docker CLI contexts | `docker context` CLI via `_cli.py` |
| `docker_mcp/tools/buildx.py` | Buildx / BuildKit (multi-arch builds, imagetools - supersedes `docker manifest` - and build history) | `docker buildx` CLI via `_cli.py` |
| `docker_mcp/tools/scout.py` | Vulnerability scanning, SBOMs, base-image recommendations | `docker scout` CLI via `_cli.py` |
| `docker_mcp/tools/registry.py` | OCI v2 registries + Docker Hub (with 429 retry policy) | HTTPS via `httpx` (no daemon) |
| `docker_mcp/tools/prompts.py` | `@prompt(domain=...)` workflow templates | - |
| `docker_mcp/tools/resources.py` | `@mcp.resource()` doc endpoints | - |

## MCP resources

`docker_mcp/tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data: the Docker SDK for Python documentation under the `docker-docs://` URI scheme, plus `docker-mcp://tool-catalog` (the live tool/domain/category snapshot from `server.tool_catalog()`), `docker-mcp://hosts` (the resolved host registry, mirroring `host_list`), and three families of "watch this specific thing over time" observability resources - **containers** (`docker://containers` index, `docker-logs://{id_or_name}` bounded log tail, `docker-stats://{id_or_name}` computed usage summary), **services** (`docker://services` index, `service-logs://{id_or_name}` bounded log tail, `service-tasks://{id_or_name}` computed task/rollout summary - running vs. desired task counts, failing tasks, and `UpdateStatus.State` if a rolling update is in progress), and **nodes** (`docker://nodes` index only - state/availability/role/reachability per node; deliberately no per-node child resource, since a "tasks on this node" view would need an unbounded fan-out across every service's tasks with no single cheap call, unlike the other two families). **In multi-host mode all three families are host-aware:** the default-host forms become empty-authority (`docker:///containers`, `docker-logs:///{id}`, `service-tasks:///{id}`, ...) and host-qualified variants (`docker://{host}/containers`, `service-logs://{host}/{id}`, ...) are registered alongside, disambiguated by path-segment count; single-host keeps the bare forms unchanged. Registration is gated on `_hosts.is_multi()`, and each index emits child `logs`/`stats`/`tasks` URIs matching its own scheme. Container resources reuse the private `_read_log_tail` / `_read_stats_summary` helpers in `containers.py`; service resources reuse `_read_service_log_tail` / `_read_service_task_summary` in `services.py` (the latter also backs `service_wait`'s `running` mode - see the swarm tool modules in [../AGENTS.md](../AGENTS.md)'s tools table); all refuse at read time when their domain (`containers`/`services`/`nodes`) is disabled (mirroring `get_docs_section`). Each doc section maps to a domain via `_SECTION_DOMAINS` (registered with the server through `register_resource_domains`), so `DOCKER_MCP_SERVER_DISABLE` hides a disabled domain's sections from `docker-docs://contents` and makes `get_docs_section` refuse them. Resources follow the same docstring format as tools and are also star-imported via `docker_mcp/tools/__init__.py`.

`resources.py` also has one `@tool()`: **`docs_lookup(section=None)`** - a tool-callable mirror of the `docker-docs://` family for clients that can't read MCP resources (e.g. Claude Desktop, Cursor). It calls `list_docs_sections()`/`get_docs_section()` directly rather than duplicating their logic, so behaviour (including the per-section domain refusal above) is identical either way. It's one of `_NO_DOMAIN_TOOLS` (see "Server singleton" above) - always registered regardless of `DOCKER_MCP_SERVER_DISABLE` - since looking something up isn't tied to any one feature area. Several tool docstrings (`container_run`/`container_create`/`service_create`'s `extra_kwargs`) and prompts (`lookup_docker_docs`, `verify_docker_method`, `review_dockerfile`, `audit_container_security`) point at it as the fallback when a client can't read the equivalent resource - a new passthrough-heavy tool or docs-reliant prompt should do the same.

`resources.py` carries a second `@tool()`: **`tool_list(domain=None, category=None, keyword=None)`** - a tool-callable mirror of `docker-mcp://tool-catalog`, and the only way to ask questions no per-tool description search can express (which tools are destructive, which accept a `host`, what this server actually registered). It is a thin pass-through to `server.py:query_catalog()`, which reads `_tool_registry` directly - no reach-in to MCPServer's tool manager, because `ToolRecord` captures each tool's docstring summary and parameter names at registration. **Only registered tools are listed**: one dropped by a read-only switch or a disabled domain is absent rather than present-and-flagged, since advertising a capability the server will refuse leaks its existence; `hidden_by_configuration` reports the per-domain counts so the configuration stays auditable without naming them. Like `docs_lookup` it is in `_NO_DOMAIN_TOOLS`, so it still answers when `DOCKER_MCP_SERVER_DISABLE` has dropped every domain - a catalog that vanished exactly when the surface was most reduced would be useless. **A new tool automatically appears in it**; what needs maintaining is the docstring's first line, which is the summary a catalog row carries.

## MCP prompts

`docker_mcp/tools/prompts.py` exposes `@prompt(description=..., domain=...)` templates (the `prompt` helper imported from `docker_mcp.server`, **not** `@mcp.prompt` directly) that return rendered prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Each prompt declares its primary `domain` so `DOCKER_MCP_SERVER_DISABLE` skips it when that domain is off; use `domain=None` for general / cross-domain prompts (doc lookup, prune, disk usage) that should always register. Prompts follow the same docstring format as tools and are star-imported via `docker_mcp/tools/__init__.py`.
