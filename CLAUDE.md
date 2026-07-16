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

The `docker` dependency is pulled with its `[ssh]` extra (paramiko), so `DOCKER_HOST=ssh://…` works through a pure-Python transport — no system `ssh` binary, identical on the host and in the container images. docker-py auto-selects paramiko for `ssh://` when present, so there is no transport code to maintain (just the `ssh://` branch in `system._connection_help`). CLI-backed tools (Compose, Buildx, Context, Scout) shell out to `docker`, which would otherwise use the *system* `ssh` — instead, `_cli.py:run_docker` detects `DOCKER_HOST=ssh://…` and routes the subprocess through a per-call local TCP proxy (`docker_mcp/tools/_ssh_proxy.py`) that opens its own paramiko connection (mirroring docker-py's `SSHHTTPAdapter` defaults) and runs `docker system dial-stdio` over it, so the CLI authenticates identically to the docker-py-backed tools with no system `ssh` binary involved (the one exception being a `ProxyCommand` in `~/.ssh/config` for bastion/jump-host setups, which paramiko runs as an external command — commonly `ssh -W %h:%p ...` — same as it would for the docker-py-backed tools).

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
double-check, not a merge blocker.

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
Instantiates `FastMCP`, exports the `mcp` object, and exports the `tool` and `prompt` registration helpers. **Tool modules import `tool`; prompt modules import `prompt`** — both gate on `DOCKER_MCP_SERVER_DISABLE` (never import from `mcp` directly in those modules — that would create circular imports). `@mcp.resource()` modules still import `mcp` (plus `is_domain_disabled` / `register_resource_domains` for section gating).

```python
from docker_mcp.server import tool     # tool modules
from docker_mcp.server import prompt   # prompt modules (with domain=...)
from docker_mcp.server import mcp      # resource modules
```

`server.py` also owns the central **`TOOL_CATEGORIES`** map (every tool name → `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`). The `@tool()` decorator uses it to (a) attach `ToolAnnotations` (`readOnlyHint` / `destructiveHint`, plus `idempotentHint` for the prune family) and (b) skip registration entirely under the read-only env switches `DOCKER_MCP_SERVER_READONLY` (only read-only tools) and `DOCKER_MCP_SERVER_NO_DESTRUCTIVE` (everything except destructive). Every registered tool must have a `TOOL_CATEGORIES` entry — `tests/test_server.py` fails if the map and the registered set drift.

**Env-var naming.** All server tunables are namespaced `DOCKER_MCP_SERVER_*` (matching the published package/image name `docker-mcp-server`); the pre-rename `DOCKER_MCP_*` alias spellings were removed in 2.0. Read env vars through `docker_mcp/_env.py` — `read_env("DOCKER_MCP_SERVER_NAME")` or `env_flag(...)`. The helper still supports alias fallbacks (`read_env(canonical, *aliases)` with a one-time stderr deprecation notice) for any future rename; no alias is currently registered. `_env.py` lives at the package root (not under `tools/`) so `server.py` can import it without pulling in `docker_mcp.tools`, which would be a circular import at registration time; `_utils.py` re-exports `env_flag` / `read_env` for tool modules. A new tunable adds a canonical `DOCKER_MCP_SERVER_*` name.

After registering each tool the decorator also calls `_slim_schema` on the tool's advertised `inputSchema` to delete three information-free patterns — together ~18% of the advertised schema tokens: (a) pydantic's `title` annotations (the title-cased field name on every property/`$def`, plus the top-level `<tool>Arguments` title); (b) the `{"type": "null"}` branch of a nullable `anyOf` (an `X | None` param — redundant with the field's optionality, dropped only when a sibling `default` is present so a required nullable can't be misrepresented); and (c) `additionalProperties: true` (the JSON Schema default; a schema-valued `additionalProperties` is kept). It's display-only: call-time validation runs off the tool's separate `fn_metadata`, so the slim never changes behavior. `tests/test_server.py` asserts none of the three survive on any registered tool.

The decorator also records each tool's **domain** — the leaf of its defining module (`docker_mcp.tools.containers` → `containers`) — so the orthogonal `DOCKER_MCP_SERVER_DISABLE=<domains>` switch can drop a whole feature area (e.g. `swarm,plugins`) from the registered surface regardless of category. A tool registers only if its category survives the read-only switches *and* its domain is not disabled. `DOCKER_MCP_SERVER_DISABLE` reaches beyond tools: the `prompt(domain=...)` helper skips a disabled domain's prompts, and `resources.py` hides a disabled domain's doc sections — so disabling e.g. `scout` drops its tools, its prompts, and its `docker-docs://scout` sections together. The full picture (every tool's domain/category, plus the `prompts` list and `disabled_doc_sections`) is exposed via `tool_catalog()` and the `docker-mcp://tool-catalog` resource, so the classification is auditable at runtime, not just in the source map.

