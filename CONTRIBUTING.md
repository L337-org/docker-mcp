# Contributing to docker-mcp-server

Contributions are welcome. The project values a tight mapping between the Docker SDK's public surface and the MCP tools we expose.

## Project layout

```
.
├── docker_mcp/            # the package — `python -m docker_mcp` runs the server
│   ├── __init__.py        # defines `main()`; side-effect-imports `server` and `tools`
│   ├── __main__.py        # calls `main()` so `python -m docker_mcp` works
│   ├── server.py          # creates the FastMCP singleton (`mcp`) shared by every tool module
│   └── tools/             # one file per Docker SDK domain or CLI/registry feature
│       ├── _cli.py        # cross-platform subprocess helper for docker CLI shell-outs (private)
│       ├── _utils.py      # shared helpers (drop_none, join_bounded, MAX_PAYLOAD_BYTES) (private)
│       ├── system.py      # DockerClient connection + lazy `_get_client()` helper
│       ├── containers.py
│       ├── images.py
│       ├── networks.py
│       ├── volumes.py
│       ├── configs.py
│       ├── secrets.py
│       ├── nodes.py
│       ├── services.py
│       ├── swarm.py
│       ├── plugins.py
│       ├── compose.py     # `docker compose` CLI plugin (shells out via _cli.py)
│       ├── stack.py       # `docker stack` (Compose-on-Swarm) CLI (shells out via _cli.py)
│       ├── context.py     # `docker context` CLI (shells out via _cli.py)
│       ├── buildx.py      # `docker buildx` CLI plugin (shells out via _cli.py)
│       ├── scout.py       # `docker scout` CLI plugin (shells out via _cli.py)
│       ├── registry.py    # OCI v2 registries + Docker Hub HTTPS APIs (no daemon)
│       ├── prompts.py     # @prompt(...) templates for common docker workflows
│       └── resources.py   # @mcp.resource() endpoints exposing SDK + CLI + registry docs
├── tests/                 # pytest suite, mirrors `docker_mcp/tools/` one-to-one
│   └── integration/       # tests that hit a real Docker daemon or docker.io
├── assets/                # bundle assets (e.g. the .mcpb icon) packed into the Desktop Extension
├── scripts/               # developer convenience scripts (not used by CI) — e.g. build-mcpb.sh
└── dist/                  # build output (git-ignored) — local .mcpb test bundles land here
```

Each `docker_mcp/tools/<file>.py` has a matching `tests/test_<file>.py`. New modules must be added to `docker_mcp/tools/__init__.py` and have a corresponding test file. Tool modules that wrap CLI features must funnel every subprocess call through `docker_mcp/tools/_cli.py` so the cross-platform safety concerns (binary discovery, no shell, UTF-8 decoding, output capping, Windows console suppression, env scrubbing) live in one place.

## Conventions

