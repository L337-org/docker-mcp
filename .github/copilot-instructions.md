# GitHub Copilot Instructions

This file provides guidance to GitHub Copilot when working with code in this repository.

## Project

`docker-mcp` is a Python MCP (Model Context Protocol) server that exposes the Docker SDK for Python — plus selected docker CLI features (Compose, Context, Buildx, Scout) and direct OCI-registry HTTPS access — as MCP tools. It requires Python >=3.14 and is managed with `uv`. It is published to PyPI as **`docker-mcp-server`** and as a container image to GHCR (`ghcr.io/gavinlucas/docker-mcp-server`), mirrored to Docker Hub (`gavinlucas/docker-mcp-server`) when the opt-in `DOCKERHUB_*` release secrets are configured (the import package stays `docker_mcp`, the repo stays `…/docker-mcp`); two console scripts — `docker-mcp` and `docker-mcp-server` — both target `docker_mcp:main`.

## Architecture

### Entry point
The `docker_mcp` package is the entry point. `docker_mcp/__init__.py` defines `main()` and side-effect-imports the `server` and `tools` submodules (which registers all `@tool()` decorators); `docker_mcp/__main__.py` calls `main()` so `python -m docker_mcp` works.

### Server singleton (`docker_mcp/server.py`)
`docker_mcp/server.py` instantiates `FastMCP` and exports three things:

- **`tool`** — the registration decorator every tool module uses. **Always import `tool` from `docker_mcp.server`** and decorate with `@tool()`; never import from the `mcp` package directly in tool files (circular import) and never use `@mcp.tool()` in tool modules.
- **`prompt`** — the prompt registration decorator `prompts.py` uses (`@prompt(description=..., domain=...)`), analogous to `tool` and gating on `DOCKER_MCP_DISABLE`; never use `@mcp.prompt()` directly in `prompts.py`.
- **`mcp`** — the FastMCP singleton, imported by `resources.py` for `@mcp.resource()`.

```python
from docker_mcp.server import tool     # tool modules
from docker_mcp.server import prompt    # prompt modules (with domain=...)
from docker_mcp.server import mcp       # resource modules only
```

`server.py` also owns **`TOOL_CATEGORIES`**, the central map classifying every tool as `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`. The `@tool()` decorator uses it to attach MCP `ToolAnnotations` and to skip registration under the env switches `DOCKER_MCP_READONLY` (register only read-only tools) and `DOCKER_MCP_NO_DESTRUCTIVE` (register everything except destructive). It also records each tool's **domain** (its defining module's leaf, e.g. `containers`) so the orthogonal `DOCKER_MCP_DISABLE=<domains>` switch can drop whole feature areas — including that domain's **prompts** (via the `prompt(domain=...)` helper) and its **doc-resource sections** (via `_SECTION_DOMAINS` in `resources.py`), not just its tools; the live snapshot is the `docker-mcp://tool-catalog` resource (`server.tool_catalog()`). **Every new tool needs a `TOOL_CATEGORIES` entry** — `tests/test_server.py` fails the build if the map drifts from the registered set.

### Tools package (`docker_mcp/tools/`)
Each file maps to one Docker SDK domain or one CLI/registry feature area. Underscore-prefixed modules are private helpers excluded from the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `_cli.py` | Cross-platform subprocess helper (private) | — |
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

An additional distribution channel alongside the uvx-from-git install (unchanged). One ARG-gated multi-stage `Dockerfile` builds `full` (docker CLI + compose + buildx + scout) and `no-scout` (sets `DOCKER_MCP_DISABLE=scout` so absent-plugin scout tools don't register), published on each GitHub Release via `.github/workflows/publish-images.yaml` — always to GHCR, and mirrored to Docker Hub (plus a `DOCKERHUB.md`→Hub-description sync — a slim container-focused readme, as the full `README.md` exceeds Hub's 25 KB cap) when the opt-in `DOCKERHUB_USER`/`DOCKERHUB_TOKEN` secrets are set — the Hub token must have `read/write/delete` scope (build/measure on PRs/pushes is the separate `images.yaml`); `lite` (`INSTALL_CLI=0`) is buildable but not published. Two container-aware guards live behind `_utils.in_container()` (true when `/.dockerenv` exists or `DOCKER_MCP_IN_CONTAINER=1`) and are **inert on the host install** — keep them in mind when editing `_utils.py` or the file-path tools:

