# Contributing to docker-mcp-server

Contributions are welcome. The project values a tight mapping between the Docker SDK's public surface and the MCP tools we expose. By participating, you're expected to uphold the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

This is an [MCP](https://modelcontextprotocol.io) server: it exposes Docker operations as `@tool()`-decorated Python functions that an AI agent calls over the Model Context Protocol. If that sentence is new to you, the short version is: each function in `docker_mcp/tools/` becomes one callable "tool" an agent can invoke, with the function's docstring as the description the agent sees and its type-annotated parameters as the input schema.

The practical mental model for finding your way around:

- One file per Docker feature area (`containers.py`, `images.py`, `swarm.py`, …), each backed by either the [docker-py SDK](https://docker-py.readthedocs.io/en/stable/) or a `docker` CLI shell-out (`compose.py`, `buildx.py`, …). See the tree below for the full map.
- Every tool function is registered centrally in `docker_mcp/server.py`, which is also where read-only/destructive classification, naming enforcement, and the domain-disable switches live.
- `CLAUDE.md` (repo root) is the full architecture reference — written to brief Claude Code, but equally the canonical source for humans. This file (`CONTRIBUTING.md`) covers the practical day-to-day: setup, the checklist for adding a tool, testing, and submitting changes. If something isn't covered here, it's almost certainly in `CLAUDE.md`.

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

New Docker functionality goes in the matching existing file (e.g. a new volume operation goes in `volumes.py`), not a new file — a new `docker_mcp/tools/<domain>.py` is only for a Docker feature area that doesn't exist in the tree above.

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
- Tool names follow `<management-command>_<verb>`, anchored to the docker CLI's own management-command structure (`docker container ls` → `container_list`), with long-form verbs (`list`/`remove`/`inspect`, never `ls`/`rm`/`get`). `tests/test_naming.py` enforces this — a new tool with a short-form verb or an unapproved prefix fails CI. See `CLAUDE.md`'s "Tool naming convention" section for the full rule, including identifier parameter naming (`id_or_name`, `name`/`names`, `repository`).
- Line length is 120 characters (enforced by ruff).
- CLI shell-outs must go through `docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper enforces `shell=False`, resolves the binary via `shutil.which` (cross-platform), decodes output as UTF-8 with replace, caps the captured bytes, scrubs the environment, and suppresses console pop-ups on Windows.
- A tool with non-obvious behavior (side effects, preconditions, a non-obvious failure mode, or overlap with another tool) needs more than a one-line summary — add a short paragraph covering when to use it vs. the alternative, side effects/preconditions, and concrete parameter formats. Verify any factual claim against the live docker-py docs / Engine API spec rather than assuming; see [`CLAUDE.md`](CLAUDE.md)'s "Tool function format" section for the full convention and why it matters.

## Verifying the SDK before writing code

To prevent hallucinated method names, the project includes a `/docker-sdk` Claude Code skill that fetches the live Docker SDK for Python documentation, inventories what's already exposed, and produces a gap analysis. Run it before adding new tools:

```
/docker-sdk                    # full gap analysis
/docker-sdk containers         # focus on a single domain
```

If you're not using Claude Code, check method names and signatures directly against the [docker-py docs](https://docker-py.readthedocs.io/en/stable/index.html) (or, for SDK gaps, the [low-level `APIClient`](https://docker-py.readthedocs.io/en/stable/api.html)) before writing a call — don't assume a method exists because it sounds plausible.

## Testing conventions

Unit tests mock the Docker client rather than hitting a real daemon, patching `_get_client` (or, for CLI-backed modules, `run_docker`) at the point the tool module looks it up. A minimal example, from `tests/test_volumes.py`:

```python
from unittest.mock import MagicMock, patch

from docker_mcp.tools.volumes import volume_inspect


def test_volume_inspect():
    volume = MagicMock()
    volume.attrs = {"Name": "myvol"}
    with patch("docker_mcp.tools.volumes._get_client") as mock_client:
        mock_client.return_value.volumes.get.return_value = volume
        assert volume_inspect("myvol") == {"Name": "myvol"}
```

Integration tests (`tests/integration/`) call the real tool function against an actual Docker daemon and are auto-marked `@pytest.mark.integration` by `tests/integration/conftest.py`, which also provides an autouse `skip_if_no_daemon` fixture — they're skipped automatically (not failed) when no daemon is reachable, so it's safe to run the full suite without Docker running. They're excluded from the default `pytest` invocation (`-m 'not integration'` in `pyproject.toml`); run them explicitly with `uv run pytest -m integration -v`.

## Checklist when adding a new tool module

When you add a new `docker_mcp/tools/<domain>.py`, also update:

1. **`docker_mcp/tools/__init__.py`** — star-import the module (private helpers prefixed with `_` are excluded).
2. **`tests/test_<domain>.py`** — unit tests using mocks (no real daemon).
3. **`tests/integration/test_<domain>.py`** — at least one happy-path test against a real daemon (or override the `skip_if_no_daemon` fixture if the module doesn't need one).
4. **`docker_mcp/tools/prompts.py`** — at least one `@prompt(...)` template that exercises the new tools end-to-end.
5. **`docker_mcp/tools/resources.py`** — add an entry under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` if the new domain has authoritative docs the agent should be able to read at runtime.
6. **README.md** — append to the "What the agent can do" list and (if relevant) the "Security considerations" section.
7. **SECURITY.md** — only if the new module exposes a new class of risk not already covered by the README's Security section.

If you're only adding a tool or two to an *existing* domain file rather than a whole new module, items 1 and 7 don't apply, but 2-6 still do wherever relevant (e.g. a new tool still needs unit + integration test cases, even if the test file itself already exists).

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

## Submitting your change

CI (`.github/workflows/premerge.yaml`) runs on every pull request and push to `main`, and must pass before merging:

- `pytest` — two separate jobs: unit tests (`uv run pytest -v`, same as local) and integration tests (`uv run pytest -m integration -v`) against the real Docker Engine the `ubuntu-latest` runner ships with. You don't need a local daemon to contribute — `uv run pytest -v` alone (which excludes integration tests by default) is enough to check your change; the integration job will exercise it in CI.
- `ruff check` and `ruff format --check` — lint and formatting; run `uv run ruff check . && uv run ruff format .` locally to fix both before committing (the pre-commit hook from `uv run pre-commit install` runs `ruff check --fix` and `ruff format` automatically on each commit, so this is usually already handled for you).
- `pyright` — type-check.
- CI installs with `uv sync --locked`, which **fails** if `uv.lock` disagrees with `pyproject.toml` rather than silently re-locking. If you change a dependency, run `uv lock` and commit the updated lockfile alongside `pyproject.toml`.

A separate, non-blocking `Check docs mirror` job flags a PR that edits `CLAUDE.md` or `.github/copilot-instructions.md` without the other — the two files intentionally mirror each other (Copilot's own PR review is driven by `copilot-instructions.md`), so a change to project structure, conventions, or the tool surface should update both.

Keep PRs focused — one logical change per PR is easier to review than a bundle of unrelated fixes. For anything beyond a small, self-contained fix, consider opening an issue first (see "Reporting issues" below) so the approach can be discussed before you invest time in an implementation.

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

Bug reports and feature requests have templates that you can choose when you [`create an issue`](https://github.com/L337-org/docker-mcp/issues/new/choose). Please select the correct issue type and follow the template.
