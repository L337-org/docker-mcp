# GitHub Copilot Instructions

This file provides guidance to GitHub Copilot when working with code in this repository.

## Project

`docker-mcp` is a Python MCP (Model Context Protocol) server that exposes the Docker SDK for Python — plus selected docker CLI features (Compose, Context, Buildx, Scout) and direct OCI-registry HTTPS access — as MCP tools. It requires Python >=3.14 and is managed with `uv`. It is published to PyPI as **`docker-mcp-server`** and as a container image to GHCR (`ghcr.io/gavinlucas/docker-mcp-server`), mirrored to Docker Hub (`gavinlucas/docker-mcp-server`) when the opt-in `DOCKERHUB_*` release secrets are configured (the import package stays `docker_mcp`, the repo stays `…/docker-mcp`); two console scripts — `docker-mcp` and `docker-mcp-server` — both target `docker_mcp:main`. A third distribution channel packages the server as a **Claude Desktop Extension (`.mcpb`)** attached to each GitHub Release (see "Desktop Extension (MCPB)" below).

The `docker` dep uses the `[ssh]` extra (paramiko), so `DOCKER_HOST=ssh://…` works via a pure-Python transport (no system `ssh` binary; works in the container images). docker-py auto-selects paramiko for `ssh://`, so there's no transport code — only the `ssh://` branch in `client._connection_help`. CLI-backed tools (Compose, Buildx, Context, Scout) shell out to `docker`, which would otherwise need the system `ssh` client — instead, `_cli.py:run_docker` detects `DOCKER_HOST=ssh://…` and routes the subprocess through a per-call local TCP proxy (`docker_mcp/tools/_ssh_proxy.py`) that opens its own paramiko connection and runs `docker system dial-stdio` over it, so the CLI authenticates the same way the docker-py-backed tools do, with no system `ssh` binary involved (except a `ProxyCommand` in `~/.ssh/config` for bastion/jump-host setups, which paramiko runs as an external command — commonly `ssh -W %h:%p ...` — for both tool families alike).

## Architecture

### Entry point
The `docker_mcp` package is the entry point. `docker_mcp/__init__.py` defines `main()` and side-effect-imports the `server` and `tools` submodules (which registers all `@tool()` decorators); `docker_mcp/__main__.py` calls `main()` so `python -m docker_mcp` works.

### Server singleton (`docker_mcp/server.py`)
`docker_mcp/server.py` instantiates `FastMCP` and exports three things:

- **`tool`** — the registration decorator every tool module uses. **Always import `tool` from `docker_mcp.server`** and decorate with `@tool()`; never import from the `mcp` package directly in tool files (circular import) and never use `@mcp.tool()` in tool modules.
- **`prompt`** — the prompt registration decorator `prompts.py` uses (`@prompt(description=..., domain=...)`), analogous to `tool` and gating on `DOCKER_MCP_SERVER_DISABLE`; never use `@mcp.prompt()` directly in `prompts.py`.
- **`mcp`** — the FastMCP singleton, imported by `resources.py` for `@mcp.resource()`.

```python
from docker_mcp.server import tool     # tool modules
from docker_mcp.server import prompt    # prompt modules (with domain=...)
from docker_mcp.server import mcp       # resource modules only
```

`server.py` also owns **`TOOL_CATEGORIES`**, the central map classifying every tool as `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`. The `@tool()` decorator uses it to attach MCP `ToolAnnotations` and to skip registration under the env switches `DOCKER_MCP_SERVER_READONLY` (register only read-only tools) and `DOCKER_MCP_SERVER_NO_DESTRUCTIVE` (register everything except destructive). It also records each tool's **domain** (its defining module's leaf, e.g. `containers`) so the orthogonal `DOCKER_MCP_SERVER_DISABLE=<domains>` switch can drop whole feature areas — including that domain's **prompts** (via the `prompt(domain=...)` helper) and its **doc-resource sections** (via `_SECTION_DOMAINS` in `resources.py`), not just its tools; the live snapshot is the `docker-mcp://tool-catalog` resource (`server.tool_catalog()`). **Every new tool needs a `TOOL_CATEGORIES` entry** — `tests/test_server.py` fails the build if the map drifts from the registered set. The decorator also runs `_slim_schema` on the advertised `inputSchema` to drop three information-free patterns (~18% of schema tokens): pydantic `title` annotations, the `{"type":"null"}` branch of a nullable `anyOf` (gated on a sibling `default`), and redundant `additionalProperties: true`; it's display-only (validation runs off `fn_metadata`), and `tests/test_server.py` asserts none survive. All server tunables are namespaced `DOCKER_MCP_SERVER_*`; read them through `docker_mcp/_env.py` (`read_env` / `env_flag`), which still honors the pre-rename `DOCKER_MCP_*` spellings as deprecated aliases and warns once to stderr.

