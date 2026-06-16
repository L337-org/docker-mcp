# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`docker-mcp` is a Python MCP server (requires Python >=3.14) managed with `uv` that exposes the Docker SDK for Python as MCP tools. The entry point is the `docker_mcp` package, run with `python -m docker_mcp` or via the installed console script. It is **published to PyPI as `docker-mcp-server`** (the `docker-mcp` name was already taken) and as a container image to GHCR (`ghcr.io/gavinlucas/docker-mcp-server`), mirrored to Docker Hub (`gavinlucas/docker-mcp-server`) when the opt-in `DOCKERHUB_*` release secrets are configured; the import package stays `docker_mcp` and the repo stays `…/docker-mcp`. Two console scripts are installed — `docker-mcp` and `docker-mcp-server` — both targeting `docker_mcp:main`.

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

## Architecture

### Entry point
The `docker_mcp` package is the entry point. `docker_mcp/__init__.py` defines `main()` and side-effect-imports the `server` and `tools` submodules (which registers all `@tool()` decorators). `docker_mcp/__main__.py` calls `main()` so `python -m docker_mcp` works; the installed `docker-mcp` console script also targets `docker_mcp:main`.

### Server singleton (`docker_mcp/server.py`)
Instantiates `FastMCP`, exports the `mcp` object, and exports the `tool` and `prompt` registration helpers. **Tool modules import `tool`; prompt modules import `prompt`** — both gate on `DOCKER_MCP_DISABLE` (never import from `mcp` directly in those modules — that would create circular imports). `@mcp.resource()` modules still import `mcp` (plus `is_domain_disabled` / `register_resource_domains` for section gating).

```python
from docker_mcp.server import tool     # tool modules
from docker_mcp.server import prompt   # prompt modules (with domain=...)
from docker_mcp.server import mcp      # resource modules
```

`server.py` also owns the central **`TOOL_CATEGORIES`** map (every tool name → `READ_ONLY` / `MUTATING` / `DESTRUCTIVE`). The `@tool()` decorator uses it to (a) attach `ToolAnnotations` (`readOnlyHint` / `destructiveHint`, plus `idempotentHint` for the prune family) and (b) skip registration entirely under the read-only env switches `DOCKER_MCP_READONLY` (only read-only tools) and `DOCKER_MCP_NO_DESTRUCTIVE` (everything except destructive). Every registered tool must have a `TOOL_CATEGORIES` entry — `tests/test_server.py` fails if the map and the registered set drift.

The decorator also records each tool's **domain** — the leaf of its defining module (`docker_mcp.tools.containers` → `containers`) — so the orthogonal `DOCKER_MCP_DISABLE=<domains>` switch can drop a whole feature area (e.g. `swarm,plugins`) from the registered surface regardless of category. A tool registers only if its category survives the read-only switches *and* its domain is not disabled. `DOCKER_MCP_DISABLE` reaches beyond tools: the `prompt(domain=...)` helper skips a disabled domain's prompts, and `resources.py` hides a disabled domain's doc sections — so disabling e.g. `scout` drops its tools, its prompts, and its `docker-docs://scout` sections together. The full picture (every tool's domain/category, plus the `prompts` list and `disabled_doc_sections`) is exposed via `tool_catalog()` and the `docker-mcp://tool-catalog` resource, so the classification is auditable at runtime, not just in the source map.

### Tools package (`docker_mcp/tools/`)
Each file maps to one Docker SDK domain (or, for CLI-only and registry-only features, one Docker feature area) and contains `@tool()` decorated functions. `docker_mcp/tools/__init__.py` imports all public modules with `*` so `docker_mcp/__init__.py` only needs `from docker_mcp import tools`. Underscore-prefixed modules (`_cli.py`, `_utils.py`) are private helpers and stay out of the star-import.

| File | Domain | Backed by |
|------|--------|-----------|
| `docker_mcp/tools/_cli.py` | Cross-platform subprocess helper (private) | — |
| `docker_mcp/tools/_utils.py` | Shared helpers (private) | — |
| `docker_mcp/tools/_labels.py` | Provenance labels stamped on created resources (private) | — |
| `docker_mcp/tools/client.py` | `DockerClient` — connection and low-level client | docker-py |
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

An additional distribution channel alongside uvx-from-git (which is unchanged). One multi-stage `Dockerfile` builds variants via build args (`INSTALL_CLI`, `INSTALL_SCOUT`, `DISABLE_DOMAINS`): `full` (docker CLI + compose + buildx + scout) and `no-scout` (sets `DOCKER_MCP_DISABLE=scout` so the absent-plugin scout tools don't register) are **published to GHCR (and mirrored to Docker Hub when the Hub secrets are set) on each GitHub Release** — the same tags on each registry (`full` → `:latest`/`:<version>`, `no-scout` → `:no-scout`/`:<version>-no-scout`); `lite` (`INSTALL_CLI=0`, docker-py SDK only — CLI domains degrade via `has_plugin()`) is buildable but not published. `.github/workflows/images.yaml` builds+measures on PRs/pushes to main; `.github/workflows/publish-images.yaml` pushes multi-arch images on a GitHub Release (kept a separate workflow so it never shows as a skipped check on PRs) — always to GHCR, and mirrored to Docker Hub (`gavinlucas/docker-mcp-server`, plus a `DOCKERHUB.md`→Hub-description sync — a slim container-focused readme, since the full `README.md` exceeds Hub's 25 KB cap) when the opt-in `DOCKERHUB_USER`/`DOCKERHUB_TOKEN` secrets are set (the Hub token needs `read/write/delete` scope or the description PATCH 403s); with them unset, only GHCR is pushed so a release never fails for lack of Hub credentials. Two container-aware guards live behind `_utils.in_container()` (true when `/.dockerenv` exists or `DOCKER_MCP_IN_CONTAINER=1`, set in the image) and are **inert on the host install**:

