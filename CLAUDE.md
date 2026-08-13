# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **MIRROR RULE (do not skip): `CLAUDE.md` and `.github/copilot-instructions.md` are mirrors.**
> `.github/copilot-instructions.md` drives GitHub Copilot's review of *every* PR, so it must stay
> current. **Any change to project structure, conventions, env vars, the tool/prompt/resource surface,
> or distribution channels MUST update BOTH files in the same change.** When you edit one, edit the
> other. This is the most-forgotten step — treat a docs/architecture change as incomplete until
> `.github/copilot-instructions.md` reflects it.

## Project

`docker-mcp` is a Python MCP server (requires Python >=3.14) managed with `uv` that exposes the Docker SDK for Python as MCP tools. The entry point is the `docker_mcp` package, run with `python -m docker_mcp` or via the installed console script. It is **published to PyPI as `docker-mcp-server`** (the `docker-mcp` name was already taken) and as a container image to GHCR (`ghcr.io/l337-org/docker-mcp-server`), mirrored to Docker Hub (`gavinlucas/docker-mcp-server`) when the opt-in `DOCKERHUB_*` release secrets are configured; the import package stays `docker_mcp` and the repo stays `…/docker-mcp`. Two console scripts are installed — `docker-mcp` and `docker-mcp-server` — both targeting `docker_mcp:main`. A third channel packages the server as a **Claude Desktop Extension (`.mcpb`)** attached to each GitHub Release — see "Desktop Extension (MCPB bundle)" below. A fourth channel (**Homebrew tap**) exists in `L337-org/homebrew-tap` but is currently **paused** — see "Homebrew tap" below.

