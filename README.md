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

- **Containers** — `run_container`, `list_containers`, `exec_in_container`, `container_logs`, `stop_container`, `commit_container`
- **Images** — `build_image`, `pull_image`, `push_image`, `tag_image`, `prune_images`
- **Networks / Volumes** — `create_network`, `connect_network`, `create_volume`, `prune_volumes`
- **Swarm** — `init_swarm`, `create_service`, `scale_service`, `list_nodes`, `create_secret`, `create_config`
- **System** — `ping`, `info`, `version`, `df`, `events`
- **Compose** — `compose_up`, `compose_down`, `compose_ps`, `compose_logs`, `compose_config`, `compose_build`, `compose_pull`, `compose_run`, `compose_exec`, `compose_ls` *(wraps the `docker compose` CLI plugin)*
- **Contexts** — `context_ls`, `context_inspect`, `context_create`, `context_use`, `context_rm` *(wraps the `docker context` CLI)*
- **Registry / Hub** — `registry_list_tags`, `registry_inspect_manifest`, `hub_list_tags`, `hub_repo_info` *(HTTPS to OCI v2 registries and the Docker Hub API — no daemon required)*

The SDK-backed surface mirrors the [Docker SDK reference](https://docker-py.readthedocs.io/en/stable/) — if it's documented there, it's available here. The Compose and Context surfaces follow the [Compose CLI](https://docs.docker.com/reference/cli/docker/compose/) and [docker context](https://docs.docker.com/reference/cli/docker/context/) references.

The server also publishes the Docker SDK for Python reference and selected Docker CLI / registry references as MCP resources so the agent can consult them at runtime: read `docker-docs://contents` for the section index, then `docker-docs://<section>` (e.g. `docker-docs://containers`, `docker-docs://compose`, `docker-docs://oci-distribution-spec`) for the rendered page.

### Example prompts

Many AI clients let you invoke registered MCP prompts directly (in Claude Code, type `/` to see them). The server ships a small library of templates in `tools/prompts.py` that scaffold multi-step workflows — they emit a structured plan that the agent then carries out using the docker tools.

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

## Security considerations

Connecting this server to an AI agent grants it the same level of access as a local Docker CLI session against the configured daemon. That is broad: the daemon's socket is effectively root-equivalent on the host running it. Treat the agent as a privileged user and weigh the risks below before enabling the server.

- **Use a scoped daemon.** Prefer pointing `DOCKER_HOST` at a daemon dedicated to workloads the agent is allowed to touch (a development VM, a remote sandbox, Docker Desktop, a rootless install) rather than your production socket. The daemon is the trust boundary — there is no per-tool authorization layer.
- **Privileged containers and host mounts.** `run_container` accepts `privileged=True` and arbitrary `volumes`. A privileged container, or one that bind-mounts `/` from the host, can trivially escape to the host filesystem. Avoid letting the agent set these unless you have reviewed the request. Compose files can declare the same — review the rendered `compose_config` output before approving `compose_up` on an unfamiliar project.
- **Registry credentials.** `login`, `push_image`, and `get_registry_data` accept credentials directly as tool arguments, as do `registry_list_tags` and `registry_inspect_manifest` for private registries. Many MCP clients log tool calls verbatim, so treat any password or `auth_config` you pass through these tools as exposed. Prefer running `docker login` once on the host running this MCP server so the `docker` module can reuse credentials cached in that host's Docker config (typically `~/.docker/config.json`) — leave the credential parameters unset. (Note: this is the host running the server, not the daemon — relevant when `DOCKER_HOST` points at a remote daemon.)
- **`exec_in_container`, `compose_exec`, and `compose_run` run arbitrary commands.** When any part of the command is derived from agent-controlled input, use an exec-form argv list that does not invoke a shell (e.g. `["python", "-V"]`). A list like `["sh", "-c", template]` that invokes a shell will interpret shell metacharacters in the untrusted substrings.
- **Container archive paths.** `get_container_archive` and `put_container_archive` forward the supplied path verbatim to the daemon. The container is the trust boundary — if you do not trust its filesystem, do not assume `..` traversal will be rejected.
- **Destructive operations have no built-in confirmation.** `prune_*`, `remove_*`, `kill_container`, `leave_swarm`, and `compose_down(volumes=True)` execute immediately. The shipped `clean_environment` prompt asks the agent to confirm before pruning volumes, but tool calls themselves are not gated. If you need an approval step, configure it at the MCP client (e.g. Claude Code's permission prompts) rather than relying on the server.
- **CLI shell-out attack surface.** Compose and Context tools spawn `docker` subprocesses on the host running this MCP server. Every invocation passes arguments as a list (no shell, no metacharacter interpretation), resolves the binary via `shutil.which`, and runs against a scrubbed environment (DOCKER_HOST and related vars only). Filesystem paths supplied to `compose_*` (project_dir, files) are read by the docker CLI on the server host — passing an unfamiliar path can expose any compose file the server's user can read.
- **Docker Context retargeting.** `context_use` only changes the CLI default for subsequent CLI-backed tools. SDK-backed tools (`list_containers`, `pull_image`, etc.) keep using whatever daemon the docker-py client connected to at server startup. Restart the server with a different `DOCKER_HOST` / `DOCKER_CONTEXT` to retarget those. `context_create(skip_tls_verify=True)` disables TLS verification for a context; use only against trusted local daemons.

## Contributing

Contributions are welcome. The project values a tight mapping between the Docker SDK's public surface and the MCP tools we expose.

### Project layout

```
.
├── main.py            # entry point — runs the FastMCP server over stdio
├── server.py          # creates the FastMCP singleton (`mcp`) shared by every tool module
├── tools/             # one file per Docker SDK domain or CLI/registry feature
│   ├── _cli.py        # cross-platform subprocess helper for docker CLI shell-outs (private)
│   ├── _utils.py      # shared helpers (drop_none, join_bounded, MAX_PAYLOAD_BYTES) (private)
│   ├── client.py      # DockerClient connection + lazy `_get_client()` helper
│   ├── containers.py
│   ├── images.py
│   ├── networks.py
│   ├── volumes.py
│   ├── configs.py
│   ├── secrets.py
│   ├── nodes.py
│   ├── services.py
│   ├── swarm.py
│   ├── plugins.py
│   ├── compose.py     # `docker compose` CLI plugin (shells out via _cli.py)
│   ├── context.py     # `docker context` CLI (shells out via _cli.py)
│   ├── registry.py    # OCI v2 registries + Docker Hub HTTPS APIs (no daemon)
│   ├── prompts.py     # @mcp.prompt() templates for common docker workflows
│   └── resources.py   # @mcp.resource() endpoints exposing SDK + CLI + registry docs
└── tests/             # pytest suite, mirrors `tools/` one-to-one
    └── integration/   # tests that hit a real Docker daemon or docker.io
```

Each `tools/<file>.py` has a matching `tests/test_<file>.py`. New modules must be added to `tools/__init__.py` and have a corresponding test file. Tool modules that wrap CLI features must funnel every subprocess call through `tools/_cli.py` so the cross-platform safety concerns (binary discovery, no shell, UTF-8 decoding, output capping, Windows console suppression, env scrubbing) live in one place.

### Conventions

Tool functions are decorated with `@mcp.tool()` (note the parentheses — required by FastMCP) and follow this docstring style:

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

- Import `mcp` from `server.py`, never directly from the `mcp` package — that creates a circular import.
- Line length is 120 characters (enforced by ruff).
- CLI shell-outs must go through `tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. The helper enforces `shell=False`, resolves the binary via `shutil.which` (cross-platform), decodes output as UTF-8 with replace, caps the captured bytes, scrubs the environment, and suppresses console pop-ups on Windows.

### Checklist when adding a new tool module

When you add a new `tools/<domain>.py`, also update:

1. **`tools/__init__.py`** — star-import the module (private helpers prefixed with `_` are excluded).
2. **`tests/test_<domain>.py`** — unit tests using mocks (no real daemon).
3. **`tests/integration/test_<domain>.py`** — at least one happy-path test against a real daemon (or override the `skip_if_no_daemon` fixture if the module doesn't need one).
4. **`tools/prompts.py`** — at least one `@mcp.prompt(...)` template that exercises the new tools end-to-end.
5. **`tools/resources.py`** — add an entry under `SDK_SECTIONS` or `EXTERNAL_SECTIONS` if the new domain has authoritative docs the agent should be able to read at runtime.
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
uv run python main.py
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