A handful of tools have **no domain at all** — `_NO_DOMAIN_TOOLS` (today just `docs_lookup`) — because their value isn't tied to any single Docker feature area being enabled or disabled. `_domain_for` returns `None` for these, and `None` short-circuits the `_domain_enabled` check entirely, so `DOCKER_MCP_SERVER_DISABLE` can never drop them (not even by their own name). This mirrors `@prompt(domain=None)`'s identical "cross-cutting, always available" semantics for prompts. They still register/deregister normally under `DOCKER_MCP_SERVER_READONLY`/`_NO_DESTRUCTIVE` based on their own category (a domain-less tool should still be `READ_ONLY` for this to matter in practice).

**Server `instructions` router.** `server.py` also builds the FastMCP `instructions` string — the text a client pre-loads into context alongside the server name and tool names, *before* any per-tool schema. For a lazy-loading client (e.g. Claude Code, which fetches tool schemas on demand) that's the main always-in-context surface we control, so it's written as a **router**, not docs: a per-domain one-liner mapping user vocabulary onto the domain keyword a tool search will hit, plus a few tool-selection caveats. It deliberately does not enumerate tools (that's the `docker-mcp://tool-catalog` resource). It's built dynamically by `build_instructions()` from `_DOMAIN_BLURBS`, emitting a domain's line **only when that domain has a registered tool** — so `DOCKER_MCP_SERVER_DISABLE` / `_READONLY` / `_NO_DESTRUCTIVE` are all honored through the one registration flag, and the router never advertises a domain whose tools didn't register. `finalize_instructions()` (called from `docker_mcp/__init__.py` *after* every tool module imports) writes the result through to `mcp._mcp_server.instructions` — FastMCP's `instructions` is a read-only property whose value is read at `run()` time, so a late write propagates to the MCP initialize handshake; the `_mcp_server` reach-in is guarded like `_slim_schema`. **A new tool *domain* needs a `_DOMAIN_BLURBS` entry** or the router silently omits it (`tests/test_server.py` checks the router tracks the registered domain set).

### Multi-daemon host registry (`docker_mcp/_hosts.py`)

`DOCKER_MCP_SERVER_HOSTS` lets one server manage several daemons in a session (e.g. local dev + remote prod). It's the single source of truth for which daemon(s) to talk to: **when it's set, `DOCKER_HOST` is ignored** (a one-time stderr notice fires when both are set); when unset, the server falls back to today's single-daemon behavior (`DOCKER_HOST`, else auto-discovery). The mcpb bundle exposes only this field.

`_hosts.py` lives at the package root (like `_env.py`, so `server.py` can import it without pulling in `docker_mcp.tools`). It parses the var into a pinned `{label: Host}` registry and owns all host resolution — no docker-py/CLI calls, just env + Docker config-file reads:

- **Grammar.** No `=` in the value → bare single-host shorthand (`ssh://ops@prod(ro)`, `auto`, `local`, or empty → `auto`). With `=` → comma-separated `label=endpoint` list. `endpoint` is the keyword `auto`/`local` or a `unix://`/`tcp://`/`ssh://`/`npipe://` URL, with combinable trailing markers `(ro)` (read-only) and `(tls=<dir>)` (a tcp+TLS cert dir; **`ca.pem` is required** — the daemon is always verified against it — and `cert.pem`+`key.pem` are optional, present together for mutual TLS or absent for verify-the-daemon-only, e.g. a self-signed daemon pinned via `ca.pem`). **Fail-fast** (`HostConfigError` → stderr + exit non-zero) on duplicate/empty/invalid labels, a missing `=`, an unknown marker, `(tls=)` on a non-tcp endpoint, a missing `ca.pem` or a lone `cert.pem`/`key.pem`, or an unrecognized scheme.
- **`auto`/`local`/`default` are OUR concepts, resolved to concrete URLs by us and pinned at `load()` (startup)** — so the docker-py SDK and the docker-CLI shell-out provably target the *same* daemon for a given label (auditable), and a mid-session `docker context use` can't silently move a label (restart to re-resolve). `auto` = the active CLI context's endpoint (`DOCKER_CONTEXT` / config.json `currentContext` → its `meta.json` Host) else the `local` socket probe; `local` = the platform-local socket (the `_probe_default_socket` candidate list); `default` = the omitted-`host` fallback = the first registry entry, and is **not** a selectable label. These resolution helpers were relocated *from* `system.py` (then `client.py`) into `_hosts.py`.
- **`load()` runs in `docker_mcp/__init__.py` before the tools import** (the `@tool()` decorator and resources read `is_multi()`/`labels()` at registration time) and scrubs whole-value `${...}` placeholders first (`_env.scrub_unresolved_env`, so an mcpb blank field resolves to the default host instead of fail-fasting).

**Per-call host selection (no modal active-host state).** Every daemon-targeting tool declares `host: str | None = None` and threads it to `_get_client(host)` / `run_docker(..., host=host)`; the `@tool()` decorator does the rest (the schema surgery is gated on `_hosts.is_multi()`; the call-time guard on the broader `_host_guard_needed()`):
- **Schema surgery** (`_apply_host_schema`, display-only like `_slim_schema` — call-time validation runs off `fn_metadata`, gated on `_hosts.is_multi()`): single-host → strip the `host` property entirely (footprint-neutral, schema byte-identical to today); multi-host → constrain `host` to an `enum` of the labels and mark it required for writes.
- **Call-time guard** (`_enforce_host_guard`, wrapped onto the tool via `_wrap_with_host_guard`, which preserves the signature and sync/async-ness): in multi-host mode writes require an explicit `host`, unknown labels are rejected, and writes to an `(ro)` host are refused. Read-only tools and the `_CONNECTION_CONTROL` set (`system_close`/`system_reconnect`/`system_login`/`system_logout`) may omit `host`. The guard is wrapped on whenever there is something to enforce — `_host_guard_needed()` = multi-host **or a single host flagged `(ro)`**: a lone `(ro)` host carries no `host` param (schema is still stripped, footprint-neutral) but its writes are still refused, so the per-host `(ro)` marker is honored even in single-host mode (distinct from the `DOCKER_MCP_SERVER_READONLY` switch, which drops write tools from the surface entirely). A single *writable* host wires no guard (today's path). **Excluded** (no `host` param at all): `registry`/`hub_*` (HTTPS, no daemon) and `context` (manages the host's CLI contexts).

**Client side** (`system.py`): a lazy pool `_clients` keyed by label; `_get_client(host)` builds per host with tiered TLS (`(tls=)` cert dir → global `DOCKER_CERT_PATH`/`DOCKER_TLS_VERIFY` → plaintext); the legacy single host (unset var) still goes through `_build_default_client`/`from_env` unchanged, and an explicit host that resolves to the platform default (url `None`) is built without a `base_url` so it never re-reads the ignored ambient `DOCKER_HOST`. `system_close(host=None)` closes all/one; **`system_reconnect(host=None)` is rebuild-only** — it cannot retarget to an arbitrary URL (to change a daemon, edit the registry and restart), which closes a trust-expansion hole. `_cli.py:_apply_host_env` injects the resolved `DOCKER_HOST` + per-host TLS into the child env for an explicit host (the ssh:// proxy keys off it). `startup_preflight` pings the *default* host but detects self-id against the *self host* (first local-transport entry, which may differ from a remote default), and `guard_not_self(container, host=)` only fires on the self host.

**Surfaces.** `host_list` (READ_ONLY) tool + the `docker-mcp://hosts` resource expose the resolved registry (the default is observable but not selectable). The router (`build_instructions`) adds a multi-host caveat, the container observability resources switch to empty-authority / host-qualified URIs (see MCP resources below), and a `prompt(multi_host=True)` gate plus the `survey_hosts` prompt register only when 2+ hosts are configured. **When changing the host grammar, the env-var precedence, the per-tool/resource/prompt host surface, or the resolution semantics, update this section.**

### Tools package (`docker_mcp/tools/`)
Each file maps to one Docker SDK domain (or, for CLI-only and registry-only features, one Docker feature area) and contains `@tool()` decorated functions. `docker_mcp/tools/__init__.py` imports all public modules with `*` so `docker_mcp/__init__.py` only needs `from docker_mcp import tools`. Underscore-prefixed modules (`_cli.py`, `_utils.py`) are private helpers and stay out of the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `docker_mcp/tools/_cli.py` | Cross-platform subprocess helper (private) | — |
| `docker_mcp/tools/_ssh_proxy.py` | Per-call paramiko proxy that lets CLI-backed tools dial `ssh://` daemons without a system `ssh` binary (private) | — |
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

`tests/integration/` holds tests that hit a real Docker daemon. `tests/integration/conftest.py` auto-marks every test in the directory with `@pytest.mark.integration` (excluded by default via `addopts = "-m 'not integration'"` in `pyproject.toml`) and provides an autouse `skip_if_no_daemon` fixture so the suite skips cleanly when no daemon is reachable. Run with `uv run pytest -m integration`.

### Container image (`Dockerfile`)

An additional distribution channel alongside uvx-from-git (which is unchanged). One multi-stage `Dockerfile` builds variants via build args (`INSTALL_CLI`, `INSTALL_SCOUT`, `DISABLE_DOMAINS`): `full` (docker CLI + compose + buildx + scout) and `no-scout` (sets `DOCKER_MCP_SERVER_DISABLE=scout` so the absent-plugin scout tools don't register) are **published to GHCR (and mirrored to Docker Hub when the Hub secrets are set) on each GitHub Release** — the same tags on each registry (`full` → `:latest`/`:<version>`, `no-scout` → `:no-scout`/`:<version>-no-scout`); `lite` (`INSTALL_CLI=0`, docker-py SDK only — CLI domains degrade via `has_plugin()`) is buildable but not published. `.github/workflows/images.yaml` builds+measures on PRs/pushes to main; the `images` job in `.github/workflows/publish.yaml` (see "Release pipeline" below) pushes multi-arch images on a GitHub Release — always to GHCR, and mirrored to Docker Hub (`gavinlucas/docker-mcp-server`, plus a `DOCKERHUB.md`→Hub-description sync — a slim container-focused readme, since the full `README.md` exceeds Hub's 25 KB cap) when the opt-in `DOCKERHUB_USER`/`DOCKERHUB_TOKEN` secrets are set (the Hub token needs `read/write/delete` scope or the description PATCH 403s); with them unset, only GHCR is pushed so a release never fails for lack of Hub credentials. Two container-aware guards live behind `_utils.in_container()` (true when `/.dockerenv` exists or `DOCKER_MCP_SERVER_IN_CONTAINER=1`, set in the image) and are **inert on the host install**:

- **Filesystem guard** (`_utils.py`): `assert_host_writable` (hooked into `stream_to_file`) refuses a host-file write (`dest_path` / `container_archive_get_to_file`) to a path that isn't a host bind mount (silent loss on `--rm`); `host_read_path` enriches the "missing file" case on reads. `_host_backed` parses `/proc/self/mountinfo`.
- **Self-termination guard** (`system.py`): `startup_preflight()` (called from `main()`) pings the default daemon, prints OS-aware socket hints to **stderr** (never stdout — that's the stdio channel) on failure, and pins the server's own container id (detected against the *self host* — the first local-transport entry, which can differ from a remote default); `guard_not_self(container, host=)` then makes the destructive container-lifecycle tools refuse to act on self, **only when the call targets the self host** (override: `DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1`).

### Desktop Extension (MCPB bundle)

A third distribution channel (alongside uvx/PyPI and the container images) for one-click install in Claude Desktop. The repo root carries the bundle sources: `manifest.json` (the MCPB manifest, `manifest_version` 0.4, `server.type: "uv"`), `mcpb_run.py` (the bundle entry point — `from docker_mcp import main; main()`, kept at the root so `import docker_mcp` resolves however the host's managed `uv` lays out `sys.path`), `.mcpbignore` (trims the packed bundle to source + `pyproject.toml` + `uv.lock` + `README.md`/`LICENSE` + `manifest.json`/`mcpb_run.py` + `assets/`), and `assets/icon.png` (512×512). Because it's a `uv`-type bundle, the host resolves dependencies from `pyproject.toml` at install time — there's no vendored venv, so the bundle stays ~250 KB and cross-platform. `manifest.json`'s `user_config` block renders the install-dialog fields and maps them to env: `DOCKER_MCP_SERVER_HOSTS` (the single host field — `DOCKER_HOST` is deliberately **not** exposed; the bare-value shorthand keeps the simple one-daemon case a one-liner) plus the `DOCKER_MCP_SERVER_READONLY` / `_NO_DESTRUCTIVE` / `_DISABLE` switches (the container-only `_ALLOW_SELF_TERMINATE` is deliberately **not** exposed — the bundle never runs containerized). The `manifest.json` `version` is kept in step with `pyproject.toml` (`tests/test_pyproject_pins.py` asserts it), but the `mcpb` job in `.github/workflows/publish.yaml` rewrites it from the release tag at pack time so it can't drift; that job packs the `.mcpb` with `npx @anthropic-ai/mcpb` and attaches it (plus a `.sha256` recording the bare filename, so `sha256sum -c` works on downloads) to each GitHub Release. `scripts/build-mcpb.sh` is the **developer-only** local equivalent of that pack step (reads the version from `pyproject.toml`, packs to `dist/` with an auto-incrementing name, never mutates `manifest.json`) — for smoke-testing a bundle in Claude Desktop; it is deliberately **not** wired into CI, which uses the workflow above. `PRIVACY.md` (a no-telemetry/no-backend statement) is referenced by the manifest's `privacy_policies` and summarized in the README. **When the manifest's tool/env surface or the bundled file set changes, update this section.**

### Homebrew tap (`L337-org/homebrew-tap`) — PAUSED

The infrastructure exists (`scripts/docker-mcp-server.rb.tpl`, `.github/workflows/publish-homebrew.yaml`, `L337-org/homebrew-tap`) but the **release trigger is disabled** pending resolution of a Homebrew dylib linkage issue: pre-built PyPI wheels for `pydantic_core` lack `-headerpad_max_install_names`, so Homebrew's post-install relocation step fails to rewrite the `@rpath` ID (the binary's Mach-O header has no room for the longer absolute path). The workflow is `workflow_dispatch`-only until a fix is found. The channel is not advertised in the README. To re-enable: add `release: types: [published]` back to `publish-homebrew.yaml`'s `on:` block and re-add the Homebrew section to the README.

### MCP Registry (`server.json`)

A discovery listing in the official MCP Registry (`registry.modelcontextprotocol.io`), which stores only **metadata** pointing at the artifacts the other channels already publish — so it's not a fourth artifact, just an index entry covering all of them. `server.json` (repo root, server name `io.github.L337-org/docker-mcp-server`) declares three package types in one entry: `pypi` (`docker-mcp-server`), `oci` (the GHCR image), and `mcpb` (the release `.mcpb`). The `registry` job in `.github/workflows/publish.yaml` stamps the tag version and the published `.mcpb`'s `fileSha256` into `server.json`, authenticates via **GitHub OIDC** (`id-token: write`, no stored secret), and runs `mcp-publisher publish`. The registry verifies we own each listed package by matching a marker against `server.json`'s `name`, so three markers must stay equal to it: the `<!-- mcp-name: … -->` comment in `README.md` (PyPI long-description), the `io.modelcontextprotocol.server.name` image label in the `images` job (OCI), and the `.mcpb` URL (must contain "mcp") + its hash (MCPB). The job runs `needs: [pypi, images, mcpb]`, so all three markers are live before it starts — its short retries only absorb registry-side read lag. **If `server.json`'s name/markers or the package set change, update this section.**

### Release pipeline (`.github/workflows/publish.yaml`)

All publishing runs through one workflow on each **published GitHub Release** (nothing publishes without that human step): `preflight` → `pypi` ∥ `images`(→`dockerhub-description`) ∥ `mcpb` → `registry` → `verify`, with a `notify` job on failure. `preflight` resolves the tag once for every job (each checks out **the tag ref**, not the event SHA) and fails fast if the tag disagrees with the committed `pyproject.toml` / `manifest.json` / `uv.lock` self-entry versions — preventing a split-brain release (PyPI ships the pyproject version; every other channel ships the tag version). It also refuses to run when `github.repository_owner` isn't `L337-org`, so a release published from a fork (or mis-timed around a repo transfer) fails before anything ships rather than half-publishing. `verify` confirms all four channels actually serve the version (PyPI JSON API, both GHCR tags, the `.mcpb` asset + checksum, the registry listing) so a partial release is loud. `notify` files (or comments on) a deduplicated issue labeled `ci-failure` + `wf:release` via the composite action `.github/actions/file-failure-issue` — one open issue per failure stream; a scheduled Claude responder routine polls `ci-failure` issues. Re-run a failed release via `workflow_dispatch` with the tag: every job is idempotent (`skip-existing` on PyPI, re-pushed image tags, `--clobber` on the `.mcpb`, duplicate-tolerant registry publish). **PyPI Trusted Publishing pins the workflow *filename*** — the trusted publisher on PyPI must name `publish.yaml`; if the `pypi` job fails OIDC, that registration is missing. Version-bump checklist for a release: edit `pyproject.toml` + `manifest.json`, run `uv lock`, merge, then publish a GitHub Release whose tag matches (`.github/release.yml` — GitHub's release-*notes* config, a different file — groups the generated notes, Dependabot PRs under "Dependencies"). `publish-homebrew.yaml` (paused, dispatch-only) stays separate. **When the pipeline's job graph, markers, or re-run semantics change, update this section.**

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
- Line length limit: 120 characters (enforced by ruff and flake8).

## Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) via `docker_mcp/tools/_labels.py`, so the agent/operator can later enumerate that footprint — the `managed_only=True` arg on `container_list` / `network_list` / `volume_list` / `service_list`, or `--filter label=docker-mcp-server.managed=true`. The `prune_managed` prompt tears down only the managed footprint. Stamping is **on by default** and additive (a caller-supplied label always wins on a key collision); `DOCKER_MCP_SERVER_NO_LABELS=1` turns it off. The prefix is the bare project name (deliberately not reverse-DNS) and is a single constant in `_labels.py`.

When adding a new create tool that accepts a `labels` dict, route it through `_labels.py:with_provenance(labels, "<tool_name>")` (it accepts the dict/list/None shapes the SDK accepts and returns `None` — feed it through `drop_none` — when stamping is off and the caller passed nothing). The six stamped creators today are `container_run`, `container_create`, `network_create`, `volume_create`, `service_create` (service-level `labels` only, not `container_labels`), `config_create`, `secret_create`. **Image builds are intentionally NOT stamped** — a build label changes the resulting image digest. Compose/stack containers (created via CLI shell-out) are also unstamped. New `managed_only`-style label filters go through `_labels.py:managed_filter`.

## CLI shell-out policy

Any tool that wraps a `docker` CLI feature (Compose, Context, Buildx, Scout, etc.) MUST go through `docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper centralizes:

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
9. `.github/copilot-instructions.md` — **mirror the architecture/convention change here too** (see the MIRROR RULE at the top of this file); it drives Copilot's review of every PR.

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

### MCP prompts

`docker_mcp/tools/prompts.py` exposes `@prompt(description=..., domain=...)` templates (the `prompt` helper imported from `docker_mcp.server`, **not** `@mcp.prompt` directly) that return rendered prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Each prompt declares its primary `domain` so `DOCKER_MCP_SERVER_DISABLE` skips it when that domain is off; use `domain=None` for general / cross-domain prompts (doc lookup, prune, disk usage) that should always register. Prompts follow the same docstring format as tools and are star-imported via `docker_mcp/tools/__init__.py`.

## Docker SDK Policy

**Before writing or modifying any code that calls the Docker SDK (`docker` package), you MUST run `/docker-sdk` (or `/docker-sdk <topic>`) to:**
1. Verify exact method signatures from the live Docker SDK for Python documentation
2. Confirm parameter names and return types before writing code
3. Never use a `docker` module method that has not been confirmed in the docs

Do not assume any method exists because it sounds plausible. If you cannot confirm it from the documentation, say so and do not use it.

When the high-level SDK has no method for an operation (e.g. swarm node removal, service rollback), drop to the low-level **`APIClient` via `_get_client().api`** — its methods (`remove_node`, `update_service`, `inspect_service`, …) are documented at https://docker-py.readthedocs.io/en/stable/api.html and must be verified the same way. Prefer the high-level object API when it exists; reach for `client.api` only for the gaps.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html  
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html  
Docker SDK GitHub: https://github.com/docker/docker-py
