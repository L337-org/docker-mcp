# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **MIRROR RULE (do not skip): `CLAUDE.md`, `.github/copilot-instructions.md` and the detail layer
> (`architecture/`, `CONTRIBUTING.md`) are one documentation set.** The first two carry the same rules
> for an implementer and for a reviewer — `.github/copilot-instructions.md` drives GitHub Copilot's
> review of *every* PR — and `architecture/` plus `CONTRIBUTING.md` carry the rule-level detail they
> point at. **Any change to project structure, conventions, env vars, the tool/prompt/resource surface,
> or distribution channels MUST reach every layer that carries the rule, in the same change.** This is
> the most-forgotten step. Before mirroring, check the rule belongs in an always-loaded file at all: if
> a CI test already enforces it, neither mirror should carry it. The `Check docs mirror` job and the
> `PostToolUse` hook in `.claude/settings.json` both prompt for this. The job **fails** only when
> `CLAUDE.md` and `.github/copilot-instructions.md` disagree — one changed, the other not — and
> **warns** on a detail-layer-only change, which is the correct shape for detail and not a defect.
> Both are touch-tests, so neither can see whether the *same rule* reached each file.

## Read these before changing the matching area

Each file carries constraints whose violation breaks the product silently, or quietly re-opens a
decision already taken. These are not optional background.

| Before changing | Read |
|---|---|
| `docker_mcp/server.py`, or anything under `docker_mcp/tools/` | [architecture/server.md](architecture/server.md) |
| `docker_mcp/_hosts.py`, or per-call host selection anywhere | [architecture/hosts.md](architecture/hosts.md) |
| `docker_mcp/tools/_cli.py`, `_ssh_proxy.py`, or any CLI-backed tool module | [architecture/cli-shell-out.md](architecture/cli-shell-out.md) |
| any `@tool()` docstring | [architecture/tool-descriptions.md](architecture/tool-descriptions.md) |
| any code calling the `docker` package | [architecture/docker-sdk.md](architecture/docker-sdk.md) |
| `Dockerfile`, `manifest.json`, `server.json`, `scripts/`, or the release workflows | [architecture/distribution.md](architecture/distribution.md) |
| anything under `skills/l337-docker/` | [architecture/agent-skill.md](architecture/agent-skill.md) |
| `.github/workflows/` | [architecture/ci.md](architecture/ci.md) |
| adding a new tool module, or the testing conventions | the checklists in [CONTRIBUTING.md](CONTRIBUTING.md) |

## Project

`docker-mcp` is a Python MCP server (requires Python >=3.14) managed with `uv` that exposes the Docker
SDK for Python as MCP tools. The entry point is the `docker_mcp` package, run with `python -m
docker_mcp` or via the installed console script.

Five channels ship from this repo, all detailed in
[architecture/distribution.md](architecture/distribution.md):

- **PyPI** as `docker-mcp-server` (the `docker-mcp` name was taken). The import package stays
  `docker_mcp` and the repo stays `…/docker-mcp`. Two console scripts, `docker-mcp` and
  `docker-mcp-server`, both target `docker_mcp:main`.
- **GHCR** (`ghcr.io/l337-org/docker-mcp-server`), mirrored to Docker Hub
  (`gavinlucas/docker-mcp-server`) when the opt-in `DOCKERHUB_*` release secrets are configured.
- **A Claude Desktop Extension (`.mcpb`)** attached to each GitHub Release.
- **A Homebrew tap** in `L337-org/homebrew-tap`, currently **paused** — do not re-enable it without
  reading the four-step re-enable procedure in the architecture note.
- **A Claude Code agent skill** in `skills/l337-docker/`, attached to each Release as a `.tar.gz`.
  Deliberately **not** a channel for the server: it is a CLI-only *alternative* to it. Its guards are
  conventions a model can skip, not refusals at a call boundary — never describe it as equivalent.

The `docker` dependency is pulled with its `[ssh]` extra (paramiko), so `DOCKER_HOST=ssh://…` works
through a pure-Python transport — no system `ssh` binary, on the host and in the container images
alike. CLI-backed tools shell out to `docker`, which would otherwise use the *system* `ssh`; they are
routed through a per-call paramiko proxy instead, and fall back to running on the remote host when
there is no local `docker` at all. That machinery, and the IPv6-to-IPv4 fallback both transports
share, is in [architecture/cli-shell-out.md](architecture/cli-shell-out.md) and
[architecture/hosts.md](architecture/hosts.md).

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

