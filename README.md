# docker-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents manage Docker — containers, images, networks, volumes, swarm services, secrets, configs, nodes, plugins, **Compose projects, CLI contexts, and OCI registries** — by wrapping the official [Docker SDK for Python](https://docker-py.readthedocs.io/en/stable/) and selectively shelling out to the `docker` CLI for features the SDK doesn't expose.

Every documented domain of the Docker SDK is exposed: build and run containers, pull and push images, manage networks and volumes, drive a swarm, install plugins, and more — all with first-class argument validation through MCP. Compose v2 and Docker contexts are wrapped via the docker CLI; OCI v2 registries and Docker Hub are queried directly over HTTPS (no daemon required).

## Requirements

- A running Docker daemon reachable from the host that runs the server (the standard `DOCKER_HOST` / unix socket conventions apply)
- [Python ≥ 3.14](https://www.python.org/downloads/)
- [uv](https://docs.astral.sh/uv/) for dependency management

## Using the server

Add an entry to your AI tool's MCP configuration (commonly `mcp.json` or the equivalent in your client). The snippet below runs the server straight from this repository — `uv` will fetch and cache the package on first use:

```json
{
  "mcpServers": {
    "docker-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/GavinLucas/docker-mcp.git",
        "docker-mcp"
      ],
      "env": {}
    }
  }
}
```

To pin a specific revision, append `@<tag-or-commit>` to the git URL.

### Talking to a remote daemon

The server connects through `docker.from_env()`, so anything the standard Docker CLI honours works here too. Common overrides via `env`:

```json
"env": {
  "DOCKER_HOST": "tcp://remote-host:2375",
  "DOCKER_TLS_VERIFY": "1",
  "DOCKER_CERT_PATH": "/path/to/certs"
}
```

### What the agent can do

Once loaded, the agent gets MCP tools grouped by Docker domain. A few examples:

- **Containers** — `run_container`, `list_containers`, `exec_in_container`, `container_logs`, `stop_container`, `commit_container`, `export_container_to_file` / `get_container_archive_to_file` / `put_container_archive_from_file` (stream tar archives to/from a host path)
- **Images** — `build_image`, `pull_image`, `push_image`, `tag_image`, `prune_images`, `save_image_to_file` / `load_image_from_file` (stream image tarballs to/from a host path)
- **Networks / Volumes** — `create_network`, `connect_network`, `create_volume`, `prune_volumes`
- **Swarm** — `init_swarm`, `get_swarm_join_tokens` (close the init → join loop), `rotate_swarm_join_token`, `create_service`, `scale_service`, `rollback_service` (re-apply the previous service spec), `list_nodes`, `remove_node`, `create_secret`, `create_config`
- **System** — `ping`, `info`, `version`, `df`, `events`, `login` / `logout` (cache or clear registry credentials), `reconnect` (rebuild the SDK client / retarget the daemon)
- **Compose** — `compose_up`, `compose_down`, `compose_stop`, `compose_start`, `compose_ps`, `compose_logs`, `compose_config`, `compose_build`, `compose_pull`, `compose_restart`, `compose_run`, `compose_exec`, `compose_ls` *(wraps the `docker compose` CLI plugin)*
- **Contexts** — `context_ls`, `context_inspect`, `context_create`, `context_use`, `context_rm` *(wraps the `docker context` CLI)*
- **Registry / Hub** — `registry_list_tags`, `registry_inspect_manifest`, `registry_get_config` (read an image's env/entrypoint/labels without pulling), `hub_list_tags`, `hub_repo_info`, `hub_rate_limit` (remaining pull budget) *(HTTPS to OCI v2 registries and the Docker Hub API — no daemon required; transparent retry on a brief 429)*
- **Buildx** — `buildx_build`, `buildx_bake`, `buildx_imagetools_inspect`, `buildx_imagetools_create`, `buildx_ls`, `buildx_inspect`, `buildx_du`, `buildx_prune`, `buildx_create`, `buildx_use`, `buildx_rm` *(wraps the `docker buildx` CLI plugin). Use `buildx_imagetools_*` in place of `docker manifest` — that command is in maintenance mode and lacks support for OCI image indexes and attestations.*
- **Scout** — `scout_cves`, `scout_quickview`, `scout_recommendations`, `scout_compare`, `scout_sbom` *(wraps the `docker scout` CLI plugin; most features benefit from `docker login` on the host running this server).*

The SDK-backed surface mirrors the [Docker SDK reference](https://docker-py.readthedocs.io/en/stable/) — if it's documented there, it's available here. The Compose and Context surfaces follow the [Compose CLI](https://docs.docker.com/reference/cli/docker/compose/) and [docker context](https://docs.docker.com/reference/cli/docker/context/) references.

The server also publishes the Docker SDK for Python reference and selected Docker CLI / registry references as MCP resources so the agent can consult them at runtime: read `docker-docs://contents` for the section index, then `docker-docs://<section>` (e.g. `docker-docs://containers`, `docker-docs://compose`, `docker-docs://oci-distribution-spec`) for the rendered page. A further resource, `docker-mcp://tool-catalog`, lists every tool this server knows about with its domain, mutation category, and whether the active configuration registered it — useful for confirming the blast radius of a tool, or why one is absent from the live list.

### Example prompts

Many AI clients let you invoke registered MCP prompts directly (in Claude Code, type `/` to see them). The server ships a small library of templates in `docker_mcp/tools/prompts.py` that scaffold multi-step workflows — they emit a structured plan that the agent then carries out using the docker tools.

**Looking things up in the SDK docs**

```
/lookup_docker_docs section=services
/verify_docker_method method=containers.run section=containers
```

…or just ask in plain English:

> Read `docker-docs://networks` and tell me the difference between `create` and `connect`.
> Before changing any code, check `docker-docs://containers` and confirm `run` accepts a `restart_policy` argument.

**Creating and managing containers**

```
/deploy_container image=nginx:1.27 name=web
/troubleshoot_container container=api-1
/migrate_container container=api-1 new_image=myorg/api:v2
/inspect_stack label=com.example.app=web
/clean_environment scope=stopped
/plan_compose_stack description="wordpress + mysql sharing a named volume"
```

**Compose, contexts, and registries**

```
/deploy_compose_project project_dir=/srv/myapp
/troubleshoot_compose_project project_dir=/srv/myapp
/audit_docker_contexts
/find_latest_image_tag image=ghcr.io/org/repo
```

**Buildx, Scout, and multi-arch manifests**

```
/plan_multiarch_build image=ghcr.io/org/app:v1 platforms=linux/amd64,linux/arm64
/audit_image_cves image=alpine:3.19
/compare_image_versions old_image=org/app:v1 new_image=org/app:v2
/recommend_base_image image=org/app:v1
/inspect_multiarch_manifest image=alpine:3.19
/create_multiarch_manifest target_tag=org/app:v1 source_tags=org/app:v1-amd64,org/app:v1-arm64
/migrate_from_docker_manifest
```

…or in plain English:

> Pull `redis:7-alpine` and run it as a container called `cache` on a new `app-net` network, exposing port 6379 only inside that network.
> Container `api-1` keeps restarting — grab the last 200 log lines, inspect its state and exit code, and tell me what's wrong before changing anything.
> Replace the running `web` container with `nginx:1.27` while keeping its current ports, mounts, and restart policy.
> Plan a wordpress + mysql stack on a private network with a named volume for the database. Show me the plan before creating anything.
> Show every container, network, and volume tagged `com.example.app=web` as one table. Don't change anything.
> We're tight on disk — show `df`, prune stopped containers and dangling images, then show `df` again. Skip volumes.
> Bring up the compose project in `/srv/myapp`, but show me the rendered config and pull the images before starting anything.
> List my Docker contexts and tell me which daemon this MCP server is currently talking to.
> Find the most recent stable tag for `ghcr.io/org/repo` without pulling it, and tell me which platforms it supports.

## Configuration

Three environment variables restrict which tools are registered when the server starts. Because they drop tools at registration time, a disabled tool never appears in the client's tool list — this is a server-side guarantee, not a client-side prompt. Set the two boolean switches to `1` / `true` / `yes` / `on`:

- **`DOCKER_MCP_READONLY`** — register only read-only tools (queries, log/data reads, scans). Every tool that changes state is omitted. Use this for monitoring or inspection agents that must not be able to modify anything.
- **`DOCKER_MCP_NO_DESTRUCTIVE`** — register everything *except* destructive tools (`remove_*`, `prune_*`, `kill_container`, `compose_down`, `leave_swarm`, `context_rm`, `buildx_prune`, `buildx_rm`). A "no data loss" mode that still allows creating and starting resources. `DOCKER_MCP_READONLY` is stricter and wins if both are set.
- **`DOCKER_MCP_DISABLE`** — a comma-separated list of *domains* (feature areas) to drop wholesale, regardless of category: e.g. `DOCKER_MCP_DISABLE=swarm,services,nodes,configs,secrets` removes the entire swarm surface from a single-host server, and `DOCKER_MCP_DISABLE=scout,buildx` trims build/scan tooling an agent will never use. A domain is a tool module's name — `containers`, `images`, `networks`, `volumes`, `compose`, `context`, `buildx`, `scout`, `registry`, `swarm`, `services`, `nodes`, `plugins`, `configs`, `secrets`, `client`. Names are case-insensitive; an unrecognized name is ignored (and surfaced as `unknown_disabled_domains` in the tool catalog, see below). This stacks with the category switches — a tool registers only if its category survives *and* its domain is enabled. Trimming domains an agent doesn't need also cuts the tool-list size the client has to reason about, which matters at this server's ~190-tool scale.

Independently, every registered tool carries [MCP `ToolAnnotations`](https://modelcontextprotocol.io/) — `readOnlyHint` on queries and `destructiveHint` on destructive operations (plus `idempotentHint` on the prune family) — so a client like Claude Code can auto-allow safe reads and gate destructive calls. The classification lives in `TOOL_CATEGORIES` in `docker_mcp/server.py`. To see the full picture at runtime — every tool with its domain, category, and whether the active switches registered it — read the **`docker-mcp://tool-catalog`** MCP resource.

For private registries, the HTTPS-backed `registry_*` tools fall back to **`DOCKER_MCP_REGISTRY_USERNAME`** / **`DOCKER_MCP_REGISTRY_PASSWORD`** from the server's environment when no explicit `username`/`password` arguments are passed (explicit arguments win; the env pair is only used when both arguments are unset). Setting credentials in the environment keeps them out of tool arguments, which many MCP clients log verbatim — the password may be a personal-access token.

### Example: a read-only monitoring server

All of these go in the `env` block of the server entry in your MCP client config (the same place as `DOCKER_HOST` above). For example, a read-only inspection server against a remote daemon:

```json
{
  "mcpServers": {
    "docker-mcp-readonly": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/GavinLucas/docker-mcp.git",
        "docker-mcp"
      ],
      "env": {
        "DOCKER_HOST": "tcp://staging-host:2376",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_MCP_READONLY": "1"
      }
    }
  }
}
```

Swap `DOCKER_MCP_READONLY` for `DOCKER_MCP_NO_DESTRUCTIVE` to allow create/start/deploy while still making `remove_*` / `prune_*` / `kill_container` impossible. You can also register the same server twice under different names — a full-access entry you enable when needed and a read-only entry for everyday use. With `claude mcp` (Claude Code), the equivalent is:

```bash
claude mcp add docker-mcp-readonly \
  --env DOCKER_MCP_READONLY=1 \
  -- uvx --from git+https://github.com/GavinLucas/docker-mcp.git docker-mcp
```

## Security considerations

Connecting this server to an AI agent grants it the same level of access as a local Docker CLI session against the configured daemon. That is broad: the daemon's socket is effectively root-equivalent on the host running it. Treat the agent as a privileged user and weigh the risks below before enabling the server.

- **Use a scoped daemon.** Prefer pointing `DOCKER_HOST` at a daemon dedicated to workloads the agent is allowed to touch (a development VM, a remote sandbox, Docker Desktop, a rootless install) rather than your production socket. The daemon is the trust boundary — there is no per-tool authorization layer.
- **Privileged containers and host mounts.** `run_container` accepts `privileged=True` and arbitrary `volumes`. A privileged container, or one that bind-mounts `/` from the host, can trivially escape to the host filesystem. Avoid letting the agent set these unless you have reviewed the request. Compose files can declare the same — review the rendered `compose_config` output before approving `compose_up` on an unfamiliar project.
- **Pass-through `extra_kwargs` / `updates` bypass the visible schema.** `run_container`, `create_container`, `create_service` (`extra_kwargs`) and `update_container`, `update_service` (`updates`) forward an arbitrary dict straight into the Docker SDK. A client that gates on, say, `privileged=False` in the tool's declared parameters can still be bypassed via `extra_kwargs={"privileged": True, "pid_mode": "host"}`. These escape hatches are consistent with the "daemon is the trust boundary" model, but any allow/deny policy you build at the MCP-client layer must account for them rather than trusting the named parameters alone.
- **Registry credentials.** Many MCP clients log tool calls verbatim, so treat any password or `auth_config` you pass through a tool as exposed.
  - **SDK-backed tools** (`login`, `push_image`, `get_registry_data`) accept credentials directly *and* can reuse credentials cached by `docker login` in `~/.docker/config.json`. Prefer running `docker login` once on the host running this MCP server and leaving the credential parameters unset. (Note: this is the host running the server, not the daemon — relevant when `DOCKER_HOST` points at a remote daemon.) A credential passed to `login` is cached in the server's memory for the life of the client; `logout` clears that in-memory cache (all registries, or one) without touching `~/.docker/config.json`, and `close` / `reconnect` clear it by discarding the client. There is no daemon-side session to end — the Engine's `/auth` endpoint only validates.
  - **HTTPS-backed registry tools** (`registry_list_tags`, `registry_inspect_manifest`, `registry_get_config`, `hub_list_tags`, `hub_repo_info`, `hub_rate_limit`) talk to the registry directly over HTTPS and do NOT read `~/.docker/config.json`. The `registry_*` tools accept `username` / `password` for private registries — or, better, read `DOCKER_MCP_REGISTRY_USERNAME` / `DOCKER_MCP_REGISTRY_PASSWORD` from the server's environment so credentials never transit tool arguments (see [Configuration](#configuration)); the `hub_*` tools currently support public Hub repositories only. If passing credentials as arguments, use a per-invocation token with the minimum required scope rather than a long-lived password. When a registry answers with a `Bearer` auth challenge, the server validates the token `realm` it points at before sending anything: the scheme must be http/https, plaintext http to a non-local host is rejected, and a public registry is not allowed to redirect the credentialed token request at a private/loopback address (an SSRF guard). A genuinely local dev registry (e.g. `localhost:5000`) may still use a local realm.
- **Swarm secret material transits tool calls too.** Beyond registry credentials, several swarm tools carry secret material through arguments or return values that MCP clients may log: `create_secret(data=...)` and `create_config(data=...)` take the payload as an argument, `get_secret` / `get_config` return the stored object, `join_swarm(join_token=...)` and `unlock_swarm(key=...)` take cluster join/unlock secrets, and `get_swarm_unlock_key`, `get_swarm_join_tokens`, and `rotate_swarm_join_token` *return* cluster credentials — a manager join token lets its holder join the swarm as a manager (root-equivalent on the cluster). Treat all of these as exposed in any client that records tool traffic, and prefer provisioning swarm secrets and reading join tokens out-of-band on the host rather than through the agent. If an agent never needs to admit nodes, drop the whole surface with `DOCKER_MCP_DISABLE=swarm` (see [Configuration](#configuration)).
- **`exec_in_container`, `compose_exec`, and `compose_run` run arbitrary commands.** When any part of the command is derived from agent-controlled input, use an exec-form argv list that does not invoke a shell (e.g. `["python", "-V"]`). A list like `["sh", "-c", template]` that invokes a shell will interpret shell metacharacters in the untrusted substrings.
- **Container archive paths.** `get_container_archive` and `put_container_archive` forward the supplied path verbatim to the daemon. The container is the trust boundary — if you do not trust its filesystem, do not assume `..` traversal will be rejected.
- **File-path payload tools read and write the server host's filesystem.** `save_image_to_file`, `export_container_to_file`, and `get_container_archive_to_file` write to a `dest_path` on the host running this MCP server (refusing to overwrite an existing file unless `overwrite=True`); `load_image_from_file` and `put_container_archive_from_file` read a host path. These run as the server's user, so the agent can write any file that user can write and read any file it can read. Prefer the in-band byte tools (capped at 32 MiB) when you don't trust the agent with host filesystem access. `DOCKER_MCP_READONLY` also drops the host-writing variants — but note it is not targeted at them: it registers *only* read-only tools, so `load_image_from_file` and `put_container_archive_from_file` (and every other mutating/destructive tool) go too. There is no switch that drops just the file-writers.
- **Destructive operations have no built-in confirmation.** `prune_*`, `remove_*`, `kill_container`, `leave_swarm`, `compose_down(volumes=True)`, `buildx_prune` (always runs with `--force`), and `buildx_rm` execute immediately. These tools carry the `destructiveHint` annotation, so a client like Claude Code can gate them, and the shipped `clean_environment` prompt asks the agent to confirm before pruning volumes — but tool calls themselves are not gated by the server. For a hard guarantee, run with `DOCKER_MCP_NO_DESTRUCTIVE=1` (drops them entirely) or `DOCKER_MCP_READONLY=1` (see [Configuration](#configuration)); for an approval step, configure it at the MCP client.
- **CLI shell-out attack surface.** Compose, Context, Buildx, and Scout tools spawn `docker` subprocesses on the host running this MCP server. Every invocation passes arguments as a list (no shell, no metacharacter interpretation), resolves the binary via `shutil.which`, and runs against a scrubbed environment (DOCKER_HOST and related vars only). Positional values (image refs, service / context / builder names, build contexts) are additionally rejected if they start with `-`, so an argument can't be smuggled in as a CLI flag (e.g. a service named `--output=…`); the one deliberate exception is the trailing command in `compose_exec` / `compose_run`, which is meant to be an arbitrary argv. Filesystem paths supplied to `compose_*` (project_dir, files) are read by the docker CLI on the server host — passing an unfamiliar path can expose any compose file the server's user can read.
- **Docker Context retargeting.** `context_use` only changes the CLI default for subsequent CLI-backed tools. SDK-backed tools (`list_containers`, `pull_image`, etc.) keep using whatever daemon the docker-py client connected to when it was first created (lazily, on the first SDK-backed tool call). Restart the server with a different `DOCKER_HOST` / `DOCKER_CONTEXT`, or call `reconnect` (see below), to retarget those. `context_create(skip_tls_verify=True)` disables TLS verification for a context; use only against trusted local daemons.
- **`reconnect` retargets the trust boundary at runtime.** `reconnect(docker_host=...)` rebuilds the shared SDK client and points it at a different daemon without a server restart — which moves the root-equivalent trust boundary to whatever endpoint is passed. Only allow it against daemons the agent is authorized to control. The `docker_host` argument is logged like any tool call, and an `ssh://` target authenticates via the server host's SSH agent / `known_hosts`. The new endpoint is validated before the previous client is replaced, so a bad target leaves the working client in place.

## Contributing

Contributions are welcome. The project values a tight mapping between the Docker SDK's public surface and the MCP tools we expose.

### Project layout

```
.
├── docker_mcp/            # the package — `python -m docker_mcp` runs the server
│   ├── __init__.py        # defines `main()`; side-effect-imports `server` and `tools`
│   ├── __main__.py        # calls `main()` so `python -m docker_mcp` works
│   ├── server.py          # creates the FastMCP singleton (`mcp`) shared by every tool module
│   └── tools/             # one file per Docker SDK domain or CLI/registry feature
│       ├── _cli.py        # cross-platform subprocess helper for docker CLI shell-outs (private)
│       ├── _utils.py      # shared helpers (drop_none, join_bounded, MAX_PAYLOAD_BYTES) (private)
│       ├── client.py      # DockerClient connection + lazy `_get_client()` helper
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
│       ├── context.py     # `docker context` CLI (shells out via _cli.py)
│       ├── buildx.py      # `docker buildx` CLI plugin (shells out via _cli.py)
│       ├── scout.py       # `docker scout` CLI plugin (shells out via _cli.py)
│       ├── registry.py    # OCI v2 registries + Docker Hub HTTPS APIs (no daemon)
│       ├── prompts.py     # @mcp.prompt() templates for common docker workflows
│       └── resources.py   # @mcp.resource() endpoints exposing SDK + CLI + registry docs
└── tests/                 # pytest suite, mirrors `docker_mcp/tools/` one-to-one
    └── integration/       # tests that hit a real Docker daemon or docker.io
```

Each `docker_mcp/tools/<file>.py` has a matching `tests/test_<file>.py`. New modules must be added to `docker_mcp/tools/__init__.py` and have a corresponding test file. Tool modules that wrap CLI features must funnel every subprocess call through `docker_mcp/tools/_cli.py` so the cross-platform safety concerns (binary discovery, no shell, UTF-8 decoding, output capping, Windows console suppression, env scrubbing) live in one place.

### Conventions

Tool functions are decorated with `@tool()` (the project's wrapper around `@mcp.tool()`, imported from `docker_mcp.server`) and follow this docstring style:

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

- Import `tool` from `docker_mcp.server` (and, for prompts/resources, `mcp`), never directly from the `mcp` package — that creates a circular import.
- Every tool needs a `TOOL_CATEGORIES` entry in `docker_mcp/server.py` (`READ_ONLY` / `MUTATING` / `DESTRUCTIVE`); the central map drives the tool's `ToolAnnotations` and the read-only env switches, and `tests/test_server.py` fails if it drifts from the registered set. A tool's *domain* (for `DOCKER_MCP_DISABLE` and the tool catalog) is derived automatically from its module name, so putting a tool in the right `docker_mcp/tools/<domain>.py` file is all that's needed.
- Line length is 120 characters (enforced by ruff).
- CLI shell-outs must go through `docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper enforces `shell=False`, resolves the binary via `shutil.which` (cross-platform), decodes output as UTF-8 with replace, caps the captured bytes, scrubs the environment, and suppresses console pop-ups on Windows.

### Checklist when adding a new tool module

When you add a new `docker_mcp/tools/<domain>.py`, also update:

1. **`docker_mcp/tools/__init__.py`** — star-import the module (private helpers prefixed with `_` are excluded).
2. **`tests/test_<domain>.py`** — unit tests using mocks (no real daemon).
3. **`tests/integration/test_<domain>.py`** — at least one happy-path test against a real daemon (or override the `skip_if_no_daemon` fixture if the module doesn't need one).
4. **`docker_mcp/tools/prompts.py`** — at least one `@mcp.prompt(...)` template that exercises the new tools end-to-end.
5. **`docker_mcp/tools/resources.py`** — add an entry under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` if the new domain has authoritative docs the agent should be able to read at runtime.
6. **README.md** — append to the "What the agent can do" list and (if relevant) the "Security considerations" section.
7. **SECURITY.md** — only if the new module exposes a new class of risk not already covered by the README's Security section.

### Verifying the SDK before writing code

To prevent hallucinated method names, the project includes a `/docker-sdk` Claude Code skill that fetches the live Docker SDK for Python documentation, inventories what's already exposed, and produces a gap analysis. Run it before adding new tools:

```
/docker-sdk                    # full gap analysis
/docker-sdk containers         # focus on a single domain
```

### Local development

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

CI runs both `pytest` and `ruff` on every push and pull request via `.github/workflows/premerge.yaml`.

### Reporting issues

Bug reports and feature requests have templates under [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/). Please use them when filing on GitHub.
