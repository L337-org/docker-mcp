# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`docker-mcp` is a Python MCP server (requires Python >=3.14) managed with `uv` that exposes the Docker SDK for Python as MCP tools. The entry point is `main.py`.

## Commands

```bash
# Install dependencies
uv sync

# Run the project
uv run python main.py

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

## Architecture

### Entry point
`main.py` imports `server` and `tools`, then calls `mcp.run(transport="stdio")`.

### Server singleton (`server.py`)
Instantiates `FastMCP` and exports the `mcp` object. **All tool modules import `mcp` from here** — never import from `mcp` directly in tool files, as this would create circular imports.

```python
from server import mcp
```

### Tools package (`tools/`)
Each file maps to one Docker SDK domain (or, for CLI-only and registry-only features, one Docker feature area) and contains `@mcp.tool()` decorated functions. `tools/__init__.py` imports all public modules with `*` so `main.py` only needs `import tools`. Underscore-prefixed modules (`_cli.py`, `_utils.py`) are private helpers and stay out of the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `tools/_cli.py` | Cross-platform subprocess helper (private) | — |
| `tools/_utils.py` | Shared helpers (private) | — |
| `tools/client.py` | `DockerClient` — connection and low-level client | docker-py |
| `tools/containers.py` | Container lifecycle and management | docker-py |
| `tools/images.py` | Image pull, build, push, inspect | docker-py |
| `tools/networks.py` | Network create, connect, inspect | docker-py |
| `tools/volumes.py` | Volume create, list, remove | docker-py |
| `tools/configs.py` | Swarm configs | docker-py |
| `tools/nodes.py` | Swarm nodes | docker-py |
| `tools/plugins.py` | Plugin install and management | docker-py |
| `tools/secrets.py` | Swarm secrets | docker-py |
| `tools/services.py` | Swarm services | docker-py |
| `tools/swarm.py` | Swarm init, join, leave | docker-py |
| `tools/compose.py` | Docker Compose v2 | `docker compose` CLI via `_cli.py` |
| `tools/context.py` | Docker CLI contexts | `docker context` CLI via `_cli.py` |
| `tools/buildx.py` | Buildx / BuildKit (multi-arch builds, imagetools — supersedes `docker manifest`) | `docker buildx` CLI via `_cli.py` |
| `tools/scout.py` | Vulnerability scanning, SBOMs, base-image recommendations | `docker scout` CLI via `_cli.py` |
| `tools/registry.py` | OCI v2 registries + Docker Hub (with 429 retry policy) | HTTPS via `httpx` (no daemon) |
| `tools/prompts.py` | `@mcp.prompt()` workflow templates | — |
| `tools/resources.py` | `@mcp.resource()` doc endpoints | — |

### Tests (`tests/`)
Each `tools/<module>.py` has a corresponding `tests/test_<module>.py`. Tests use pytest. The `tests/__init__.py` is intentionally empty.

`tests/integration/` holds tests that hit a real Docker daemon. `tests/integration/conftest.py` auto-marks every test in the directory with `@pytest.mark.integration` (excluded by default via `addopts = "-m 'not integration'"` in `pyproject.toml`) and provides an autouse `skip_if_no_daemon` fixture so the suite skips cleanly when no daemon is reachable. Run with `uv run pytest -m integration`.

## Conventions

- New Docker functionality goes in the matching `tools/<domain>.py` file, not in a new file.
- Every new `tools/` file must be imported in `tools/__init__.py` (private `_*.py` helpers excluded).
- Every new `tools/<module>.py` must have a matching `tests/test_<module>.py`.
- Tool functions are decorated with `@mcp.tool` and imported from `server.py`.
- Line length limit: 120 characters (enforced by ruff and flake8).

## CLI shell-out policy

Any tool that wraps a `docker` CLI feature (Compose, Context, Buildx, Scout, etc.) MUST go through `tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper centralizes:

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

## Checklist when adding a new tool module

When you add a new `tools/<domain>.py` (especially for CLI features outside docker-py), update **all** of these — easy to miss:

1. `tools/__init__.py` — star-import.
2. `tests/test_<domain>.py` — unit tests using mocks.
3. `tests/integration/test_<domain>.py` — at least one happy-path test against a real daemon (override `skip_if_no_daemon` if the module doesn't need one).
4. `tools/prompts.py` — at least one `@mcp.prompt()` template using the new tools.
5. `tools/resources.py` — add a section under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` pointing at the authoritative docs.
6. `README.md` — append to "What the agent can do" and "Security considerations" (the latter only if a new class of risk is introduced).
7. `SECURITY.md` — only if a new class of risk is introduced beyond what's already documented.

### Tool function format

All `@mcp.tool` functions must follow this exact docstring format:

```python
@mcp.tool()
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

### MCP resources

`tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data — currently the Docker SDK for Python documentation under the `docker-docs://` URI scheme. Resources follow the same docstring format as tools and are also star-imported via `tools/__init__.py`.

### MCP prompts

`tools/prompts.py` exposes `@mcp.prompt(description=...)` templates that return rendered prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, doc lookup). Prompts follow the same docstring format as tools and are star-imported via `tools/__init__.py`.

## Docker SDK Policy

**Before writing or modifying any code that calls the Docker SDK (`docker` package), you MUST run `/docker-sdk` (or `/docker-sdk <topic>`) to:**
1. Verify exact method signatures from the live Docker SDK for Python documentation
2. Confirm parameter names and return types before writing code
3. Never use a `docker` module method that has not been confirmed in the docs

Do not assume any method exists because it sounds plausible. If you cannot confirm it from the documentation, say so and do not use it.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html  
Docker SDK GitHub: https://github.com/docker/docker-py