`server.py` also builds the FastMCP **`instructions`** string — pre-loaded into a client's context with the server name and tool names, *before* any per-tool schema, so for a lazy-loading client (e.g. Claude Code, which fetches tool schemas on demand) it's the main always-in-context surface. It's written as a **router** (per-domain keyword one-liners + a few tool-selection caveats), not docs, and does not enumerate tools (that's `docker-mcp://tool-catalog`). `build_instructions()` renders it from `_DOMAIN_BLURBS`, emitting a domain's line **only when that domain has a registered tool**, so `DOCKER_MCP_SERVER_DISABLE` / `_READONLY` / `_NO_DESTRUCTIVE` are honored via the one registration flag. `finalize_instructions()` (called from `docker_mcp/__init__.py` after all tools import) writes it through to `mcp._mcp_server.instructions` (FastMCP's `instructions` is a read-only property read at `run()` time, so a late write propagates; the reach-in is guarded). **A new tool domain needs a `_DOMAIN_BLURBS` entry** or the router silently omits it.

### Multi-daemon host registry (`docker_mcp/_hosts.py`)

`DOCKER_MCP_SERVER_HOSTS` lets one server manage several daemons in a session (e.g. local dev + remote prod). **When set, `DOCKER_HOST` is ignored** (a one-time stderr notice fires when both are set); unset = today's single-daemon behavior (`DOCKER_HOST`, else auto-discovery). The mcpb bundle exposes only this field. `_hosts.py` lives at the package root (like `_env.py`, so `server.py` can import it without pulling in `docker_mcp.tools`) and parses the var into a pinned `{label: Host}` registry — pure env + Docker-config-file reads, no docker-py/CLI calls:

- **Grammar.** No `=` in the value → bare single-host shorthand (`ssh://ops@prod(ro)`, `auto`, `local`, empty → `auto`); with `=` → comma-separated `label=endpoint` list. `endpoint` is the keyword `auto`/`local` or a `unix://`/`tcp://`/`ssh://`/`npipe://` URL, with combinable trailing markers `(ro)` (read-only) and `(tls=<dir>)` (a tcp+TLS cert dir; **`ca.pem` is required** — the daemon is always verified against it — and `cert.pem`+`key.pem` are optional, present together for mutual TLS or absent for verify-the-daemon-only, e.g. a self-signed daemon pinned via `ca.pem`). **Fail-fast** (`HostConfigError` → stderr + non-zero exit) on duplicate/empty/invalid labels, a missing `=`, an unknown marker, `(tls=)` on a non-tcp endpoint, a missing `ca.pem` or a lone `cert.pem`/`key.pem`, or an unrecognized scheme.
- **`auto`/`local`/`default` are resolved to concrete URLs by us and pinned at `load()` (startup)** so the docker-py SDK and the CLI shell-out target the *same* daemon for a label (auditable) and a mid-session `docker context use` can't silently move a label. `default` = the first registry entry = the omitted-`host` fallback, and is **not** a selectable label. `load()` runs in `docker_mcp/__init__.py` before the tools import (the `@tool()` decorator and resources read `is_multi()`/`labels()` at registration) and scrubs whole-value `${...}` placeholders first.
- **Per-call host selection (no modal active-host state).** Every daemon-targeting tool declares `host: str | None = None` and threads it to `_get_client(host)` / `run_docker(..., host=host)`. The `@tool()` decorator does **display-only schema surgery** (`_apply_host_schema`, like `_slim_schema`, gated on `_hosts.is_multi()`) — strip `host` in single-host mode (footprint-neutral), or constrain it to an `enum` of the labels and mark it required for writes in multi-host mode — and wraps the tool with `_enforce_host_guard` (multi-host: writes require an explicit `host`, unknown labels rejected, writes to an `(ro)` host refused; read-only tools and the `_CONNECTION_CONTROL` set `close`/`reconnect`/`login`/`logout` may omit `host`). The guard is wrapped on when `_host_guard_needed()` — multi-host **or a single host flagged `(ro)`**: a lone `(ro)` host has its `host` param stripped (footprint-neutral) but its writes are still refused, so the per-host `(ro)` marker is honored even in single-host mode (distinct from `DOCKER_MCP_SERVER_READONLY`, which drops write tools entirely); a single *writable* host wires no guard. **Excluded** (no `host` param): `registry`/`hub_*` (HTTPS, no daemon) and `context` (manages the host's CLI contexts).
- **Client / CLI.** `client.py` keeps a lazy pool `_clients` keyed by label with tiered per-host TLS (`(tls=)` dir → global `DOCKER_CERT_PATH`/`DOCKER_TLS_VERIFY` → plaintext); the legacy single host still uses `_build_default_client`/`from_env` unchanged. **`reconnect(host=None)` is rebuild-only** — it can't retarget to an arbitrary URL (edit the registry + restart), closing a trust-expansion hole. `close(host=None)` closes all/one. `startup_preflight` pings the default host but detects self-id against the *self host* (first local-transport entry, which can differ from a remote default); `guard_not_self(container, host=)` only fires on the self host. `_cli.py:_apply_host_env` injects the resolved `DOCKER_HOST` + per-host TLS into the child env for an explicit host.
- **Surfaces.** `list_hosts` tool + `docker-mcp://hosts` resource expose the resolved registry; the router gains a multi-host caveat; the container observability resources go host-aware (empty-authority `docker:///…` for the default + `docker://{host}/…` variants); a `prompt(multi_host=True)` gate plus the `survey_hosts` prompt register only with 2+ hosts.

**When reviewing a PR that changes the host grammar, env precedence, or the per-tool/resource/prompt host surface, this section is the spec.**

### Tools package (`docker_mcp/tools/`)
Each file maps to one Docker SDK domain or one CLI/registry feature area. Underscore-prefixed modules are private helpers excluded from the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `_cli.py` | Cross-platform subprocess helper (private) | — |
| `_ssh_proxy.py` | Per-call paramiko proxy letting CLI-backed tools dial `ssh://` daemons without a system `ssh` binary (private) | — |
| `_utils.py` | Shared helpers: `drop_none`, `join_bounded`, `stream_to_file`, `close_stream_quietly`, `MAX_PAYLOAD_BYTES`, plus the container guards `in_container` / `assert_host_writable` / `host_read_path` / `classify_host_kernel` (private) | — |
| `_labels.py` | Provenance labels stamped on created resources: `with_provenance` / `managed_filter` / `provenance_labels` (private) | — |
| `client.py` | `DockerClient` — connection, lifecycle, `login`/`logout`, `reconnect` | docker-py |
| `containers.py` | Container lifecycle and management | docker-py |
| `images.py` | Image pull, build, push, inspect, save/load | docker-py |
| `networks.py` | Network create, connect, inspect | docker-py |
| `volumes.py` | Volume create, list, remove | docker-py |
| `configs.py` | Swarm configs | docker-py |
| `nodes.py` | Swarm nodes | docker-py |
| `plugins.py` | Plugin install and management | docker-py |
| `secrets.py` | Swarm secrets | docker-py |
| `services.py` | Swarm services | docker-py |
| `swarm.py` | Swarm init, join, leave, join tokens | docker-py |
| `compose.py` | Docker Compose v2 | `docker compose` CLI via `_cli.py` |
| `stack.py` | Docker stacks (Compose-on-Swarm) | `docker stack` CLI via `_cli.py` |
| `context.py` | Docker CLI contexts | `docker context` CLI via `_cli.py` |
| `buildx.py` | Buildx / BuildKit (incl. build history) | `docker buildx` CLI via `_cli.py` |
| `scout.py` | Vulnerability scanning, SBOMs | `docker scout` CLI via `_cli.py` |
| `registry.py` | OCI v2 registries + Docker Hub | HTTPS via `httpx` (no daemon) |
| `prompts.py` | `@mcp.prompt()` workflow templates | — |
| `resources.py` | `@mcp.resource()` doc endpoints | — |

`docker_mcp/tools/__init__.py` star-imports all public modules so `docker_mcp/__init__.py` only needs `from docker_mcp import tools`.

### Tests (`tests/`)
Each `docker_mcp/tools/<module>.py` has a corresponding `tests/test_<module>.py`; `tests/test_server.py` covers the classification/registration machinery. Tests use pytest with mocks. `tests/integration/` holds tests that need a real Docker daemon — excluded by default, run with `uv run pytest -m integration`. `tests/conftest.py` clears the `DOCKER_MCP_*` env switches so the suite is hermetic.

### Container image (`Dockerfile`)

An additional distribution channel alongside the uvx-from-git install (unchanged). One ARG-gated multi-stage `Dockerfile` builds `full` (docker CLI + compose + buildx + scout) and `no-scout` (sets `DOCKER_MCP_SERVER_DISABLE=scout` so absent-plugin scout tools don't register), published on each GitHub Release via `.github/workflows/publish-images.yaml` — always to GHCR, and mirrored to Docker Hub (plus a `DOCKERHUB.md`→Hub-description sync — a slim container-focused readme, as the full `README.md` exceeds Hub's 25 KB cap) when the opt-in `DOCKERHUB_USER`/`DOCKERHUB_TOKEN` secrets are set — the Hub token must have `read/write/delete` scope (build/measure on PRs/pushes is the separate `images.yaml`); `lite` (`INSTALL_CLI=0`) is buildable but not published. Two container-aware guards live behind `_utils.in_container()` (true when `/.dockerenv` exists or `DOCKER_MCP_SERVER_IN_CONTAINER=1`) and are **inert on the host install** — keep them in mind when editing `_utils.py` or the file-path tools:

- **Filesystem guard** — `assert_host_writable` (hooked into `stream_to_file`) refuses a `*_to_file` write to a path that isn't a host bind mount (it would be lost on `--rm`); `host_read_path` enriches the read-side "missing file" case.
- **Self-termination guard** — `client.startup_preflight()` (called from `main()`) pins the server's own container id (detected against the *self host* — the first local-transport entry, which can differ from a remote default) and prints OS-aware socket hints to stderr; `client.guard_not_self(container, host=)` stops the destructive container-lifecycle tools (`remove`/`kill`/`stop`/`restart`/`pause_container`) from acting on the server's own container, **only when the call targets the self host** (override `DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1`).

### Desktop Extension (MCPB)

A third distribution channel for one-click install in Claude Desktop. Repo-root sources: `manifest.json` (MCPB manifest, `manifest_version` 0.4, `server.type: "uv"`), `mcpb_run.py` (bundle entry point — `from docker_mcp import main; main()`, at the root so `import docker_mcp` resolves under the host's managed `uv`), `.mcpbignore` (trims the packed bundle to source + `pyproject.toml` + `uv.lock` + `README.md`/`LICENSE` + manifest/entry-point + `assets/`), and `assets/icon.png` (512×512). It's a `uv`-type bundle: the host resolves deps from `pyproject.toml` at install time (no vendored venv). `manifest.json`'s `user_config` maps install-dialog fields to env — `DOCKER_MCP_SERVER_HOSTS` (the single host field; `DOCKER_HOST` is **not** exposed — the bare-value shorthand keeps the one-daemon case a one-liner) and the `DOCKER_MCP_SERVER_READONLY` / `_NO_DESTRUCTIVE` / `_DISABLE` switches (the container-only `_ALLOW_SELF_TERMINATE` is intentionally omitted — the bundle never runs containerized). `.github/workflows/publish-mcpb.yaml` rewrites the manifest `version` from the release tag, packs the `.mcpb` via `npx @anthropic-ai/mcpb`, and attaches it (plus a `.sha256`) to each GitHub Release. `PRIVACY.md` (no-telemetry statement) is referenced by the manifest's `privacy_policies`. Keep the manifest `version` aligned with `pyproject.toml`; update this section when the manifest's tool/env surface or the bundled file set changes.

### MCP Registry (`server.json`)

A discovery listing in the official MCP Registry (`registry.modelcontextprotocol.io`), which stores only metadata pointing at the existing artifacts. `server.json` (repo root, name `io.github.GavinLucas/docker-mcp-server`) declares three package types — `pypi`, `oci` (GHCR image), `mcpb` (release `.mcpb`). `.github/workflows/publish-registry.yaml` runs on each Release: stamps the tag version + the published `.mcpb`'s `fileSha256` into `server.json`, authenticates via GitHub OIDC (`id-token: write`, no secret), and runs `mcp-publisher publish`. Ownership is verified per package against `server.json`'s `name`, so three markers must equal it: the `<!-- mcp-name: … -->` comment in `README.md` (PyPI), the `io.modelcontextprotocol.server.name` image label in `publish-images.yaml` (OCI), and the `.mcpb` URL + hash (MCPB). Keep them in sync with `server.json`'s `name`.

## Conventions

- **MIRROR RULE:** this file mirrors `CLAUDE.md` and drives Copilot review of every PR. Any change to project structure, conventions, env vars, or the tool/prompt/resource surface must update **both** files in the same PR — flag a PR that updates one but not the other.
- New Docker functionality goes in the matching `docker_mcp/tools/<domain>.py` — do not create new tool files without a corresponding entry in `docker_mcp/tools/__init__.py` and a matching test file.
- Tool functions are decorated with `@tool()` (imported from `docker_mcp.server`) and **must have a `TOOL_CATEGORIES` entry** in `docker_mcp/server.py`. A new tool module is a new domain — also add a `_DOMAIN_BLURBS` entry so the `instructions` router advertises it.
- A daemon-targeting tool declares `host: str | None = None` (last parameter) and threads it to `_get_client(host)` / `run_docker(..., host=host)`; it is intentionally **not** documented in the docstring `args:` (the `@tool()` decorator generates its description and strips/enum-injects it per mode — see the host-registry section). Registry/hub and context tools omit `host`.
- Line length limit: 120 characters.
- **Bound any externally-sourced bytes before buffering/parsing them, and parse safely.** CLI output is capped in `run_docker` (`MAX_CLI_OUTPUT_BYTES`); registry HTTP bodies are streamed and capped at `_MAX_RESPONSE_BYTES` in `registry.py` (registries are agent-pointed/untrusted; the cap is on the *decoded* stream, so it also stops a decompression bomb). New code reading an untrusted file or network body must apply a similar bound. Always `json.loads` (never `eval`); if YAML is ever parsed in Python, `yaml.safe_load` only — today nothing parses YAML in Python (Compose YAML is read by the `docker` CLI). Flag a PR that buffers an untrusted body unbounded.
- Do not add comments that describe what the code does — only add comments for non-obvious constraints or workarounds.
- **Target runtime is Python >=3.14** — when reviewing, assume current stable CPython grammar and stdlib are available and valid; do not flag 3.14-valid syntax as a bug. A concrete example reviewers (and older models) get wrong: [PEP 758](https://peps.python.org/pep-0758/) — **Status: Final, Python-Version: 3.14** — makes parentheses optional in `except` / `except*` clauses, so `except OSError, ValueError:` is a valid two-exception handler, **not** a `SyntaxError` (verifiable: `python3.14 -c "import ast; ast.parse('try:\n pass\nexcept OSError, ValueError:\n pass')"` parses it as a tuple handler). The `as`-binding form still requires parens (`except (OSError, ValueError) as e:`), and `ruff` (pyupgrade, 3.14 target) may rewrite to the unparenthesized form.

### Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) so the agent/operator can later enumerate that footprint (the `managed_only=True` arg on `list_containers` / `list_networks` / `list_volumes` / `list_services`, or `--filter label=docker-mcp-server.managed=true`; the `prune_managed` prompt removes only the managed footprint). On by default; opt out with `DOCKER_MCP_SERVER_NO_LABELS=1`. When adding a new create tool that accepts a `labels` dict, route it through `docker_mcp/tools/_labels.py:with_provenance(labels, "<tool_name>")` — it merges provenance without overwriting caller keys and returns `None` (drop it via `drop_none`) when stamping is disabled and the caller passed nothing. **Image builds are intentionally not stamped** (a build label changes the image digest).

### CLI shell-out policy

Any tool wrapping a `docker` CLI feature MUST go through `docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper enforces `shell=False`, argv lists, binary resolution via `shutil.which`, UTF-8 decoding with replace, an output byte cap, an environment allow-list, and Windows console suppression. Additional rules:

- Wrap every user-supplied **positional** value in `safe_positional(value, "what")` — a value starting with `-` would otherwise be parsed by the docker CLI as a flag (argument injection). The only exception is an argv that is *meant* to be arbitrary (the trailing command of `compose_exec` / `compose_run`).
- Always pass an explicit `timeout=` to `run_docker` (generous for build/pull, short for queries).
- Error convention (intentional — do not "unify"): **action tools** return the raw `{"returncode", "stdout", "stderr", "truncated"}` dict and never raise on a non-zero exit; **parsed-query tools** (`context_ls`, `buildx_ls`/`du`, `compose_ls`) raise `RuntimeError` via `raise_on_cli_failure` because they cannot return a useful partial parse.

### Tool function format

All `@tool()` functions must follow this exact docstring format:

```python
from docker_mcp.server import tool


@tool()
def mcp_example(name: str):
    """
    Say hello to someone by name.

    args: name: str - The name to say hello to
    returns: str - The greeting
    """
    return f"Hello, {name}!"
```

- One-line summary sentence, then a blank line
- `args:` section lists each parameter as `name: type - description`
- `returns:` line documents the return type and what it contains

### Bounding rules

- Tools that buffer a daemon-side byte stream in memory must cap it with `join_bounded(stream, max_bytes, what)`; the in-band default is `MAX_PAYLOAD_BYTES` (32 MiB). Large payloads belong in the `*_to_file` / `*_from_file` variants, which stream via `stream_to_file` / an open file handle.
- Tools that iterate a potentially endless stream (events, followed logs) must have a wall-clock bound — see the `threading.Timer` + `CancellableStream.close()` watchdog pattern in `client.py:events` and `containers.py:follow_container_logs`.

### MCP resources

`docker_mcp/tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data: the Docker SDK for Python documentation under the `docker-docs://` URI scheme, `docker-mcp://tool-catalog` (the live tool/domain/category snapshot), `docker-mcp://hosts` (the resolved host registry), and the container-observability resources `docker://containers` / `docker-logs://{id_or_name}` / `docker-stats://{id_or_name}` (the last reuse `containers.py`'s private `_read_log_tail` / `_read_stats_summary`, now `host=`-aware, and refuse when the `containers` domain is disabled). **In multi-host mode these container resources are host-aware** — the default forms become empty-authority (`docker:///containers`, …) and host-qualified variants (`docker://{host}/containers`, …) register alongside, disambiguated by path-segment count; single-host keeps the bare forms. Registration is gated on `_hosts.is_multi()`. `_SECTION_DOMAINS` maps each doc section to a domain so `DOCKER_MCP_SERVER_DISABLE` hides a disabled domain's sections (registered with the server via `register_resource_domains`). Use the same docstring format as tools.

### MCP prompts

`docker_mcp/tools/prompts.py` exposes `@prompt(description=..., domain=...)` templates (the `prompt` helper from `docker_mcp.server`, not `@mcp.prompt` directly) that return prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Each declares its primary `domain` so `DOCKER_MCP_SERVER_DISABLE` drops it with that domain; `domain=None` for general/cross-domain prompts. Use the same docstring format as tools.

## Docker SDK Policy

**Only use `docker` module methods that are documented in the official reference.**
Always verify the exact method name, parameter names, and return type at https://docker-py.readthedocs.io/en/stable/ before writing or suggesting code. Do not suggest methods that sound plausible but are not in the docs.

When the high-level SDK lacks a method (e.g. swarm node removal, service rollback), use the low-level `APIClient` via `_get_client().api` (`remove_node`, `update_service`, `inspect_service`, …), documented at https://docker-py.readthedocs.io/en/stable/api.html — verified the same way. Prefer the high-level object API where it exists.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html
Docker SDK GitHub: https://github.com/docker/docker-py

### Review Notes

There is a dependencies line in pyproject.toml: `"cryptography<49; platform_system == 'Darwin' and platform_machine == 'x86_64'"`
Changing this line to a later version of cryptography can break installs on Intel (x86_64) macOS, which CI does not currently cover.
From version 49, cryptography no longer ships a universal2 macOS wheel; on Intel macOS the resolver typically falls back to a source build that requires a newer Rust toolchain and may fail.
Changing to a later version of cryptography for other platforms is fine, but this pin should remain for macOS x86_64.
Do not change this line. If a PR touches this line, please add a strong comment and do not allow the change to be merged without a strong justification and approval from the repo owner.