Tool functions are decorated with `@tool()` (the project's wrapper around `@mcp.tool()`, imported from `docker_mcp.server`) and follow this docstring style:

```python
from docker_mcp.server import tool


@tool()
def mcp_example(name: str):
    """
    Say hello to someone by name.

    args: name - The name to say hello to
    returns: str - The greeting
    """
    return f"Hello, {name}!"
```

- Tool modules import `tool` from `docker_mcp.server`; prompt modules import `prompt` from `docker_mcp.server`. Only resource modules (`@mcp.resource(...)`) import `mcp` directly — never import `mcp` directly in a tool or prompt module, that creates a circular import.
- Every tool needs a `TOOL_CATEGORIES` entry in `docker_mcp/server.py` (`READ_ONLY` / `MUTATING` / `DESTRUCTIVE`); the central map drives the tool's `ToolAnnotations` and the read-only env switches, and `tests/test_server.py` fails if it drifts from the registered set. A tool's *domain* (for `DOCKER_MCP_SERVER_DISABLE` and the tool catalog) is derived automatically from its module name, so putting a tool in the right `docker_mcp/tools/<domain>.py` file is all that's needed.
- Line length is 120 characters (enforced by ruff).
- CLI shell-outs must go through `docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper enforces `shell=False`, resolves the binary via `shutil.which` (cross-platform), decodes output as UTF-8 with replace, caps the captured bytes, scrubs the environment, and suppresses console pop-ups on Windows.
- A tool with non-obvious behavior (side effects, preconditions, a non-obvious failure mode, or overlap with another tool) needs more than a one-line summary — add a short paragraph covering when to use it vs. the alternative, side effects/preconditions, and concrete parameter formats. Verify any factual claim against the live docker-py docs / Engine API spec rather than assuming; see [`CLAUDE.md`](CLAUDE.md)'s "Tool function format" section for the full convention and why it matters.

## Checklist when adding a new tool module

When you add a new `docker_mcp/tools/<domain>.py`, also update:

1. **`docker_mcp/tools/__init__.py`** — star-import the module (private helpers prefixed with `_` are excluded).
2. **`tests/test_<domain>.py`** — unit tests using mocks (no real daemon).
3. **`tests/integration/test_<domain>.py`** — at least one happy-path test against a real daemon (or override the `skip_if_no_daemon` fixture if the module doesn't need one).
4. **`docker_mcp/tools/prompts.py`** — at least one `@prompt(...)` template that exercises the new tools end-to-end.
5. **`docker_mcp/tools/resources.py`** — add an entry under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` if the new domain has authoritative docs the agent should be able to read at runtime.
6. **README.md** — append to the "What the agent can do" list and (if relevant) the "Security considerations" section.
7. **SECURITY.md** — only if the new module exposes a new class of risk not already covered by the README's Security section.

## Verifying the SDK before writing code

To prevent hallucinated method names, the project includes a `/docker-sdk` Claude Code skill that fetches the live Docker SDK for Python documentation, inventories what's already exposed, and produces a gap analysis. Run it before adding new tools:

```
/docker-sdk                    # full gap analysis
/docker-sdk containers         # focus on a single domain
```

## Local development

```bash
# install dependencies (creates .venv, installs runtime + dev deps)
uv sync

# run the server
uv run python -m docker_mcp
# …or via the installed console script
uv run docker-mcp

# unit tests (integration tests are excluded by default)
uv run pytest -v

# integration tests — require a running Docker daemon at $DOCKER_HOST
uv run pytest -m integration -v

# lint, format, type-check
uv run ruff check .
uv run ruff format .
uv run pyright

# install the pre-commit hook (one-time, runs ruff on every commit)
uv run pre-commit install

# add a runtime dependency
uv add <package>

# add a development dependency
uv add --group dev <package>
```

CI runs `pytest` (unit + integration), `ruff` (lint + format check), and `pyright` on every pull request and push to `main` via `.github/workflows/premerge.yaml`.

## Building a local Desktop Extension (.mcpb)

To smoke-test the Claude Desktop Extension locally, pack a bundle with the developer helper in `scripts/`:

```bash
# pack dist/docker-mcp-server-<version>.mcpb (auto-increments -1, -2, … if it exists)
scripts/build-mcpb.sh

# …or give it an explicit name (a .mcpb extension is added if missing)
scripts/build-mcpb.sh my-test-bundle
```

It reads the version from `pyproject.toml`, creates `dist/` if needed, writes a `.sha256` alongside the bundle, and packs via Anthropic's `mcpb` CLI (a global `mcpb`, else `npx @anthropic-ai/mcpb`; see `--help` for the `MCPB=` override). The official release bundle is built separately by the `mcpb` job in `.github/workflows/publish.yaml` — this script is for local testing only and is **not** used by CI.

## Reporting issues

Bug reports and feature requests have templates that you can choose when you [`create an issue`](https://github.com/GavinLucas/docker-mcp/issues/new/choose). Please select the correct issue type and follow the template.