The `docker` dependency is pulled with its `[ssh]` extra (paramiko), so `DOCKER_HOST=ssh://…` works through a pure-Python transport — no system `ssh` binary, identical on the host and in the container images. docker-py auto-selects paramiko for `ssh://` when present, so there is no transport code to maintain (just the `ssh://` branch in `system._connection_help`). CLI-backed tools (Compose, Stack, Buildx, Scout, Context) shell out to `docker`, which would otherwise use the *system* `ssh` — instead, `_cli.py:run_docker` detects `DOCKER_HOST=ssh://…` and routes the subprocess through a per-call local TCP proxy (`docker_mcp/tools/_ssh_proxy.py`) that opens its own paramiko connection (mirroring docker-py's `SSHHTTPAdapter` defaults) and runs `docker system dial-stdio` over it, so the CLI authenticates identically to the docker-py-backed tools with no system `ssh` binary involved (the one exception being a `ProxyCommand` in `~/.ssh/config` for bastion/jump-host setups, which paramiko runs as an external command — commonly `ssh -W %h:%p ...` — same as it would for the docker-py-backed tools). Where there is no local `docker` binary (or plugin) at all, all of those except the `context_*` tools (which manage *this* host's own CLI contexts) fall back to running the command on the `ssh://` host itself — see "SSH remote-exec fallback" under the CLI shell-out policy. Both the docker-py-backed and CLI-backed SSH connections fall back from IPv6 to IPv4 on any connect failure (not just paramiko's own narrower retry) via `_ssh_proxy.py:connect_socket_with_family_fallback` — see "Client side" under the multi-daemon host registry below.

The repo additionally ships a **Claude Code agent skill** in `skills/l337-docker/`. It is deliberately **not** a fifth channel for the server - it is a CLI-only *alternative* to it, for users who want Docker capability without running a server process. It is attached to each GitHub Release as a single `.tar.gz`. See "Agent skill" below.

## Commands

```bash
# Install dependencies
uv sync

# Run the project
uv run python -m docker_mcp

# Add a dependency
uv add <package>

# Run unit tests (integration tests are excluded by default)
uv run pytest -v

# Run integration tests (require a real Docker daemon)
uv run pytest -m integration -v

# Lint and format
uv run ruff check .
uv run ruff format .

# Type-check
uv run pyright

# Install pre-commit hooks (one-time)
uv run pre-commit install
```

CI (`.github/workflows/premerge.yaml`) enforces `pytest`, `ruff check`, `ruff format --check`, and
`pyright` on every PR and push to main — all via `uv run`, so the dev-group pins in `pyproject.toml` (bumped by
Dependabot's monthly uv pass) are the single tool-version source. The pre-commit hooks are local
hooks running `uv run ruff …` for the same reason, so a synced venv (`uv sync`) is required before
committing. CI installs with `uv sync --locked`, which fails if `uv.lock` disagrees with
`pyproject.toml` instead of silently re-locking — a lockfile-only dependency change (e.g. a
Dependabot lock rewrite that raises a cap pyproject still pins) fails CI rather than landing. A
non-required `Check docs mirror` job flags a PR that edits `CLAUDE.md` or
`.github/copilot-instructions.md` without the other (see the MIRROR RULE above) — it's a prompt to
double-check, not a merge blocker. A second non-required job, `Check for tracker references`, flags
an added line, commit message, PR title or body that names an issue key or links an external
tracker or wiki: neither means anything to a reader of this repository. Also a prompt rather than a
blocker, since a quoted external string could legitimately match.

An `mcp<2` cap existed briefly: mcp 2.0.0 removed `mcp.server.fastmcp`, which `server.py` imported
`FastMCP` from, and an uncapped 2.2.0 shipped dead on arrival at import while every CI job stayed
green, because CI installs `--locked` against a lockfile pinning mcp 1.x; 2.2.1 hotfixed the cap.
`server.py` has since been ported to `mcp.server.mcpserver.MCPServer` and the cap lifted. Rather
than re-adding a cap for the next major (there is no known 3.x incompatibility to guard against),
`tests/test_pyproject_pins.py::test_the_declared_mcp_bound_matches_what_the_code_imports` is a
living guard: it fails whenever the installed mcp stops providing the import path `server.py`
actually uses, with no reliance on remembering to add a cap first. When adding a direct dependency
whose *import surface* we touch, consider whether a cap or a guard like this belongs with it.

**A required `Check fresh resolve still imports` job** (`premerge.yaml`) closes the blind spot that
let the 2.2.0 incident through: every job above installs with `uv sync --locked`, so none of them
ever resolve what a fresh `uvx`/`pip install` actually gets from the bare `pyproject.toml`
specifiers — only the pinned, known-good set in `uv.lock`. This job does, via `uv pip install`
(the pip-compatible interface, which never reads or writes `uv.lock`) into a throwaway venv, then
runs `import docker_mcp` and `docker-mcp-server --version` against that install. It reports a
resolution failure (a specifier no longer satisfiable) and an import failure (resolved fine, but
broke on import) as distinct errors, since they call for different fixes. This is a PR/push gate,
not a schedule — it complements rather than replaces the weekly canary's published-package install
smoke below, which exercises the actual shipped artefact rather than a hypothetical resolve of the
current tree.

A **weekly canary** (`.github/workflows/canary.yaml`, Mondays + dispatch) hunts platform/ecosystem
drift premerge CI can't see: wheels-only (`--only-binary :all:`) dependency resolution for Intel
macOS / ARM macOS / Windows against both the repo `pyproject.toml` and the latest published PyPI
release (the check that would have caught cryptography 49 dropping its x86_64-macOS wheel), plus
real install smokes of the published package on `macos-latest`, `macos-15-intel` (Intel runner
label retires Aug 2027), and `windows-latest` — `import docker_mcp` and `uvx docker-mcp-server
--version`. **PRs into main also run the repo-pyproject resolution leg** (the only part that
exercises PR content), so a dependency change that breaks a platform is caught before merge; the
published-package legs and issue filing stay schedule/dispatch-only. Failures on unattended runs
file a deduplicated `ci-failure` + `wf:canary` issue via `.github/actions/file-failure-issue`.
`main()` handles `--version` (print the installed version, exit) before any daemon/network
contact — the canary's entry-point smoke depends on it.

## Architecture

### Entry point
The `docker_mcp` package is the entry point. `docker_mcp/__init__.py` defines `main()` and side-effect-imports the `server` and `tools` submodules (which registers all `@tool()` decorators). `docker_mcp/__main__.py` calls `main()` so `python -m docker_mcp` works; the installed `docker-mcp` console script also targets `docker_mcp:main`.

### Server singleton (`docker_mcp/server.py`)
Instantiates `MCPServer` (from `mcp.server.mcpserver`), exports the `mcp` object, and exports the `tool` and `prompt` registration helpers. **Tool modules import `tool`; prompt modules import `prompt`** — both gate on `DOCKER_MCP_SERVER_DISABLE` (never import from `mcp` directly in those modules — that would create circular imports). `@mcp.resource()` modules still import `mcp` (plus `is_domain_disabled` / `register_resource_domains` for section gating).

```python
from docker_mcp.server import tool     # tool modules
from docker_mcp.server import prompt   # prompt modules (with domain=...)
from docker_mcp.server import mcp      # resource modules
```

`server.py` also owns the central **`TOOL_CATEGORIES`** map (every tool name → `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`). The `@tool()` decorator uses it to (a) attach `ToolAnnotations` (`title` — mechanically derived from the tool name by `_title_for`, e.g. `container_list` → "Container List", with a small `_TITLE_ACRONYMS` fixup list so names like `scout_cves`/`scout_sbom` title-case to "Scout CVEs"/"Scout SBOM" rather than "Cves"/"Sbom"; plus `readOnlyHint` / `destructiveHint`, and `idempotentHint` for the prune family) and (b) skip registration entirely under the read-only env switches `DOCKER_MCP_SERVER_READONLY` (only read-only tools) and `DOCKER_MCP_SERVER_NO_DESTRUCTIVE` (everything except destructive). Every registered tool must have a `TOOL_CATEGORIES` entry — `tests/test_server.py` fails if the map and the registered set drift. The `title` annotation exists because some external directories (e.g. the Claude Connectors Directory) mechanically require one on every tool, independent of description quality — see the Docstring quality standard below, point 2's "annotations don't substitute for prose" is the opposite failure mode, not a contradiction.

**Env-var naming.** All server tunables are namespaced `DOCKER_MCP_SERVER_*` (matching the published package/image name `docker-mcp-server`); the pre-rename `DOCKER_MCP_*` alias spellings were removed in 2.0. Read env vars through `docker_mcp/_env.py` — `read_env("DOCKER_MCP_SERVER_NAME")` or `env_flag(...)`. The helper still supports alias fallbacks (`read_env(canonical, *aliases)` with a one-time stderr deprecation notice) for any future rename; no alias is currently registered. `_env.py` lives at the package root (not under `tools/`) so `server.py` can import it without pulling in `docker_mcp.tools`, which would be a circular import at registration time; `_utils.py` re-exports `env_flag` / `read_env` for tool modules. A new tunable adds a canonical `DOCKER_MCP_SERVER_*` name.

After registering each tool the decorator also calls `_slim_schema` on the tool's advertised `inputSchema` to delete three information-free patterns — together ~18% of the advertised schema tokens: (a) pydantic's `title` annotations (the title-cased field name on every property/`$def`, plus the top-level `<tool>Arguments` title); (b) the `{"type": "null"}` branch of a nullable `anyOf` (an `X | None` param — redundant with the field's optionality, dropped only when a sibling `default` is present so a required nullable can't be misrepresented); and (c) `additionalProperties: true` (the JSON Schema default; a schema-valued `additionalProperties` is kept). It's display-only: call-time validation runs off the tool's separate `fn_metadata`, so the slim never changes behavior. `tests/test_server.py` asserts none of the three survive on any registered tool.

The decorator also records each tool's **domain** — the leaf of its defining module (`docker_mcp.tools.containers` → `containers`) — so the orthogonal `DOCKER_MCP_SERVER_DISABLE=<domains>` switch can drop a whole feature area (e.g. `swarm,plugins`) from the registered surface regardless of category. A tool registers only if its category survives the read-only switches *and* its domain is not disabled. `DOCKER_MCP_SERVER_DISABLE` reaches beyond tools: the `prompt(domain=...)` helper skips a disabled domain's prompts, and `resources.py` hides a disabled domain's doc sections — so disabling e.g. `scout` drops its tools, its prompts, and its `docker-docs://scout` sections together. The full picture (every tool's domain/category, plus the `prompts` list and `disabled_doc_sections`) is exposed via `tool_catalog()` and the `docker-mcp://tool-catalog` resource, so the classification is auditable at runtime, not just in the source map.

A handful of tools have **no domain at all** — `_NO_DOMAIN_TOOLS` (today `docs_lookup` and `tool_list`) — because their value isn't tied to any single Docker feature area being enabled or disabled. `_domain_for` returns `None` for these, and `None` short-circuits the `_domain_enabled` check entirely, so `DOCKER_MCP_SERVER_DISABLE` can never drop them (not even by their own name). This mirrors `@prompt(domain=None)`'s identical "cross-cutting, always available" semantics for prompts. They still register/deregister normally under `DOCKER_MCP_SERVER_READONLY`/`_NO_DESTRUCTIVE` based on their own category (a domain-less tool should still be `READ_ONLY` for this to matter in practice).

**Server `instructions` router.** `server.py` also builds the MCPServer `instructions` string — the text a client pre-loads into context alongside the server name and tool names, *before* any per-tool schema. For a lazy-loading client (e.g. Claude Code, which fetches tool schemas on demand) that's the main always-in-context surface we control, so it's written as a **router**, not docs: a per-domain one-liner mapping user vocabulary onto the domain keyword a tool search will hit, plus a few tool-selection caveats. It deliberately does not enumerate tools (that's the `docker-mcp://tool-catalog` resource). It's built dynamically by `build_instructions()` from `_DOMAIN_BLURBS`, emitting a domain's line **only when that domain has a registered tool** — so `DOCKER_MCP_SERVER_DISABLE` / `_READONLY` / `_NO_DESTRUCTIVE` are all honored through the one registration flag, and the router never advertises a domain whose tools didn't register. `finalize_instructions()` (called from `docker_mcp/__init__.py` *after* every tool module imports) writes the result through to `mcp._lowlevel_server.instructions` — MCPServer's `instructions` is a read-only property whose value is read at `run()` time, so a late write propagates to the MCP initialize handshake; the `_lowlevel_server` reach-in is guarded like `_slim_schema`. **A new tool *domain* needs a `_DOMAIN_BLURBS` entry** or the router silently omits it (`tests/test_server.py` checks the router tracks the registered domain set).

### Multi-daemon host registry (`docker_mcp/_hosts.py`)

`DOCKER_MCP_SERVER_HOSTS` lets one server manage several daemons in a session (e.g. local dev + remote prod). It's the single source of truth for which daemon(s) to talk to: **when it's set, `DOCKER_HOST` is ignored** (a one-time stderr notice fires when both are set); when unset, the server falls back to today's single-daemon behavior (`DOCKER_HOST`, else auto-discovery). The mcpb bundle exposes only this field.

`_hosts.py` lives at the package root (like `_env.py`, so `server.py` can import it without pulling in `docker_mcp.tools`). It parses the var into a pinned `{label: Host}` registry and owns all host resolution — no docker-py/CLI calls, just env + Docker config-file reads:

- **Grammar.** No `=` in the value → bare single-host shorthand (`ssh://ops@prod(ro)`, `auto`, `local`, or empty → `auto`). With `=` → comma-separated `label=endpoint` list. `endpoint` is the keyword `auto`/`local` or a `unix://`/`tcp://`/`ssh://`/`npipe://` URL, with combinable trailing markers `(ro)` (read-only), `(nd)` (non-destructive — blocks only DESTRUCTIVE calls; `(ro)` already implies it, so combining the two is harmless but redundant) and `(tls=<dir>)` (a tcp+TLS cert dir; **`ca.pem` is required** — the daemon is always verified against it — and `cert.pem`+`key.pem` are optional, present together for mutual TLS or absent for verify-the-daemon-only, e.g. a self-signed daemon pinned via `ca.pem`). **Fail-fast** (`HostConfigError` → stderr + exit non-zero) on duplicate/empty/invalid labels, a missing `=`, an unknown marker, `(tls=)` on a non-tcp endpoint, a missing `ca.pem` or a lone `cert.pem`/`key.pem`, or an unrecognized scheme.
- **`auto`/`local`/`default` are OUR concepts, resolved to concrete URLs by us and pinned at `load()` (startup)** — so the docker-py SDK and the docker-CLI shell-out provably target the *same* daemon for a given label (auditable), and a mid-session `docker context use` can't silently move a label (restart to re-resolve). `auto` = the active CLI context's endpoint (`DOCKER_CONTEXT` / config.json `currentContext` → its `meta.json` Host) else the `local` socket probe; `local` = the platform-local socket (the `_probe_default_socket` candidate list); `default` = the omitted-`host` fallback = the first registry entry, and is **not** a selectable label. These resolution helpers were relocated *from* `system.py` (then `client.py`) into `_hosts.py`.
- **`load()` runs in `docker_mcp/__init__.py` before the tools import** (the `@tool()` decorator and resources read `is_multi()`/`labels()` at registration time) and scrubs whole-value `${...}` placeholders first (`_env.scrub_unresolved_env`, so an mcpb blank field resolves to the default host instead of fail-fasting).

**Per-call host selection (no modal active-host state).** Every daemon-targeting tool declares `host: str | None = None` and threads it to `_get_client(host)` / `run_docker(..., host=host)`; the `@tool()` decorator does the rest (the schema surgery is gated on `_hosts.is_multi()`; the call-time guard on the broader `_host_guard_needed()`):
- **Schema surgery** (`_apply_host_schema`, display-only like `_slim_schema` — call-time validation runs off `fn_metadata`, gated on `_hosts.is_multi()`): single-host → strip the `host` property entirely (footprint-neutral, schema byte-identical to today); multi-host → constrain `host` to an `enum` of the labels and mark it required for writes.
- **Call-time guard** (`_enforce_host_guard`, wrapped onto the tool via `_wrap_with_host_guard`, which preserves the signature and sync/async-ness): in multi-host mode writes require an explicit `host`, unknown labels are rejected, writes to an `(ro)` host are refused, and DESTRUCTIVE calls to an `(nd)` host are refused. Read-only tools and the `_CONNECTION_CONTROL` set (`system_close`/`system_reconnect`/`system_login`/`system_logout`) may omit `host`. The guard is wrapped on whenever there is something to enforce — `_host_guard_needed()` = multi-host **or a single host flagged `(ro)` or `(nd)`**: a lone `(ro)`/`(nd)` host carries no `host` param (schema is still stripped, footprint-neutral) but its refusals still apply, so the per-host markers are honored even in single-host mode (distinct from the `DOCKER_MCP_SERVER_READONLY`/`_NO_DESTRUCTIVE` switches, which drop tools from the surface entirely). A host with both markers is refused by the `(ro)` check first — `(ro)` is strictly stronger, so `(nd)` never fires for it. A single *unrestricted* host wires no guard (today's path). **Excluded** (no `host` param at all): `registry`/`hub_*` (HTTPS, no daemon) and `context` (manages the host's CLI contexts).

**Client side** (`system.py`): a lazy pool `_clients` keyed by label; `_get_client(host)` builds per host with tiered TLS (`(tls=)` cert dir → global `DOCKER_CERT_PATH`/`DOCKER_TLS_VERIFY` → plaintext); the legacy single host (unset var) still goes through `_build_default_client`/`from_env` unchanged, and an explicit host that resolves to the platform default (url `None`) is built without a `base_url` so it never re-reads the ignored ambient `DOCKER_HOST`. **Every `from_env` call passes `use_context=False`** — the reason `docker[ssh]>=7.2.0` is a hard floor rather than a preference. 7.2.0 made `from_env` resolve the active Docker CLI context whenever the environment yields no `base_url`, which is *our* job: `resolve_auto()` reads `DOCKER_CONTEXT` / config.json `currentContext` itself and pins the result at `load()`, so letting docker-py resolve independently would reintroduce the mid-session `docker context use` drift that pinning exists to prevent, and could disagree with the endpoint the CLI shell-out targets for the same label. `tests/test_pyproject_pins.py::test_the_declared_docker_floor_supports_the_kwarg_the_code_passes` fails if the floor stops excluding 7.1.0 or an installed docker-py drops the kwarg. `system_close(host=None)` closes all/one; **`system_reconnect(host=None)` is rebuild-only** — it cannot retarget to an arbitrary URL (to change a daemon, edit the registry and restart), which closes a trust-expansion hole. `_cli.py:_apply_host_env` injects the resolved `DOCKER_HOST` + per-host TLS into the child env for an explicit host (the ssh:// proxy keys off it). `startup_preflight` pings the *default* host but detects self-id against the *self host* (first local-transport entry, which may differ from a remote default), and `guard_not_self(container, host=)` only fires on the self host.

Both `_build_default_client` and `_build_client` run every `ssh://` URL through two docker-py workarounds before handing it to `docker.from_env()`/`docker.DockerClient()`, composed as `_ensure_reachable_family(_ensure_ssh_port(url))`:
- **`_ensure_ssh_port`**: `docker.utils.parse_host()` hardcodes port 22 into the URL *before* `SSHHTTPAdapter._create_paramiko_client` ever runs, so that adapter's own `~/.ssh/config` `Port` fallback (which only fires while the port is still unset) never triggers — a non-22 `Port` in `~/.ssh/config` would otherwise be silently ignored. Splices in the configured port first, reusing the same `~/.ssh/config` lookup `_ssh_proxy.parse_ssh_url` does for the CLI-backed tools.
- **`_ensure_reachable_family`**: `paramiko.SSHClient.connect()` (which `SSHHTTPAdapter` calls internally) resolves both address families but only retries the next one on `ECONNREFUSED`/`EHOSTUNREACH` — a timed-out or black-holed IPv6 route (`ETIMEDOUT`, what a broken IPv6 path actually produces) is never retried, so a host that's perfectly reachable over IPv4 fails outright (`tcp://` doesn't have this problem — `urllib3`'s `create_connection` catches any `OSError` per attempt). Rather than reaching into `SSHHTTPAdapter` internals, this probes every resolved address itself with `_ssh_proxy.connect_socket_with_family_fallback` (the same broad-`OSError`-per-attempt helper `connect_ssh_client` passes to paramiko as `sock=` for the CLI-backed tools) and splices whichever address actually answered into the URL as a literal IP, so docker-py's own connect resolves trivially with nothing left to get wrong. One extra short-lived probe connection per client build, absorbed by the pool (once per host, not per call). A URL that's already a literal address, or where every candidate fails, is left unchanged.

**Surfaces.** `host_list` (READ_ONLY) tool + the `docker-mcp://hosts` resource expose the resolved registry (the default is observable but not selectable). The router (`build_instructions`) adds a multi-host caveat, the container observability resources switch to empty-authority / host-qualified URIs (see MCP resources below), and a `prompt(multi_host=True)` gate plus the `survey_hosts` prompt register only when 2+ hosts are configured. **When changing the host grammar, the env-var precedence, the per-tool/resource/prompt host surface, or the resolution semantics, update this section.**

### Tools package (`docker_mcp/tools/`)
Each file maps to one Docker SDK domain (or, for CLI-only and registry-only features, one Docker feature area) and contains `@tool()` decorated functions. `docker_mcp/tools/__init__.py` imports all public modules with `*` so `docker_mcp/__init__.py` only needs `from docker_mcp import tools`. Underscore-prefixed modules (`_cli.py`, `_utils.py`) are private helpers and stay out of the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `docker_mcp/tools/_cli.py` | Cross-platform subprocess helper (private) | — |
| `docker_mcp/tools/_ssh_proxy.py` | Per-call paramiko proxy (dial-stdio) plus the remote-exec and file-staging primitives, so CLI-backed tools reach `ssh://` daemons without a system `ssh` binary (private) | — |
| `docker_mcp/tools/_utils.py` | Shared helpers (private) | — |
| `docker_mcp/tools/_labels.py` | Provenance labels stamped on created resources (private) | — |
| `docker_mcp/tools/system.py` | `DockerClient` — connection and low-level client | docker-py |
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
| `docker_mcp/tools/buildx.py` | Buildx / BuildKit (multi-arch builds, imagetools — supersedes `docker manifest` — and build history) | `docker buildx` CLI via `_cli.py` |
| `docker_mcp/tools/scout.py` | Vulnerability scanning, SBOMs, base-image recommendations | `docker scout` CLI via `_cli.py` |
| `docker_mcp/tools/registry.py` | OCI v2 registries + Docker Hub (with 429 retry policy) | HTTPS via `httpx` (no daemon) |
| `docker_mcp/tools/prompts.py` | `@prompt(domain=...)` workflow templates | — |
| `docker_mcp/tools/resources.py` | `@mcp.resource()` doc endpoints | — |

### Tests (`tests/`)
Each `docker_mcp/tools/<module>.py` has a corresponding `tests/test_<module>.py`. Tests use pytest. The `tests/__init__.py` is intentionally empty.

`tests/test_docs.py` is not tied to a module: it checks the repo's own prose, today that any line describing the CLI-backed surface enumerates a complete set of those domains (derived from `server.py`'s `_CLI_DOMAINS` / `_REMOTE_EXEC_DOMAINS`, so adding a domain fails until the docs name it). It exists because that one list was wrong in six places across a single feature branch; a line that legitimately carries a marker phrase without enumerating gets a recorded exemption there rather than a looser rule.

`tests/test_skill.py` is likewise not tied to a module: it checks the `skills/l337-docker/` agent skill - frontmatter, cross-references, no orphaned files, licence/attribution, one canonical lowercase provenance label - plus a regression guard per shell/CLI trap the skill hit while being written (`--until now` being rejected, `timeout(1)` absent on macOS, `status` read-only in zsh, `compose ps` being NDJSON while `compose ls` is a JSON array). Its parity checks derive from `tool_catalog()`, so adding a tool or domain fails until `MCP_VS_SKILLS.md` is updated. Its negative guards deliberately police **fenced code blocks only** - prose has to be able to name an invalid form in order to warn about it.

`tests/integration/test_remote_exec.py` is the exception to needing a daemon: it exercises the SSH remote-exec fallback against a real remote host and is gated on `DOCKER_MCP_TEST_SSH_HOST=ssh://user@host` (deliberately *not* in the `DOCKER_MCP_SERVER_*` namespace — it configures the test, not the server), overriding the autouse daemon fixture and skipping cleanly when unset. Everything it runs remotely is read-only.

**An integration test skips only for a cause it can name.** `tests/integration/conftest.py` provides `fail_unless_environmental` (for a CLI result) and `fail_unless_environmental_error` (for a raised exception): each matches the failure against `_ENVIRONMENTAL_SIGNALS` and **fails** when nothing matches, on the basis that an unrecognised failure is a product defect until shown otherwise. Both search stdout *and* stderr, because the CLI and its plugins are inconsistent about which stream an error lands on. This exists because three `scout` tools shipped passing a `--format` flag that `docker scout quickview`/`recommendations` do not define: the tests skipped on *any* non-zero exit, so on a fully working machine the defect was reported as "unreachable (offline or auth required?)" and nobody looked. A precondition check (plugin absent, daemon not a swarm manager) is a legitimate skip and stays; a blanket `except Exception` around a fixture is not. Widen `_ENVIRONMENTAL_SIGNALS` rather than the skip.

`tests/integration/test_cli_flag_drift.py` is the mechanical counterpart: it reads the argv each CLI-backed tool builds and asserts every literal flag still appears in the installed CLI's `--help`. A mocked unit test cannot catch this class of drift, because what changed is outside the mock. It carries a `_MIN_FUNCTIONS_CHECKED` floor so an extractor that stops matching fails loudly rather than passing while verifying nothing, and it deliberately has no exemption list: a flag surviving only as a hidden alias (as `buildx create --config` did before becoming `--buildkitd-config`) gets migrated, not exempted.

`tests/integration/` holds tests that hit a real Docker daemon. `tests/integration/conftest.py` auto-marks every test in the directory with `@pytest.mark.integration` (excluded by default via `addopts = "-m 'not integration'"` in `pyproject.toml`) and provides an autouse `skip_if_no_daemon` fixture so the suite skips cleanly when no daemon is reachable. Run with `uv run pytest -m integration`.

### Container image (`Dockerfile`)

An additional distribution channel alongside uvx-from-git (which is unchanged). One multi-stage `Dockerfile` builds variants via build args (`INSTALL_CLI`, `INSTALL_SCOUT`, `DISABLE_DOMAINS`): `full` (docker CLI + compose + buildx + scout) and `no-scout` (sets `DOCKER_MCP_SERVER_DISABLE=scout` so the absent-plugin scout tools don't register) are **published to GHCR (and mirrored to Docker Hub when the Hub secrets are set) on each GitHub Release** — the same tags on each registry (`full` → `:latest`/`:<version>`, `no-scout` → `:no-scout`/`:<version>-no-scout`); `lite` (`INSTALL_CLI=0`, docker-py SDK only — CLI domains degrade via `has_plugin()`) is buildable but not published. `.github/workflows/images.yaml` builds+measures on PRs/pushes to main; the `images` job in `.github/workflows/publish.yaml` (see "Release pipeline" below) pushes multi-arch images on a GitHub Release — always to GHCR, and mirrored to Docker Hub (`gavinlucas/docker-mcp-server`, plus a `DOCKERHUB.md`→Hub-description sync — a slim container-focused readme, since the full `README.md` exceeds Hub's 25 KB cap) when the opt-in `DOCKERHUB_USER`/`DOCKERHUB_TOKEN` secrets are set (the Hub token needs `read/write/delete` scope or the description PATCH 403s); with them unset, only GHCR is pushed so a release never fails for lack of Hub credentials. Two container-aware guards live behind `_utils.in_container()` (true when `/.dockerenv` exists or `DOCKER_MCP_SERVER_IN_CONTAINER=1`, set in the image) and are **inert on the host install**:

- **Filesystem guard** (`_utils.py`): `assert_host_writable` (hooked into `stream_to_file`) refuses a host-file write (`dest_path` / `container_archive_get_to_file`) to a path that isn't a host bind mount (silent loss on `--rm`); `host_read_path` enriches the "missing file" case on reads. `_host_backed` parses `/proc/self/mountinfo`.
- **Self-termination guard** (`system.py`): `startup_preflight()` (called from `main()`) pings the default daemon, prints OS-aware socket hints to **stderr** (never stdout — that's the stdio channel) on failure, and pins the server's own container id (detected against the *self host* — the first local-transport entry, which can differ from a remote default); `guard_not_self(container, host=)` then makes the destructive container-lifecycle tools refuse to act on self, **only when the call targets the self host** (override: `DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1`).

### Desktop Extension (MCPB bundle)

A third distribution channel (alongside uvx/PyPI and the container images) for one-click install in Claude Desktop. The repo root carries the bundle sources: `manifest.json` (the MCPB manifest, `manifest_version` 0.4, `server.type: "uv"`), `mcpb_run.py` (the bundle entry point — `from docker_mcp import main; main()`, kept at the root so `import docker_mcp` resolves however the host's managed `uv` lays out `sys.path`), `.mcpbignore` (trims the packed bundle to source + `pyproject.toml` + `uv.lock` + `README.md`/`LICENSE` + `manifest.json`/`mcpb_run.py` + `assets/`), and `assets/icon.png` (512×512). Because it's a `uv`-type bundle, the host resolves dependencies from `pyproject.toml` at install time — there's no vendored venv, so the bundle stays ~250 KB and cross-platform. `manifest.json`'s `user_config` block renders the install-dialog fields and maps them to env: `DOCKER_MCP_SERVER_HOSTS` (the single host field — `DOCKER_HOST` is deliberately **not** exposed; the bare-value shorthand keeps the simple one-daemon case a one-liner) plus the `DOCKER_MCP_SERVER_READONLY` / `_NO_DESTRUCTIVE` / `_DISABLE` switches (the container-only `_ALLOW_SELF_TERMINATE` is deliberately **not** exposed — the bundle never runs containerized). The `manifest.json` `version` is kept in step with `pyproject.toml` (`tests/test_pyproject_pins.py` asserts it), but the `mcpb` job in `.github/workflows/publish.yaml` rewrites it from the release tag at pack time so it can't drift; that job packs the `.mcpb` with `npx @anthropic-ai/mcpb` and attaches it (plus a `.sha256` recording the bare filename, so `sha256sum -c` works on downloads) to each GitHub Release. `scripts/build-mcpb.sh` is the **developer-only** local equivalent of that pack step (packs to `dist/` with an auto-incrementing name) — for smoke-testing a bundle in Claude Desktop. It stamps a **dev version** derived from `pyproject.toml` plus the git HEAD — `<version>-dev.<short-commit>[.dirty]` (`.dirty` = uncommitted changes at pack time) — so a local bundle is never mistaken for a release in Claude Desktop's extension list and is traceable to the commit it was built from despite having no tag; the stamp is written into `manifest.json` only for the duration of the pack and restored by an `EXIT` trap, so the working tree is left unmodified. It is deliberately **not** wired into CI, which uses the workflow above. `PRIVACY.md` (a no-telemetry/no-backend statement) is referenced by the manifest's `privacy_policies` and summarized in the README. **When the manifest's tool/env surface or the bundled file set changes, update this section.**

### Homebrew tap (`L337-org/homebrew-tap`) — PAUSED

The infrastructure exists (`scripts/docker-mcp-server.rb.tpl`, `.github/workflows/publish-homebrew.yaml`, `L337-org/homebrew-tap`) but the **release trigger is disabled** pending resolution of a Homebrew dylib linkage issue: pre-built PyPI wheels for `pydantic_core` lack `-headerpad_max_install_names`, so Homebrew's post-install relocation step fails to rewrite the `@rpath` ID (the binary's Mach-O header has no room for the longer absolute path). The workflow is `workflow_dispatch`-only until a fix is found. The channel is not advertised in the README. To re-enable: add `release: types: [published]` back to `publish-homebrew.yaml`'s `on:` block and re-add the Homebrew section to the README.

### MCP Registry (`server.json`)

A discovery listing in the official MCP Registry (`registry.modelcontextprotocol.io`), which stores only **metadata** pointing at the artifacts the other channels already publish — so it's not a fourth artifact, just an index entry covering all of them. `server.json` (repo root, server name `io.github.L337-org/docker-mcp-server`) declares three package types in one entry: `pypi` (`docker-mcp-server`), `oci` (the GHCR image), and `mcpb` (the release `.mcpb`). The `registry` job in `.github/workflows/publish.yaml` stamps the tag version and the published `.mcpb`'s `fileSha256` into `server.json`, authenticates via **GitHub OIDC** (`id-token: write`, no stored secret), and runs `mcp-publisher publish`. The registry verifies we own each listed package by matching a marker against `server.json`'s `name`, so three markers must stay equal to it: the `<!-- mcp-name: … -->` comment in `README.md` (PyPI long-description), the `io.modelcontextprotocol.server.name` image label in the `images` job (OCI), and the `.mcpb` URL (must contain "mcp") + its hash (MCPB). The job runs `needs: [pypi, images, mcpb]`, so all three markers are live before it starts — its short retries only absorb registry-side read lag. **If `server.json`'s name/markers or the package set change, update this section.**

### Agent skill (`skills/l337-docker/`)

A **Claude Code agent skill** that drives Docker entirely through the `docker` CLI - the CLI-only alternative to running this MCP server, for simple cases where a `docker` binary is all the user wants. It is a peer of the server, not a channel for it, and nothing in `docker_mcp/` imports or depends on it.

**Layout** mirrors the server's own three-layer discovery model, for the same reason (a client pre-loads only the top layer): `SKILL.md` is the always-loaded **router** - preflight, seven always-apply rules, the JSON-parsing contract, daemon targeting - and it maps into `reference/` (11 files, one per domain group: how to do a thing) and `workflows/` (5 files: multi-step procedures ported from the server's MCP prompts). The comparison/coverage record lives at the repo root as `MCP_VS_SKILLS.md`, not inside the skill - it documents both systems, and the skill ships standalone (its `SKILL.md` links to it on GitHub). `LICENSE` is a copy of the repo's MIT licence, so the skill stands alone when downloaded. It follows the open [Agent Skills specification](https://agentskills.io/specification), so it is not Claude-specific: GitHub Copilot reads the same `SKILL.md` from `.github/skills`, `.agents/skills` or the same `.claude/skills` directory. `tests/test_skill.py` asserts the spec's `name`/`description` constraints, so an edit cannot quietly make it Claude-only.

**Guards are conventions here, not enforcement** - this is the structural difference from the server and must not be papered over when describing it. `DOCKER_MCP_SERVER_READONLY`, per-host `(ro)`/`(nd)` markers and the self-termination guard refuse at the server's call boundary; a skill has no call boundary, so its rules are instructions a model can skip or lose to a prompt injection in a container log. The skill says so in `MCP_VS_SKILLS.md` ("Structural gaps") and points at the server for cases needing a hard guarantee. It also declares **no tool-permission frontmatter** (`tools` / `disallowedTools`, nor the `allowed-tools` spelling slash commands use), deliberately: an allow-list covering `docker` would pre-approve `docker rm -f` and defeat the skill's own confirmation rule, leaving the host's permission prompts as the only thing that actually refuses.

**Provenance labels** are `l337-docker-skill.managed=true` / `.created`, a separate footprint from the server's `docker-mcp-server.managed=true` - the skill must never stamp the server's label, since that would attribute its resources to the server. The prefix is **lowercase deliberately**: Docker label keys are case-sensitive, so a mixed-case key published in one place and filtered in lowercase in another matches nothing silently, and a teardown would report a clean daemon while resources remained.

**Tests**: `tests/test_skill.py` (static, in the default CI gate) and `tests/integration/test_skill.py` (daemon-backed, in the `integration-tests` job). Two of the static checks derive from `tool_catalog()`, so **adding a tool or a domain fails CI until `MCP_VS_SKILLS.md`'s per-domain counts are updated** - the same "derive, don't copy" shape as `tests/test_docs.py`. The integration tests **extract the shell snippets out of the skill's own markdown and execute them**, so a documented snippet cannot drift from what the CLI actually does; a pasted copy in the test would have kept passing when the real one broke, which is exactly how a zsh-fatal `local status=…` survived a round of manual verification.

**Distribution**: the `skill` job in `.github/workflows/publish.yaml` packs it (see "Release pipeline"). There is no official packaging format for a skill - unlike `.mcpb` for MCP servers, a plain directory is the unit - so the archives are just that directory. Native alternative not currently used: a Claude Code **plugin marketplace** (`.claude-plugin/marketplace.json` + `/plugin marketplace add`), which would give `/plugin install` and auto-updates.

**When changing the skill's structure, its provenance label, or the server's tool/prompt surface, update this section and `MCP_VS_SKILLS.md` together.**

### Release pipeline (`.github/workflows/publish.yaml`)

All publishing runs through one workflow on each **published GitHub Release** (nothing publishes without that human step): `preflight` → `pypi` ∥ `images`(→`dockerhub-description`) ∥ `mcpb` → `registry` → `verify`, with `skill` running in parallel from `preflight` and joining at `verify`, and a `notify` job on failure. The `skill` job is deliberately **not** in `registry`'s `needs` (the registry entry doesn't reference the skill archives) but **is** in `verify`'s and `notify`'s, or verify could race ahead of it and a pack failure would file no issue. `preflight` resolves the tag once for every job (each checks out **the tag ref**, not the event SHA) and fails fast if the tag disagrees with the committed `pyproject.toml` / `manifest.json` / `uv.lock` self-entry versions - preventing a split-brain release (PyPI ships the pyproject version; every other channel ships the tag version). It also refuses to run when `github.repository_owner` isn't `L337-org`, so a release published from a fork (or mis-timed around a repo transfer) fails before anything ships rather than half-publishing. `verify` confirms every channel actually serves the version (PyPI JSON API, both GHCR tags, the `.mcpb` asset + checksum, the registry listing, and the skill archive - checksum plus an extraction asserting it really is rooted at `l337-docker/SKILL.md` and stamps this release's `VERSION`) so a partial release is loud. `notify` files (or comments on) a deduplicated issue labeled `ci-failure` + `wf:release` via the composite action `.github/actions/file-failure-issue` - one open issue per failure stream; a scheduled Claude responder routine polls `ci-failure` issues. Re-run a failed release via `workflow_dispatch` with the tag: every job is idempotent (`skip-existing` on PyPI, re-pushed image tags, `--clobber` on the `.mcpb` and the skill archive, duplicate-tolerant registry publish). The skill archive is additionally **byte-reproducible** (sorted entries, zeroed ownership, normalised mtimes, `gzip -n`), so a re-run republishes an identical checksum rather than a new one. **PyPI Trusted Publishing pins the workflow *filename*** - the trusted publisher on PyPI must name `publish.yaml`; if the `pypi` job fails OIDC, that registration is missing. Version-bump checklist for a release: edit `pyproject.toml` + `manifest.json`, run `uv lock`, merge, then publish a GitHub Release whose tag matches (`.github/release.yml` - GitHub's release-*notes* config, a different file - groups the generated notes, Dependabot PRs under "Dependencies"). `publish-homebrew.yaml` (paused, dispatch-only) stays separate. **When the pipeline's job graph, markers, or re-run semantics change, update this section.**

## Conventions

- **Tool naming convention (2.0, permanent):** every tool is named `<management-command>_<verb>`, anchored
  to the docker CLI's management-command structure (`docker container ls` → `container_list`), with
  **long-form verbs** (`list`/`remove`/`inspect` — never `ls`/`rm`/`get`), applied uniformly even where the
  CLI lacks the long alias. Singular prefixes (`container_`, not `containers_`); read-only fetches may be
  noun-form (`container_logs`, `registry_tags`). Names never encode the backend (SDK vs CLI). Identifier
  params use one rule: `id_or_name` (daemon objects addressable by either), `name`/`names` (name-only
  resources: volumes, contexts, plugins, stacks, builders), `repository` (remote repo refs); durations are
  `timeout_seconds`. `tests/test_naming.py` enforces all of this (approved prefixes, banned short forms and
  1.x spellings, canonical shared-param descriptions) — a violating tool fails CI.
- New Docker functionality goes in the matching `docker_mcp/tools/<domain>.py` file, not in a new file.
- Every new `docker_mcp/tools/` file must be imported in `docker_mcp/tools/__init__.py` (private `_*.py` helpers excluded).
- Every new `docker_mcp/tools/<module>.py` must have a matching `tests/test_<module>.py`.
- Tool functions are decorated with `@tool()` (imported from `docker_mcp.server`) and must have a `TOOL_CATEGORIES` entry in `docker_mcp/server.py`.
- **Bound any externally-sourced bytes before buffering/parsing them, and parse safely.** CLI output is capped in `run_docker` (`MAX_CLI_OUTPUT_BYTES`); registry HTTP bodies are streamed and capped at `_MAX_RESPONSE_BYTES` (`registry.py`) since registries are agent-pointed/untrusted (the cap is on the *decoded* stream, so it also stops a decompression bomb). New code that reads an untrusted file or network body must apply a similar bound. Always `json.loads` (never `eval`); if YAML is ever parsed in Python use `yaml.safe_load` only — today no module parses YAML (Compose YAML is read by the `docker` CLI, not us).
- **A parameter whose legal values are a genuinely closed set is typed `Literal[...]`, not `str`.**
  Pydantic turns that into an `enum` in the advertised `inputSchema` (`_slim_schema` preserves it,
  proven by a test) so an out-of-set value fails validation before anything executes, and the
  docstring then drops the value list rather than repeating it. Verify the set against a primary
  source - the subcommand's own `--help`, or docker-py's documented value list - never from
  memory: `docker scout cves --only-severity CRITICAL` exits 0 reporting no vulnerabilities on an
  image with three critical CVEs, and the daemon records an unrecognised `--scope` verbatim, so a
  wrong value is not reliably rejected by Docker itself. Where the set is *not* provably closed
  (compose validates `--protocol` not at all; buildx drivers are pluggable; docker-py documents no
  values for `isolation`), leave it a `str` and record why at the parameter, so a later pass does
  not re-propose it. `tests/test_server.py::test_closed_value_sets_are_advertised_as_enums` pins
  the current set.
- **The `@tool()` decorator is generic (`def tool[F: Callable[..., Any]](...) -> Callable[[F], F]`)
  so pyright checks arguments at every tool call site**, in tests and at internal callers alike.
  Annotating it as a bare `Callable` erases the parameter list and silently disables that checking
  everywhere: a wrong type, an unknown keyword and a value outside a `Literal` all passed the gate
  until this was fixed. `tests/test_server.py::test_pyright_still_checks_arguments_at_tool_call_sites`
  runs pyright over those three deliberate errors, so it also fails if only the return annotation is
  loosened while the type parameter stays. A test that must pass a deliberately invalid value marks
  that one call `# pyright: ignore[reportArgumentType]` with a reason, rather than being softened to
  a legal one.
- Line length limit: 120 characters (enforced by ruff and flake8).

## Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) via `docker_mcp/tools/_labels.py`, so the agent/operator can later enumerate that footprint — the `managed_only=True` arg on `container_list` / `network_list` / `volume_list` / `service_list`, or `--filter label=docker-mcp-server.managed=true`. The `prune_managed` prompt tears down only the managed footprint. Stamping is **on by default** and additive (a caller-supplied label always wins on a key collision); `DOCKER_MCP_SERVER_NO_LABELS=1` turns it off. The prefix is the bare project name (deliberately not reverse-DNS) and is a single constant in `_labels.py`.

When adding a new create tool that accepts a `labels` dict, route it through `_labels.py:with_provenance(labels, "<tool_name>")` (it accepts the dict/list/None shapes the SDK accepts and returns `None` — feed it through `drop_none` — when stamping is off and the caller passed nothing). The seven stamped creators today are `container_run`, `container_create`, `network_create`, `volume_create`, `service_create` (service-level `labels` only, not `container_labels`), `config_create`, `secret_create`. **Image builds are intentionally NOT stamped** — a build label changes the resulting image digest. Compose/stack containers (created via CLI shell-out) are also unstamped, as is `plugin_create` — the Engine's plugin-create call accepts no labels field, so there is nothing to stamp. The rule is conditional on the tool *accepting a `labels` dict*: a creator with nowhere to put a label is an expected exception, not an oversight, and should say so in its docstring. New `managed_only`-style label filters go through `_labels.py:managed_filter`.

## CLI shell-out policy

Any tool that wraps a `docker` CLI feature (Compose, Stack, Buildx, Scout, Context) MUST go through `docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper centralizes:

- Binary resolution via `shutil.which` (handles `docker` vs `docker.exe` on Windows).
- `shell=False` always; argv as a list so PowerShell/cmd/zsh quoting cannot bite us.
- UTF-8 decoding with `errors="replace"` (Windows defaults to cp1252 otherwise).
- Output byte cap with a `truncated` flag in the result.
- `creationflags=CREATE_NO_WINDOW` on Windows so child processes don't flash a console.
- Environment scrubbed to an allow-list (DOCKER_HOST, DOCKER_CONTEXT, PATH, etc., plus Windows-specific keys for credential helpers).
- Plugin availability probing via `has_plugin(name)` / `require_plugin(name)`.

Multi-platform notes for new shell-out tools:

- **Never** pass `shell=True`, never construct paths by string concatenation, never expand `~` or globs yourself (use `Path.expanduser()` / `Path.glob()`).
- Always pass an explicit `timeout=` to `run_docker`; pick a generous ceiling for long-running ops (build/pull at 1800s) and a short one for queries.
- Don't hardcode binary paths — Docker Desktop on Mac, Windows, and Linux all install `docker` differently; `shutil.which` is the only safe lookup.

### SSH remote-exec fallback (CLI-backed tools with no local `docker`)

The dial-stdio proxy above still needs a local `docker` binary to *point* at the remote daemon. With none (a machine with SSH access to real Docker hosts and no Docker installed), CLI-backed tools fail at `_resolve("docker")` before any host logic runs — while every docker-py-backed tool works fine against that same host. So when the target host resolves to `ssh://` **and** nothing local can serve the call, the command runs **on that host**, over the same paramiko machinery (`_ssh_proxy.py:run_remote_exec`, a sibling of the dial-stdio proxy): `PosixDialect` wraps the argv in an `sh -c` script that enforces the timeout itself (`sh`/`sleep`/`kill`/`mktemp` only — GNU `timeout` is Linux-only) and reports `124` when it kills, which maps back to `subprocess.TimeoutExpired` so both backends raise the same exception; stdout and stderr are drained concurrently, since an unread stream would block the remote command rather than truncate its output.

Two rules for tool modules:

- **One decision point per module.** Its shared `_run_*` wrapper calls `_cli.py:should_remote_exec(host, plugin=...)` (`plugin=None` for a core-CLI subcommand such as `docker stack`) and routes to `remote_exec_cli(host, args, timeout=)`, which returns the same `CliResult` — so the error conventions below need no remote branch — else falls through to `require_plugin` + `run_docker` unchanged. Never probe ad hoc in a tool body.
- **A fallback, never a preference.** A usable local CLI always wins, so the credentials, filesystem, and buildx state a call sees change only when there is no local alternative. A non-`ssh://` host is never eligible (no shell to run anything on) and `remote_exec_cli` raises rather than degrading if called for one.

Consequences to state in the docstring of any tool that gains this path: the command runs as the **remote** SSH user, so registry credentials come from *its* `~/.docker/config.json` (`system_login` talks to the daemon through the SDK and never writes the remote CLI's config); `stdin`/`extra_env` are rejected rather than dropped; and the remote must be POSIX (`uname -s` allow-list — sshd inside WSL is Linux and accepted; a Windows-side cmd/PowerShell sshd, or MSYS/Cygwin on a Windows host, is refused by name). **Wired in: every CLI-backed domain except `context`** — `scout`, `compose`, `stack`, `buildx`. `context.py` is excluded permanently, not pending: its tools manage *this* host's CLI context registry, which a remote host knows nothing about.

- **`scout`** takes image references and reads nothing locally, so it goes through `remote_exec_cli`. The one exception, `scout_compare`'s `to` (which may name a local directory or archive), is refused when the value exists locally rather than being resolved against the remote filesystem.
- **`compose`** reads its files from a working directory, so `_run_compose` routes through `remote_stage_and_exec`, which stages `project_dir` (or the server's cwd) and runs there. `compose_list` is the exception — it asks the daemon, so it takes the exec-only path. **`compose_cp` is bespoke** rather than going through `remote_stage_and_exec` (one side of the copy is a local path outside the compose file's working directory, which that helper has no concept of relaying): it stages `project_dir` the same way, then uses `_split_cp_arg` (mirroring `docker compose cp`'s own `splitCpArg`, verified against `docker/compose`'s `pkg/compose/cp.go`) to identify which of `source`/`dest` is the container reference. A local source is staged like `project_dir` (`stage_file`/`stage_tree`); a local destination gets a fresh path from `RemoteStagingSession.reserve_path()` for the remote `docker compose cp` to write into, fetched back via `RemoteStagingSession.fetch_path()` once the copy succeeds (`test -d` decides file vs. directory; a directory is packed remotely with `tar`, downloaded, and extracted locally with `tarfile`'s `filter="data"`, which is what actually stops an escaping member — the same size/count bounds `_enforce_stage_limits` applies to uploads apply to a fetch, checked against the packed archive's `stat` size before download and its member count after). Because the real CLI always executes the copy, every parameter (`--all`, `--index`, `files`) carries over unchanged; the one gap with no remote equivalent is a container→host copy whose local destination already exists, which is refused rather than merged into or overwritten, since only this host knows that state. When neither or both sides look like `SERVICE:PATH`, nothing beyond `project_dir` is staged and the call passes through unchanged, so the real remote CLI's own validation error surfaces exactly as it would locally. `unix://`/`tcp://`+TLS with no local plugin are not covered (no shell to run the CLI on) and still raise via `require_plugin`, whose message now also names pointing the host at an `ssh://` endpoint via `DOCKER_MCP_SERVER_HOSTS` as an alternative to installing the plugin — a message shared by `compose`/`buildx`/`scout`, all of which support this fallback.
- **`stack`** gained the shared `_run_stack` wrapper it lacked (5 direct `run_docker` calls before). Only `stack_deploy` reads local files, so `stage_cwd=True` is explicit there and the four query/removal tools take the exec-only path — the distinction is *not* inferred from `cwd is None`, since a Compose-style `cwd=None` still means "stage the server's cwd".
- **`buildx`** splits three ways. The query/lifecycle tools are exec-only; `buildx_bake` stages a working directory (`stage_cwd=True`, `path_values=files` passed **explicitly** — bake appends caller-supplied target names, so an argv scan for `-f` could match one, and those targets now go through `safe_positional` too); `buildx_create --config` and `buildx_imagetools_create --file` stage only the files they name (`stage_cwd=False`, which stages every existing `path_values` entry individually and gives the remote command no cwd). **`buildx_build` is bespoke**: it drives a session via `remote_cli_session` / `run_in_session`, because its context needs `.dockerignore`-aware tarring and its `--build-context` / `--secret` values carry paths *inside* composite `key=value` tokens (rewritten via `_spec_component` / `_replace_spec_component`, flag-anchored rather than whole-token). The context is staged only when it names an **existing local directory** — the inverse test to recognising URL syntax, which cannot be done reliably from the string — so a Git/HTTP context passes through untouched. **buildx resolves `--file` against the CLI's working directory, not the context** (verified empirically; the pre-2.2.0 docstring claimed the opposite), so the remote command gets **no working directory** and every path rewritten here is absolute — an in-context Dockerfile becomes `<staged context>/<relative>` via `RemoteStagingSession.join`, one outside it (or beside a URL context) is staged on its own. Running in the staged context instead would let a relative `--file` the local CLI cannot find resolve *there*, so the same build would fail locally and succeed remotely. `buildx_build` **refuses** (RuntimeError, before connecting) a filesystem `dest=` in `output`/`cache_to`, a local `src=` in `cache_from`, and any `ssh=` — each would resolve on the remote machine, losing the output, writing cache to the wrong disk, silently building uncached (a missing local cache import is non-fatal to BuildKit), or reading the remote user's agent. `dest=-` is stdout and passes.

The `instructions` router carries the cross-cutting half of this (it changes which host a call runs on, before any schema is fetched): `build_instructions` appends the fallback sentence naming the domains in `_REMOTE_EXEC_DOMAINS` that actually registered, so a surface of only `context` never advertises it.

**Path tokens are reconciled with the staged copy** (`_cli.py:_reconcile_path_tokens`), which matters because the argv still contains *local* paths after the tree is copied: a relative in-tree path is left alone (the remote cwd is that tree), an absolute in-tree path is rewritten relative (the local absolute path names nothing remotely, even though the file was copied), and an out-of-tree path is staged separately and rewritten to where it landed. The values to reconcile are recovered from the argv with `flag_values(args, "-f")` / `"-c"` rather than threaded from each tool — one producer, one consumer, instead of twenty call sites each able to forget. A staged out-of-tree file that itself references relative paths (an override Compose file with its own `build:` context) will not find them; the remote CLI reports that, since following those references would mean parsing the file.

**Staging local files** (`_ssh_proxy.py:remote_staging_session`) is the counterpart for a command whose arguments name files that exist *here* — Compose files, a bake file, a build context. It holds one connection, one `mktemp -d` root (mode 0700, named `docker-mcp-server.stage.*` so an abandoned one is attributable and tellable apart from the watchdog's marker files, which share the project prefix), and teardown in a `finally`, so success, an exception and a timeout all remove it — only a dropped transport can leave one behind. `stage_tree` / `stage_file` / `stage_build_context` each land in their own numbered subdirectory (same-basename items cannot collide, and a staged tree never contains the archive it arrived in) and return an absolute remote path to use as a `cwd` or an argument; `session.exec(...)` then runs on that same connection. `stage_tree` is an unfiltered tar → SFTP → remote `tar -xf`, because nothing can tell which files a Compose file will reference; `stage_build_context` reuses docker-py's `tar`/`exclude_paths` (a soft dependency on undocumented-but-stable internals, flagged at the import) so `.dockerignore` applies exactly as it would for an SDK build, and measures only the *included* set. Both refuse an oversized payload (`_MAX_STAGE_BYTES` / `_MAX_STAGE_FILES`) with a message naming a way out, checked lazily so a pathological tree is refused rather than walked. Two host requirements beyond the POSIX shell: a working `tar`, and an SFTP subsystem on the **same filesystem** as the exec channel — verified once per session by stat-ing the directory exec just created, which catches a Windows sshd shelling into `wsl.exe` (exec lands in WSL, SFTP stays Windows-side) and a chrooted/jailed SFTP. That check is deliberately staging-only, since exec-only tools work fine against such a host. The consumers are listed above.

### CLI error convention (intentional, do not "unify")

CLI-backed tools follow one of two error styles depending on what they return:

- **Action tools** (`compose_up`, `buildx_build`, `context_use`, …) return the raw `{"returncode", "stdout", "stderr", "truncated"}` dict from `CliResult.to_dict()` and never raise on a non-zero exit — stderr is informative, and the agent decides what to do with a failure.
- **Parsed-query tools** (`context_list`, `buildx_list`, `buildx_du`, `compose_list`) return a parsed list/dict and therefore *cannot* return a useful partial result on failure — they raise `RuntimeError` via `_cli.py:raise_on_cli_failure`. (`compose_ps` and `compose_config` are the sanctioned hybrids: they return the parsed view plus `raw` — callers want both the structure and the CLI's warnings — and never raise.)

New CLI tools should pick the style matching their return shape rather than mixing them.

## Checklist when adding a new tool module

When you add a new `docker_mcp/tools/<domain>.py` (especially for CLI features outside docker-py), update **all** of these — easy to miss:

1. `docker_mcp/tools/__init__.py` — star-import.
2. `docker_mcp/server.py` — add a `TOOL_CATEGORIES` entry for every new tool (`READ_ONLY` / `MUTATING` / `DESTRUCTIVE`); `tests/test_server.py` fails otherwise. Tool names must follow the naming convention above (`tests/test_naming.py` fails otherwise; a new domain also adds its prefix to that test's approved list). A new module is a new **domain**, so also add a `_DOMAIN_BLURBS` entry (one-line router blurb) or the `instructions` router will silently omit it.
3. `tests/test_<domain>.py` — unit tests using mocks.
4. `tests/integration/test_<domain>.py` — at least one happy-path test against a real daemon (override `skip_if_no_daemon` if the module doesn't need one).
5. `docker_mcp/tools/prompts.py` — at least one `@mcp.prompt()` template using the new tools.
6. `docker_mcp/tools/resources.py` — add a section under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` pointing at the authoritative docs.
7. `README.md` — append to "What the agent can do" and "Security considerations" (the latter only if a new class of risk is introduced).
8. `SECURITY.md` — only if a new class of risk is introduced beyond what's already documented.
9. `MCP_VS_SKILLS.md` - bump the domain's tool count (and add a `### <domain> (n)` heading for a new domain), plus a `reference/` entry covering the CLI equivalent. `tests/test_skill.py` derives the expected counts from `tool_catalog()`, so this fails CI until done; a genuinely uncoverable tool is recorded under "Structural gaps" rather than left out silently.
10. `.github/copilot-instructions.md` - **mirror the architecture/convention change here too** (see the MIRROR RULE at the top of this file); it drives Copilot's review of every PR.

### Tool function format

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

(`mcp_example` and `mcp_example_bulk` are illustrative only and exist nowhere in the repo — in a
real docstring the discriminator must name an actually-registered sibling tool.)

- One-line summary sentence, then a blank line
- `args:` section lists each parameter as `name - description`. Do **not** repeat the parameter's
  type — the type annotation already lands in the tool's `inputSchema`, which the client sees
  alongside the description, so a `name: type - ...` form just duplicates it as prose tokens. (The
  `returns:` line keeps its type, since the return shape is not in the input schema.)
- `returns:` line documents the return type and what it contains
- Keep descriptions terse: state every functional fact (defaults, accepted formats/values, return
  keys, important caveats) but cut redundancy and verbose phrasing. The docstring is the entire
  tool `description` the client pays tokens for on every session.

#### Docstring quality standard

Tool descriptions are scored externally on Glama's six-dimension Tool Definition Quality rubric
(<https://glama.ai/mcp/servers/L337-org/docker-mcp/score>): Purpose Clarity 25%, Usage Guidelines
20%, Behavioral Transparency 20%, Parameter Semantics 15%, Conciseness & Structure 10%, Contextual
Completeness 10%. Four rounds of cleanup (#97, the 2.0 rename, #129, and the 2026-07 bottom-20
pass — see [[project_glama_docstring_quality]] in memory) all chased the same failure: docstrings
that state *what* the tool does but never *when to use it over its neighbors*, plus `args:` /
`returns:` lines that merely restate the schema. The standard below exists to prevent a fifth
round. It applies to **every `@tool()` docstring added or modified in a PR** (a ratchet — untouched
legacy docstrings are cleaned opportunistically, not churned):

1. **Summary = specific verb + resource**, with the distinguishing trait up front when a sibling
   could be confused ("Send a signal to a running container (default SIGKILL — immediate, no
   graceful shutdown)").
2. **A usage-guidance paragraph (1–5 sentences between the summary and `args:`) is required for
   every tool, not just complex ones.** Every tool in a 150+-tool server has neighbors. It must
   carry:
   - at least one *discriminator* naming the sibling tool(s) an agent could reach for instead and
     when to prefer which (`container_stop` vs `container_kill` vs `container_restart`;
     `service_ps` vs `stack_ps` vs the `service-tasks://` resource);
   - preconditions in prose (swarm manager only, plugin required, container must be
     running/paused);
   - side effects and destructive/irreversible behavior in prose — the scorer explicitly discounts
     `readOnlyHint`/`destructiveHint` annotations as a substitute for description text;
   - for CLI-backed tools, the error style ("does not raise on a non-zero CLI exit — inspect
     `returncode`/`stderr`" vs "raises `RuntimeError` on CLI failure"). Don't overpromise "never
     raises" — a missing binary/plugin or a subprocess timeout still raises even in action tools.
   Scale it to the tool: a trivial read-only tool needs one discriminator sentence, not five.
3. **Every `args:` line adds semantics the schema cannot carry**: format, accepted values/ranges,
   defaults, units, and interactions with other parameters. A line that echoes the parameter name
   ("name - The volume name") scores 2/5 on the rubric — say what makes a value valid or how it
   behaves ("name - The volume name (volumes have no separate id)"). Canonical shared-param
   prefixes in `tests/test_naming.py` still apply — append tool-specific detail after the
   canonical prefix rather than rewording it.
4. **`returns:` names the shape, not just the type.** There is no output schema, so this line is
   all an agent gets. For computed or partial returns, name the load-bearing keys (`{"Titles",
   "Processes"}`; `{"LayersSize", "Images", "Containers", "Volumes", "BuildCache"}`). For a full
   engine inspect document, do NOT enumerate an arbitrary subset of its hundreds of keys — say
   what document it is ("full inspect payload, as `docker inspect`"), optionally plus the one or
   two keys a caller typically wants from it. What stays banned is the shapeless "dict - The X's
   attrs", which identifies neither form.
5. **Front-load and stay terse** — the description is paid for in every session's context; every
   sentence must earn its place.
6. **Verify every factual claim** against the live docker-py docs / Engine API spec per the Docker
   SDK Policy below — an unverified claim about identifier semantics (e.g. "name or id" for a
   resource actually addressed by name only) is exactly the kind of thing PR review catches late.

**Division of labor across the three discovery layers.** For a lazy-loading client (e.g. Claude
Code), tool schemas load on demand; what is always in context is only (1) the **tool names** and
(2) the **`instructions` router**. Docstrings are layer (3): a deferred tool cannot be invoked
without fetching its definition, so the docstring is guaranteed to be read at the moment of
choice — typically side by side with the sibling definitions the same search returned, which is
where the item-2 discriminators do their work. Consequences: pre-fetch discoverability belongs to
the naming convention and the router, not the docstring (don't pad docstrings with search
keywords); a *cross-domain* selection caveat that must be visible before any schema is fetched
(e.g. "prefer `dest_path` for large output") goes in the router's caveat list (`_DOMAIN_BLURBS` /
`build_instructions()` — see "Server singleton" above), while *sibling-level* discriminators stay
in docstrings and are never duplicated into the router; and sibling references must use the exact
tool name (`container_kill`, never "the kill tool") — lazy clients keyword-search descriptions, so
exact names double as retrieval anchors that surface the right alternative even when the agent
searched for the wrong one.

Self-check before opening the PR: read the docstring as an agent holding 150+ tool names and
nothing else — could you pick this tool over its neighbors and call it correctly on the first try?
**Write it this way the first time a tool is added or its behavior changes** — don't wait for a
future Glama pass to catch it.

### MCP resources

`docker_mcp/tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data: the Docker SDK for Python documentation under the `docker-docs://` URI scheme, plus `docker-mcp://tool-catalog` (the live tool/domain/category snapshot from `server.tool_catalog()`), `docker-mcp://hosts` (the resolved host registry, mirroring `host_list`), and three families of "watch this specific thing over time" observability resources — **containers** (`docker://containers` index, `docker-logs://{id_or_name}` bounded log tail, `docker-stats://{id_or_name}` computed usage summary), **services** (`docker://services` index, `service-logs://{id_or_name}` bounded log tail, `service-tasks://{id_or_name}` computed task/rollout summary — running vs. desired task counts, failing tasks, and `UpdateStatus.State` if a rolling update is in progress), and **nodes** (`docker://nodes` index only — state/availability/role/reachability per node; deliberately no per-node child resource, since a "tasks on this node" view would need an unbounded fan-out across every service's tasks with no single cheap call, unlike the other two families). **In multi-host mode all three families are host-aware:** the default-host forms become empty-authority (`docker:///containers`, `docker-logs:///{id}`, `service-tasks:///{id}`, …) and host-qualified variants (`docker://{host}/containers`, `service-logs://{host}/{id}`, …) are registered alongside, disambiguated by path-segment count; single-host keeps the bare forms unchanged. Registration is gated on `_hosts.is_multi()`, and each index emits child `logs`/`stats`/`tasks` URIs matching its own scheme. Container resources reuse the private `_read_log_tail` / `_read_stats_summary` helpers in `containers.py`; service resources reuse `_read_service_log_tail` / `_read_service_task_summary` in `services.py` (the latter also backs `service_wait`'s `running` mode — see Swarm tools below); all refuse at read time when their domain (`containers`/`services`/`nodes`) is disabled (mirroring `get_docs_section`). Each doc section maps to a domain via `_SECTION_DOMAINS` (registered with the server through `register_resource_domains`), so `DOCKER_MCP_SERVER_DISABLE` hides a disabled domain's sections from `docker-docs://contents` and makes `get_docs_section` refuse them. Resources follow the same docstring format as tools and are also star-imported via `docker_mcp/tools/__init__.py`.

`resources.py` also has one `@tool()`: **`docs_lookup(section=None)`** — a tool-callable mirror of the `docker-docs://` family for clients that can't read MCP resources (e.g. Claude Desktop, Cursor). It calls `list_docs_sections()`/`get_docs_section()` directly rather than duplicating their logic, so behavior (including the per-section domain refusal above) is identical either way. It's one of `_NO_DOMAIN_TOOLS` (see "Server singleton" above) — always registered regardless of `DOCKER_MCP_SERVER_DISABLE` — since looking something up isn't tied to any one feature area. Several tool docstrings (`container_run`/`container_create`/`service_create`'s `extra_kwargs`) and prompts (`lookup_docker_docs`, `verify_docker_method`, `review_dockerfile`, `audit_container_security`) point at it as the fallback when a client can't read the equivalent resource — a new passthrough-heavy tool or docs-reliant prompt should do the same.

`resources.py` carries a second `@tool()`: **`tool_list(domain=None, category=None, keyword=None)`** — a tool-callable mirror of `docker-mcp://tool-catalog`, and the only way to ask questions no per-tool description search can express (which tools are destructive, which accept a `host`, what this server actually registered). It is a thin pass-through to `server.py:query_catalog()`, which reads `_tool_registry` directly — no reach-in to MCPServer's tool manager, because `ToolRecord` captures each tool's docstring summary and parameter names at registration. **Only registered tools are listed**: one dropped by a read-only switch or a disabled domain is absent rather than present-and-flagged, since advertising a capability the server will refuse leaks its existence; `hidden_by_configuration` reports the per-domain counts so the configuration stays auditable without naming them. Like `docs_lookup` it is in `_NO_DOMAIN_TOOLS`, so it still answers when `DOCKER_MCP_SERVER_DISABLE` has dropped every domain — a catalog that vanished exactly when the surface was most reduced would be useless. **A new tool automatically appears in it**; what needs maintaining is the docstring's first line, which is the summary a catalog row carries.

### MCP prompts

`docker_mcp/tools/prompts.py` exposes `@prompt(description=..., domain=...)` templates (the `prompt` helper imported from `docker_mcp.server`, **not** `@mcp.prompt` directly) that return rendered prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Each prompt declares its primary `domain` so `DOCKER_MCP_SERVER_DISABLE` skips it when that domain is off; use `domain=None` for general / cross-domain prompts (doc lookup, prune, disk usage) that should always register. Prompts follow the same docstring format as tools and are star-imported via `docker_mcp/tools/__init__.py`.

## Docker SDK Policy

**Before writing or modifying any code that calls the Docker SDK (`docker` package), you MUST run `/docker-sdk` (or `/docker-sdk <topic>`) to:**
1. Verify exact method signatures from the live Docker SDK for Python documentation
2. Confirm parameter names and return types before writing code
3. Never use a `docker` module method that has not been confirmed in the docs

Do not assume any method exists because it sounds plausible. If you cannot confirm it from the documentation, say so and do not use it.

When the high-level SDK has no method for an operation (e.g. swarm node removal, service rollback), drop to the low-level **`APIClient` via `_get_client().api`** — its methods (`remove_node`, `update_service`, `inspect_service`, …) are documented at https://docker-py.readthedocs.io/en/stable/api.html and must be verified the same way. Prefer the high-level object API when it exists; reach for `client.api` only for the gaps.

**Verify against the source, not just the rendered docs, and treat "the method exists" as separate from "the method works."** The rendered docs show a method's docstring, not the URL it builds, so a method can be documented, importable, and still non-functional. `plugin_push` is the standing example: docker-py's `Plugin.push()` / `APIClient.push_plugin()` both POST to `/plugins/{name}/pull`, a route the Engine does not define (push is `POST /plugins/{name}/push`), so they 404 against every daemon — a copy-paste from the pull method present since 2017 and still in `main`, surviving because upstream has no test covering it. Where a *documented* method is provably broken, the fallback ladder is: (1) another public SDK path, (2) the correct endpoint through docker-py's private request helpers (`_url`/`_post`/`_raise_for_status`/`_stream_helper`), resolved via `getattr` and guarded so a missing helper raises an actionable message rather than an `AttributeError` — the same treatment as `system_logout`'s `api._auth_configs` reach-in and `stage_build_context`'s use of docker-py's `tar`/`exclude_paths`, (3) a CLI shell-out, if the domain is already CLI-backed. Each such reach-in must name the bug and the escape hatch in the tool's docstring, so it can be removed when upstream fixes it.

**Rungs (2) and (3) are not an agent's call to make.** Rung (1) is ordinary work; anything below it leaves the supported surface, so an agent — a routine, or anyone implementing from an audit issue — must **stop, write up what it found and why the public path fails, and escalate for a human decision** rather than implementing it. This is not a formality: it is how `plugin_push` was actually settled. The draft-PR routine hit the broken method, declined to reach past the public SDK, shipped the rest, and *documented the omission*; that write-up is what prompted the investigation that found the real endpoint and the human judgement to take it. A routine that had "helpfully" hand-rolled the call instead would have made a trust decision nobody asked it to make, and one that silently dropped the candidate would have buried it. **Document and escalate is the correct behavior, not a failure to finish the job** — a parked candidate with a clear rationale is a better outcome than an autonomous workaround. Sign-off is needed once, when the reach-in is introduced: the ones listed here are already blessed, so touching or refactoring them later needs no fresh approval.

**Confirm the real route from the Engine API spec (`moby/moby`'s `api/swagger.yaml`) before writing a hand-built path, and note which kind of bet it is** — the two are not equivalent risks:

- **A published endpoint reached through private client plumbing** (what `plugin_push` does): `POST /plugins/{name}/push` is in the Engine API spec and is what `docker plugin push` itself calls, so the *contract* is stable and unlikely to move; only docker-py's `_url`/`_post` internals are unofficial, which is what the `getattr` guard covers. This is the acceptable shape.
- **An endpoint that is not in the spec at all** is a different proposition — no compatibility promise, no deprecation cycle, nothing to pin the behavior. Do not use one, even guarded, without explicit human sign-off recorded in the PR; never on an agent's own initiative.

### SDK audit exclusions (deliberate non-candidates)

A recurring cloud routine audits the docker-py surface for coverage gaps and for low-level
`client.api.*` calls a high-level method could replace. The decisions below were made once, on the
merits, and are **not** to be re-proposed — a periodic audit has no memory of last time, so without
this list it re-files the same rejected candidates forever. Removing an entry is a real decision;
say why. **Anything deliberately not wrapped, or wrapped in an unobvious way, belongs here.**

- **`Plugin.push()` / `APIClient.push_plugin()`** — never migrate `plugin_push` onto these. They are
  broken upstream (wrong URL, see above); our hand-built endpoint call is the working path, not
  technical debt to be tidied away. Revisit only if upstream fixes the URL, at which point the
  reach-in should be replaced by the public method.
- **`Container.attach` / `attach_socket` / `resize`** — real methods, deliberately unwrapped: they
  open an interactive bidirectional stream/TTY, which does not fit a request/response tool call.
  `container_exec` covers scripted one-shot execution.
- **`service_rollback`'s `api.inspect_service` + `api.update_service`** — stays low-level
  permanently. The high-level `Service`/`ServiceCollection` expose no `rollback`.
- **`system_logout`'s `api._auth_configs`** — stays low-level permanently. There is no `logout`
  anywhere in the SDK and no server-side session to end, so there is nothing to migrate to.

The audit must also **check the latest published docker-py, not the pinned one**: `uv.lock` is
routinely behind what `pyproject.toml`'s floor lets a fresh `uvx`/`pip install` resolve, so auditing
the installed tree alone misses whatever published users are already running. And it should flag
**deprecated** surface we still depend on, not only missing coverage — e.g. `image_prune_builds`'s
`keep_storage`, which the Engine renamed `reserved-space` at API v1.48 — so a migration happens on
our schedule rather than when removal breaks us.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html  
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html  
Docker SDK GitHub: https://github.com/docker/docker-py