# Regenerate the measured figures MCP_VS_SKILLS.md quotes (reports only; add --json for the
# structured form). Self-contained: pins its own tiktoken, installs the working tree editable.
uv run scripts/measure-comparison-figures.py
```

### CI

`.github/workflows/premerge.yaml` enforces `pytest`, `ruff check`, `ruff format --check` and `pyright`
on every PR and push to main, all via `uv run`, so the dev-group pins in `pyproject.toml` are the
single tool-version source. CI installs with `uv sync --locked`, which fails rather than silently
re-locking when `uv.lock` disagrees with `pyproject.toml`.

Four jobs are easy to get wrong; **read [architecture/ci.md](architecture/ci.md) before editing any of
them**, and before adding a direct dependency whose import surface this project touches:

- **`Check fresh resolve still imports`** exists because every other job installs `--locked` and so
  never resolves what a fresh `uvx`/`pip install` actually gets. A published release once shipped dead
  on arrival at import with all of CI green.
- **`Action pins are immutable`** requires every `uses:` to name a full 40-hex commit SHA. It is a port
  of the job of the same name in `L337-org/apt` (the reference implementation), with a third copy in
  `send-to-influx` — **keep all three in step**; the uppercase-SHA fix had to be chased across all of
  them because it was written once and copied twice.
- **`Check docs mirror`** is deliberately **not** required, so its red X prompts rather than blocks.
  See the MIRROR RULE above.
- **The weekly canary** hunts platform drift a PR cannot see. `main()` handles `--version` before any
  daemon contact because the canary's entry-point smoke depends on it.

## Architecture

The package is `docker_mcp`: `__init__.py` defines `main()` and side-effect-imports `server` and
`tools`, which is what registers every `@tool()`. `server.py` owns the `MCPServer` singleton, the
central `TOOL_CATEGORIES` map, the domain derivation behind `DOCKER_MCP_SERVER_DISABLE`, the schema
slimming, and the `instructions` router. `docker_mcp/tools/` holds one module per Docker feature area,
each backed by docker-py, a `docker` CLI shell-out, or direct registry HTTPS.

That is the map, not the detail. **Before changing any of it, read the matching file from the table at
the top** — [server.md](architecture/server.md) for registration, the tools package, resources and
prompts; [hosts.md](architecture/hosts.md) for the multi-daemon registry;
[cli-shell-out.md](architecture/cli-shell-out.md) for the CLI and SSH machinery.

Two things about that surface are load-bearing enough to state here:

- **A new tool module is a new domain**, so it needs a `TOOL_CATEGORIES` entry per tool *and* a
  `_DOMAIN_BLURBS` entry, or the `instructions` router omits the domain and a lazy-loading client
  never learns the vocabulary that finds its tools. Both are caught by `tests/test_server.py` — the
  router pair asserts the advertised domains equal the registered ones — so this is a "do it up
  front" note, not a silent trap. Full checklist in [CONTRIBUTING.md](CONTRIBUTING.md).
- **Tool modules import `tool` from `docker_mcp.server`; prompt modules import `prompt`.** Only
  resource modules import `mcp` directly — doing so in a tool or prompt module is a circular import.

## CLI shell-out policy

Any tool wrapping a `docker` CLI feature (Compose, Stack, Buildx, Scout, Context) **MUST** go through
`docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. That
helper is the single place holding binary resolution, `shell=False`, UTF-8 decoding, the output byte
cap, Windows console suppression and the environment allow-list. Always pass an explicit `timeout=`.
Never pass `shell=True`, never build paths by string concatenation, never expand `~` or globs yourself.

**Read [architecture/cli-shell-out.md](architecture/cli-shell-out.md) before touching any of it**, and
before adding a CLI-backed tool. It covers the SSH remote-exec fallback (which changes *which host* a
command runs on), the file-staging session, path-token reconciliation, and the deliberate two-style
error convention — action tools return the raw result dict and never raise on a non-zero exit, while
parsed-query tools raise `RuntimeError`. **That split is intentional; do not "unify" it.**

## Docker SDK Policy

**Before writing or modifying any code that calls the Docker SDK, run `/docker-sdk` (or `/docker-sdk
<topic>`)** to verify exact method signatures against the live documentation. Never use a `docker`
module method you have not confirmed in the docs. Do not assume a method exists because it sounds
plausible; if you cannot confirm it, say so and do not use it.

Prefer the high-level object API; drop to `_get_client().api` only for documented gaps, verified the
same way. **"The method exists" and "the method works" are separate claims** — `plugin_push` is the
standing example of a documented, importable SDK method that 404s against every daemon.

**Reaching past the public SDK is not an agent's call to make.** Where the public path fails, stop,
write up what you found, and escalate for a human decision rather than implementing a workaround.
Document-and-escalate is the correct outcome, not a failure to finish. The fallback ladder, the
already-blessed reach-ins, and the **SDK audit exclusions that must not be re-proposed** are in
[architecture/docker-sdk.md](architecture/docker-sdk.md) — read it before responding to an SDK
coverage audit.

## Tool function format

Every `@tool()` function carries a docstring in one exact format: a one-line summary, a blank line, a
usage-guidance paragraph, then `args:` (one `name - description` line per parameter, never repeating
the type — the annotation already reaches the client in `inputSchema`) and a `returns:` line naming
the shape.