- **Filesystem guard** — `assert_host_writable` (hooked into `stream_to_file`) refuses a `*_to_file` write to a path that isn't a host bind mount (it would be lost on `--rm`); `host_read_path` enriches the read-side "missing file" case.
- **Self-termination guard** — `client.startup_preflight()` (called from `main()`) pins the server's own container id and prints OS-aware socket hints to stderr; `client.guard_not_self` stops the destructive container-lifecycle tools (`remove`/`kill`/`stop`/`restart`/`pause_container`) from acting on the server's own container (override `DOCKER_MCP_ALLOW_SELF_TERMINATE=1`).

## Conventions

- New Docker functionality goes in the matching `docker_mcp/tools/<domain>.py` — do not create new tool files without a corresponding entry in `docker_mcp/tools/__init__.py` and a matching test file.
- Tool functions are decorated with `@tool()` (imported from `docker_mcp.server`) and **must have a `TOOL_CATEGORIES` entry** in `docker_mcp/server.py`.
- Line length limit: 120 characters.
- Do not add comments that describe what the code does — only add comments for non-obvious constraints or workarounds.

### Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) so the agent/operator can later enumerate that footprint (the `managed_only=True` arg on `list_containers` / `list_networks` / `list_volumes` / `list_services`, or `--filter label=docker-mcp-server.managed=true`; the `prune_managed` prompt removes only the managed footprint). On by default; opt out with `DOCKER_MCP_NO_LABELS=1`. When adding a new create tool that accepts a `labels` dict, route it through `docker_mcp/tools/_labels.py:with_provenance(labels, "<tool_name>")` — it merges provenance without overwriting caller keys and returns `None` (drop it via `drop_none`) when stamping is disabled and the caller passed nothing. **Image builds are intentionally not stamped** (a build label changes the image digest).

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

`docker_mcp/tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data: the Docker SDK for Python documentation under the `docker-docs://` URI scheme, `docker-mcp://tool-catalog` (the live tool/domain/category snapshot), and the container-observability resources `docker://containers` / `docker-logs://{id_or_name}` / `docker-stats://{id_or_name}` (the last reuse `containers.py`'s private `_read_log_tail` / `_read_stats_summary` and refuse when the `containers` domain is disabled). `_SECTION_DOMAINS` maps each doc section to a domain so `DOCKER_MCP_DISABLE` hides a disabled domain's sections (registered with the server via `register_resource_domains`). Use the same docstring format as tools.

### MCP prompts

`docker_mcp/tools/prompts.py` exposes `@prompt(description=..., domain=...)` templates (the `prompt` helper from `docker_mcp.server`, not `@mcp.prompt` directly) that return prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Each declares its primary `domain` so `DOCKER_MCP_DISABLE` drops it with that domain; `domain=None` for general/cross-domain prompts. Use the same docstring format as tools.

## Docker SDK Policy

**Only use `docker` module methods that are documented in the official reference.**
Always verify the exact method name, parameter names, and return type at https://docker-py.readthedocs.io/en/stable/ before writing or suggesting code. Do not suggest methods that sound plausible but are not in the docs.

When the high-level SDK lacks a method (e.g. swarm node removal, service rollback), use the low-level `APIClient` via `_get_client().api` (`remove_node`, `update_service`, `inspect_service`, …), documented at https://docker-py.readthedocs.io/en/stable/api.html — verified the same way. Prefer the high-level object API where it exists.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html
Docker SDK GitHub: https://github.com/docker/docker-py