- **Filesystem guard** (`_utils.py`): `assert_host_writable` (hooked into `stream_to_file`) refuses a `*_to_file` write to a path that isn't a host bind mount (silent loss on `--rm`); `host_read_path` enriches the "missing file" case on reads. `_host_backed` parses `/proc/self/mountinfo`.
- **Self-termination guard** (`client.py`): `startup_preflight()` (called from `main()`) pings the daemon, prints OS-aware socket hints to **stderr** (never stdout — that's the stdio channel) on failure, and pins the server's own container id; `guard_not_self` then makes the destructive container-lifecycle tools refuse to act on self (override: `DOCKER_MCP_ALLOW_SELF_TERMINATE=1`).

## Conventions

- New Docker functionality goes in the matching `docker_mcp/tools/<domain>.py` file, not in a new file.
- Every new `docker_mcp/tools/` file must be imported in `docker_mcp/tools/__init__.py` (private `_*.py` helpers excluded).
- Every new `docker_mcp/tools/<module>.py` must have a matching `tests/test_<module>.py`.
- Tool functions are decorated with `@tool()` (imported from `docker_mcp.server`) and must have a `TOOL_CATEGORIES` entry in `docker_mcp/server.py`.
- Line length limit: 120 characters (enforced by ruff and flake8).

## Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) via `docker_mcp/tools/_labels.py`, so the agent/operator can later enumerate that footprint — the `managed_only=True` arg on `list_containers` / `list_networks` / `list_volumes` / `list_services`, or `--filter label=docker-mcp-server.managed=true`. The `prune_managed` prompt tears down only the managed footprint. Stamping is **on by default** and additive (a caller-supplied label always wins on a key collision); `DOCKER_MCP_NO_LABELS=1` turns it off. The prefix is the bare project name (deliberately not reverse-DNS) and is a single constant in `_labels.py`.

When adding a new create tool that accepts a `labels` dict, route it through `_labels.py:with_provenance(labels, "<tool_name>")` (it accepts the dict/list/None shapes the SDK accepts and returns `None` — feed it through `drop_none` — when stamping is off and the caller passed nothing). The six stamped creators today are `run_container`, `create_container`, `create_network`, `create_volume`, `create_service` (service-level `labels` only, not `container_labels`), `create_config`, `create_secret`. **Image builds are intentionally NOT stamped** — a build label changes the resulting image digest. Compose/stack containers (created via CLI shell-out) are also unstamped. New `managed_only`-style label filters go through `_labels.py:managed_filter`.

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
- **Parsed-query tools** (`context_ls`, `buildx_ls`, `buildx_du`, `compose_ls`) return a parsed list/dict and therefore *cannot* return a useful partial result on failure — they raise `RuntimeError` via `_cli.py:raise_on_cli_failure`. (`compose_ps` is the hybrid: it returns `{"services": [...], "raw": <CliResult dict>}` so the caller gets both.)

New CLI tools should pick the style matching their return shape rather than mixing them.

## Checklist when adding a new tool module

When you add a new `docker_mcp/tools/<domain>.py` (especially for CLI features outside docker-py), update **all** of these — easy to miss:

1. `docker_mcp/tools/__init__.py` — star-import.
2. `docker_mcp/server.py` — add a `TOOL_CATEGORIES` entry for every new tool (`READ_ONLY` / `MUTATING` / `DESTRUCTIVE`); `tests/test_server.py` fails otherwise.
3. `tests/test_<domain>.py` — unit tests using mocks.
4. `tests/integration/test_<domain>.py` — at least one happy-path test against a real daemon (override `skip_if_no_daemon` if the module doesn't need one).
5. `docker_mcp/tools/prompts.py` — at least one `@mcp.prompt()` template using the new tools.
6. `docker_mcp/tools/resources.py` — add a section under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` pointing at the authoritative docs.
7. `README.md` — append to "What the agent can do" and "Security considerations" (the latter only if a new class of risk is introduced).
8. `SECURITY.md` — only if a new class of risk is introduced beyond what's already documented.

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

### MCP resources

`docker_mcp/tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data: the Docker SDK for Python documentation under the `docker-docs://` URI scheme, plus `docker-mcp://tool-catalog` (the live tool/domain/category snapshot from `server.tool_catalog()`). Each doc section maps to a domain via `_SECTION_DOMAINS` (registered with the server through `register_resource_domains`), so `DOCKER_MCP_DISABLE` hides a disabled domain's sections from `docker-docs://contents` and makes `get_docs_section` refuse them. Resources follow the same docstring format as tools and are also star-imported via `docker_mcp/tools/__init__.py`.

### MCP prompts

`docker_mcp/tools/prompts.py` exposes `@prompt(description=..., domain=...)` templates (the `prompt` helper imported from `docker_mcp.server`, **not** `@mcp.prompt` directly) that return rendered prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, audit/security, networking, volume backup/restore, doc lookup). Each prompt declares its primary `domain` so `DOCKER_MCP_DISABLE` skips it when that domain is off; use `domain=None` for general / cross-domain prompts (doc lookup, prune, disk usage) that should always register. Prompts follow the same docstring format as tools and are star-imported via `docker_mcp/tools/__init__.py`.

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