The docstring **is** the tool `description` the client pays context for on every session, and it is
scored externally on a six-dimension rubric. **Read
[architecture/tool-descriptions.md](architecture/tool-descriptions.md) before writing or changing
one** — it carries the format, the quality standard the ratchet applies to every touched docstring,
and the division of labour across the three discovery layers. Write it right the first time; four
cleanup rounds have chased the same failure.

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
- **A URL taken from a response body is pinned to the origin it came from; a URL the caller named is not.** The distinction is who chose the destination. A registry host in a tool argument is the caller's choice and the whole point of the `registry_*`/`hub_*` tools, so it is not restricted. A `next`/`Link` URL is the *response's* choice, so `_validate_hub_next` requires Hub pagination to stay on `_HUB_API_BASE`'s scheme/host/port, and the OCI tag-list path keeps only the path from a `Link` header and re-applies the registry already being queried. A new paginating or link-following tool does the same. **Cross-host redirects are deliberately followed** and must not be "hardened" away: registries answer blob fetches with a redirect to a CDN on another host as normal operation (Docker Hub answers a config-blob GET with `307` to `production.cloudfront.docker.com`), so refusing them breaks `registry_image_config`; httpx strips `Authorization` on any cross-origin redirect, and a redirect reaches nothing a tool argument could not reach directly. Note the read-only/no-destructive switches are **not** egress controls (the registry tools are read-only); `DOCKER_MCP_SERVER_DISABLE=registry` is.
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
- **Prose that ships is British English in plain ASCII punctuation.** A tool docstring ships: the server
  advertises it verbatim as the tool's `description`, which is what a model reads when choosing between
  164 tools. So do the README, the agent skill, prompts and resources, comments, commit messages and PR
  descriptions — the test is whether it ships, not who reads it. **Never `—` or `–`**: use `-`, or `:`
  where what follows explains what came before. Likewise `...` not `…`, `x` not `×`, straight quotes, and
  `10-15%` for ranges.
- **Tool descriptions are strict ASCII, with no symbol exception**, and CI enforces exactly that:
  `tests/test_docs.py::test_advertised_tool_descriptions_are_plain_ascii` asserts it against what
  `list_tools()` advertises, so a violating tool fails. There is no allow-list because none of the 164 has
  needed one - the three arrows and one ellipsis found during the sweep all read better as words. Elsewhere
  in shipped prose a symbol carrying meaning (`≥`, or `→` inside a table) is still fine; that is the one
  place the general rule and this check deliberately differ.
- **Scope stops at tool descriptions.** Comments, tests and workflow files still hold em dashes; none of
  them ship, and sweeping them is a separate decision.
- **`CLAUDE.md` and `.github/copilot-instructions.md` are both exempt** — working instructions that never
  ship — which is why they still use em dashes freely.

## Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) via `docker_mcp/tools/_labels.py`, so the agent/operator can later enumerate that footprint — the `managed_only=True` arg on `container_list` / `network_list` / `volume_list` / `service_list`, or `--filter label=docker-mcp-server.managed=true`. The `prune_managed` prompt tears down only the managed footprint. Stamping is **on by default** and additive (a caller-supplied label always wins on a key collision); `DOCKER_MCP_SERVER_NO_LABELS=1` turns it off. The prefix is the bare project name (deliberately not reverse-DNS) and is a single constant in `_labels.py`.

When adding a new create tool that accepts a `labels` dict, route it through `_labels.py:with_provenance(labels, "<tool_name>")` (it accepts the dict/list/None shapes the SDK accepts and returns `None` — feed it through `drop_none` — when stamping is off and the caller passed nothing). The seven stamped creators today are `container_run`, `container_create`, `network_create`, `volume_create`, `service_create` (service-level `labels` only, not `container_labels`), `config_create`, `secret_create`. **Image builds are intentionally NOT stamped** — a build label changes the resulting image digest. Compose/stack containers (created via CLI shell-out) are also unstamped, as is `plugin_create` — the Engine's plugin-create call accepts no labels field, so there is nothing to stamp. The rule is conditional on the tool *accepting a `labels` dict*: a creator with nowhere to put a label is an expected exception, not an oversight, and should say so in its docstring. New `managed_only`-style label filters go through `_labels.py:managed_filter`.

## Reference

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html  
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html  
Docker SDK GitHub: https://github.com/docker/docker-py

Contributor-facing setup, the project-layout tree, the testing conventions and the new-tool-module
checklist live in [CONTRIBUTING.md](CONTRIBUTING.md); see also [SECURITY.md](SECURITY.md),
[PRIVACY.md](PRIVACY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and the server-versus-skill
comparison in [MCP_VS_SKILLS.md](MCP_VS_SKILLS.md).
