# GitHub Copilot Instructions

This file provides guidance to GitHub Copilot when working with code in this repository.

## Project

`docker-mcp` is a Python MCP (Model Context Protocol) server that exposes the Docker SDK for Python — plus selected docker CLI features (Compose, Context, Buildx, Scout) and direct OCI-registry HTTPS access — as MCP tools. It requires Python >=3.14 and is managed with `uv`.

## Architecture

### Entry point
The `docker_mcp` package is the entry point. `docker_mcp/__init__.py` defines `main()` and side-effect-imports the `server` and `tools` submodules (which registers all `@tool()` decorators); `docker_mcp/__main__.py` calls `main()` so `python -m docker_mcp` works.

### Server singleton (`docker_mcp/server.py`)
`docker_mcp/server.py` instantiates `FastMCP` and exports two things:

- **`tool`** — the registration decorator every tool module uses. **Always import `tool` from `docker_mcp.server`** and decorate with `@tool()`; never import from the `mcp` package directly in tool files (circular import) and never use `@mcp.tool()` in tool modules.
- **`mcp`** — the FastMCP singleton, imported only by `prompts.py` / `resources.py` for `@mcp.prompt()` / `@mcp.resource()`.

```python
from docker_mcp.server import tool   # tool modules
from docker_mcp.server import mcp    # prompts / resources only
```

`server.py` also owns **`TOOL_CATEGORIES`**, the central map classifying every tool as `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`. The `@tool()` decorator uses it to attach MCP `ToolAnnotations` and to skip registration under the env switches `DOCKER_MCP_READONLY` (register only read-only tools) and `DOCKER_MCP_NO_DESTRUCTIVE` (register everything except destructive). It also records each tool's **domain** (its defining module's leaf, e.g. `containers`) so the orthogonal `DOCKER_MCP_DISABLE=<domains>` switch can drop whole feature areas; the live snapshot is the `docker-mcp://tool-catalog` resource (`server.tool_catalog()`). **Every new tool needs a `TOOL_CATEGORIES` entry** — `tests/test_server.py` fails the build if the map drifts from the registered set.

### Tools package (`docker_mcp/tools/`)
Each file maps to one Docker SDK domain or one CLI/registry feature area. Underscore-prefixed modules are private helpers excluded from the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `_cli.py` | Cross-platform subprocess helper (private) | — |
| `_utils.py` | Shared helpers: `drop_none`, `join_bounded`, `stream_to_file`, `close_stream_quietly`, `MAX_PAYLOAD_BYTES` (private) | — |
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
| `context.py` | Docker CLI contexts | `docker context` CLI via `_cli.py` |
| `buildx.py` | Buildx / BuildKit | `docker buildx` CLI via `_cli.py` |
| `scout.py` | Vulnerability scanning, SBOMs | `docker scout` CLI via `_cli.py` |
| `registry.py` | OCI v2 registries + Docker Hub | HTTPS via `httpx` (no daemon) |
| `prompts.py` | `@mcp.prompt()` workflow templates | — |
| `resources.py` | `@mcp.resource()` doc endpoints | — |

`docker_mcp/tools/__init__.py` star-imports all public modules so `docker_mcp/__init__.py` only needs `from docker_mcp import tools`.

### Tests (`tests/`)
Each `docker_mcp/tools/<module>.py` has a corresponding `tests/test_<module>.py`; `tests/test_server.py` covers the classification/registration machinery. Tests use pytest with mocks. `tests/integration/` holds tests that need a real Docker daemon — excluded by default, run with `uv run pytest -m integration`. `tests/conftest.py` clears the `DOCKER_MCP_*` env switches so the suite is hermetic.

## Conventions

- New Docker functionality goes in the matching `docker_mcp/tools/<domain>.py` — do not create new tool files without a corresponding entry in `docker_mcp/tools/__init__.py` and a matching test file.
- Tool functions are decorated with `@tool()` (imported from `docker_mcp.server`) and **must have a `TOOL_CATEGORIES` entry** in `docker_mcp/server.py`.
- Line length limit: 120 characters.
- Do not add comments that describe what the code does — only add comments for non-obvious constraints or workarounds.

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

`docker_mcp/tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data: the Docker SDK for Python documentation under the `docker-docs://` URI scheme, plus `docker-mcp://tool-catalog` (the live tool/domain/category snapshot). Use the same docstring format as tools.

### MCP prompts

`docker_mcp/tools/prompts.py` exposes `@mcp.prompt(description=...)` templates that return prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Use the same docstring format as tools.

## Docker SDK Policy

**Only use `docker` module methods that are documented in the official reference.**
Always verify the exact method name, parameter names, and return type at https://docker-py.readthedocs.io/en/stable/ before writing or suggesting code. Do not suggest methods that sound plausible but are not in the docs.

When the high-level SDK lacks a method (e.g. swarm node removal, service rollback), use the low-level `APIClient` via `_get_client().api` (`remove_node`, `update_service`, `inspect_service`, …), documented at https://docker-py.readthedocs.io/en/stable/api.html — verified the same way. Prefer the high-level object API where it exists.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html
Docker SDK GitHub: https://github.com/docker/docker-py
