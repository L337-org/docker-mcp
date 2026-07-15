<img src="https://raw.githubusercontent.com/L337-org/docker-mcp/main/assets/icon.png" align="left" width="72" height="72" alt="">

# docker-mcp-server

[![docker-mcp MCP server](https://glama.ai/mcp/servers/L337-org/docker-mcp/badges/score.svg)](https://glama.ai/mcp/servers/L337-org/docker-mcp)

<!-- mcp-name: io.github.L337-org/docker-mcp-server -->

More than just a fully featured [MCP](https://modelcontextprotocol.io) server that lets AI agents manage Docker — containers, images, networks, volumes, swarm services, secrets, configs, nodes, plugins, etc., it helps you create workflows to easily manage your Docker environments.

For simple cases, you can just install and go with no configuration required - once loaded it will discover your local Docker socket and expose the full command surface to your AI agent. For more advanced users it can [manage multiple Docker daemons](#managing-several-daemons), e.g. both your local dev environment and also a remote production environment [over TCP, TLS or SSH](#talking-to-a-remote-daemon) in a single session. It can also be configured to mark some daemons as read-only, so you can monitor them without the risk of making accidental changes.

The MCP server also exposes things like logs and stats as resources so that you can monitor and triage, enabling you to [answer questions](#example-prompts) like 'why did my container crash?', 'what is the state of my swarm?', 'am I suffering memory pressure?', 'what is the disk usage of my volumes?', 'what differences are there between my test and production systems?', and more...

docker-mcp-server is optimized to work efficiently with the new generation of MCP clients that support lazy tool loading. For clients that still eagerly load all tools, the server can optionally be configured to exclude tools from a subset of domains (e.g. exclude 'swarm' and 'scout' tools) to reduce the tool list size. It's also possible to put the MCP server into 'read-only' or 'no-destructive' modes that prevent any tools with write or destructive capabilities from being registered, which again reduces the footprint.

The server runs entirely on your machine, either [natively](#using-the-server), as an [mcpb bundle](#install-as-a-desktop-extension-mcpb), or [containerized](#run-as-a-container), and sends no telemetry. You are entirely in control — see the [Privacy Policy](#privacy-policy).

## Requirements

Note: If you're using the containerized MCP server or MCPB bundle, the Python and uv requirements are taken care of for you.

- A running Docker daemon reachable from the host that runs the server (the standard `DOCKER_HOST` / unix socket conventions apply)
- [Python ≥ 3.14](https://www.python.org/downloads/)
- [uv](https://docs.astral.sh/uv/) for dependency management

## Using the server

The server is published to [PyPI](https://pypi.org/project/docker-mcp-server/) as **`docker-mcp-server`**. Add an entry to your AI tool's MCP configuration (commonly `mcp.json` or the equivalent in your client) pointing `uvx` at it — `uv` will fetch and cache the package on first use:

```json
{
  "mcpServers": {
    "docker-mcp-server": {
      "command": "uvx",
      "args": ["docker-mcp-server"],
      "env": {}
    }
  }
}
```

To pin a specific version, append `==<version>` to the package name (e.g. `docker-mcp-server==1.5.0`). If you'd rather install it onto your `PATH`, `pipx install docker-mcp-server` gives you the `docker-mcp-server` console script (a `docker-mcp` alias is also installed).

**Installing from git instead.** To run an unreleased revision straight from this repository:

```json
{
  "mcpServers": {
    "docker-mcp-server": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/L337-org/docker-mcp.git",
        "docker-mcp-server"
      ],
      "env": {}
    }
  }
}
```

To pin a specific revision, append `@<tag-or-commit>` to the git URL.

### Install as a Desktop Extension (.mcpb)

For [Claude Desktop](https://claude.com/download), a one-click bundle is attached to each
[GitHub Release](https://github.com/L337-org/docker-mcp/releases) as
`docker-mcp-server-<version>.mcpb` (with a matching `.sha256`). Download it and drag it into
**Settings → Extensions**, or use **Settings → Extensions → Advanced settings → Install extension…**
and pick the file. The install dialog surfaces a **Docker host(s)** field and the read-only /
no-destructive / disabled-domain switches, so no manual JSON editing is needed.

It's a [`uv`-type bundle](https://github.com/modelcontextprotocol/mcpb): Claude Desktop's managed
`uv` resolves the dependencies and runs the server, so the only host prerequisite is Docker itself —
no separate Python, `uv`, or `git`. Leave the **Docker host(s)** field blank to use your default
Docker context; set one endpoint (`ssh://user@host`) for a remote daemon, or list several (see
[Managing several daemons](#managing-several-daemons)).

### Run as a container

Running the server as a container removes the Python / uv / git prerequisites entirely — the only
thing the host needs is Docker, which you already have. Prebuilt multi-arch images (linux/amd64 +
linux/arm64) are published on each release to **Docker Hub** (`gavinlucas/docker-mcp-server`) and
**GHCR** (`ghcr.io/l337-org/docker-mcp-server`) — the two are identical. Point your MCP client at
`docker run`:

```json
{
  "mcpServers": {
    "docker-mcp-server": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "gavinlucas/docker-mcp-server:latest"
      ],
      "env": {}
    }
  }
}
```

`-i` is required (the server speaks MCP over stdio); `--rm` cleans up when the client disconnects. To
pin a version, replace `:latest` with a release tag (e.g. `:1.5.1`). To pull from GHCR instead, use
`ghcr.io/l337-org/docker-mcp-server:latest`.

> **Image renamed.** As of 1.5.0 the image is published as `docker-mcp-server` (matching the PyPI
> name). The old `ghcr.io/gavinlucas/docker-mcp` image is frozen at 1.4.0 and no longer updated —
> point new pulls at `ghcr.io/l337-org/docker-mcp-server`.

**Image variants.** Two variants are published to both registries (`gavinlucas/docker-mcp-server` on
Docker Hub and `ghcr.io/l337-org/docker-mcp-server` on GHCR), both built from one `Dockerfile`. The
CLI-backed domains (Compose, Stack, Buildx, Scout, Context) shell out to the `docker` CLI and its
plugins.

| Variant | Tags | Approx. size | Includes |
|---------|------|-------------|----------|
| `full` *(default)* | `:latest`, `:<version>` | ~510 MB | docker CLI + compose + buildx + **scout** |
| `no-scout` | `:no-scout`, `:<version>-no-scout` | ~315 MB | docker CLI + compose + buildx |

Scout's plugin binary alone accounts for the ~195 MB jump from `no-scout` to `full`. The `no-scout`
image also defaults `DOCKER_MCP_SERVER_DISABLE=scout`, so the scout *tools* don't register — the agent is
never offered tools whose CLI plugin isn't present (it sees a smaller, fully-working tool list rather
than scout tools that error on every call). Override at runtime with `-e DOCKER_MCP_SERVER_DISABLE=...` if you
ever need to change the disabled set (note it replaces, not appends).

**Building it yourself.** All variants build from the repo's `Dockerfile` via build args:

```bash
docker build -t docker-mcp-server:full .                                    # full (default)
docker build --build-arg INSTALL_SCOUT=0 --build-arg DISABLE_DOMAINS=scout \
  -t docker-mcp-server:no-scout .                                           # no-scout
docker build --build-arg INSTALL_CLI=0 -t docker-mcp-server:lite .          # lite (SDK-only, ~165 MB)
```

The `lite` image (docker-py SDK tools only — Compose/Buildx/Scout/Context degrade to "plugin
unavailable") is buildable but not published.

**Reaching the daemon from inside the container.** The image defaults `DOCKER_HOST` to
`unix:///var/run/docker.sock`, so mounting your host's socket onto that path is all that's needed.
Where the host socket *is*, however, varies — and the server prints a platform-aware hint to stderr
if it can't connect at startup:

- **Linux:** `-v /var/run/docker.sock:/var/run/docker.sock` (rootless: `-v $XDG_RUNTIME_DIR/docker.sock:/var/run/docker.sock`).
- **macOS (Docker Desktop):** the real socket is usually `~/.docker/run/docker.sock` — mount it onto the in-container path: `-v $HOME/.docker/run/docker.sock:/var/run/docker.sock` (or enable *Settings → Advanced → Allow the default Docker socket* and use `/var/run/docker.sock`).
- **Windows (Docker Desktop / WSL2):** the engine uses a named pipe, not a Unix socket — prefer `-e DOCKER_HOST=tcp://host.docker.internal:2375` (enable the TCP endpoint in Docker Desktop). That endpoint is **unauthenticated and unencrypted** — keep it bound to localhost, disable it when you're not using it, and use TLS or `DOCKER_HOST=ssh://...` for any remote daemon.
- **Remote / TLS / SSH daemon:** skip the socket mount and pass `-e DOCKER_HOST=...` (plus the TLS vars below) — see [Talking to a remote daemon](#talking-to-a-remote-daemon).

**Host filesystem access.** Inside a container, the file-path tools (`image_save` / `container_export`
with `dest_path`, `image_load` / `container_archive_put` with `from_file`, `container_archive_get_to_file`,
and compose `project_dir` / `files`) resolve paths *inside the container*,
not on your host. Bind-mount any directory you want to exchange files through — using the **same
path inside and out** keeps host and container paths identical:

```
-v $HOME/docker-work:$HOME/docker-work
```

If you call one of these tools with a path that isn't on a bind mount, the server refuses up front
with a message telling you exactly which `-v` to add — a write to an unmapped path would otherwise be
silently discarded when the container exits. (The in-band byte tools, capped at 32 MiB, need no
mount.) Configuration env vars (`DOCKER_MCP_SERVER_READONLY`, `DOCKER_HOST`, etc.) go in the client's `env`
block exactly as for the uvx install.

### Talking to a remote daemon

When `DOCKER_HOST` is set the server uses it directly (via `docker.from_env()`, so `DOCKER_TLS_VERIFY` / `DOCKER_CERT_PATH` are honoured too). Common overrides via `env`:

```json
"env": {
  "DOCKER_HOST": "tcp://remote-host:2375",
  "DOCKER_TLS_VERIFY": "1",
  "DOCKER_CERT_PATH": "/path/to/certs"
}
```

**Default daemon (no `DOCKER_HOST`).** With `DOCKER_HOST` unset, the server resolves the daemon the way the `docker` CLI does, rather than assuming `/var/run/docker.sock`: it follows the active Docker **context** (`DOCKER_CONTEXT`, else `currentContext` from `~/.docker/config.json`, reading the endpoint from that context's `meta.json`), and if that yields nothing it probes the well-known socket locations (`~/.docker/run/docker.sock` for Docker Desktop 4.13+, `$XDG_RUNTIME_DIR/docker.sock` for rootless, then `/var/run/docker.sock`). This matters because `docker.from_env()` alone ignores contexts and would fall back to `/var/run/docker.sock` — which **Docker Desktop 4.13+ no longer creates by default** (it uses the `desktop-linux` context unless you enable *Settings → Advanced → Allow the default Docker socket*), so a stock Desktop install reachable by your CLI would otherwise fail here. Precedence: a non-empty `DOCKER_HOST` always wins and goes straight through `docker.from_env()` (which ignores contexts); `DOCKER_CONTEXT` / `currentContext` is consulted only when `DOCKER_HOST` is unset, and the socket probe only when neither resolves. TLS material attached to a remote context is not applied automatically — a `tcp://` + TLS context still needs `DOCKER_HOST` / `DOCKER_CERT_PATH`.

**Over SSH.** `DOCKER_HOST=ssh://user@remote-host` is supported via a pure-Python transport (paramiko, pulled in by the `docker[ssh]` dependency) — there is **no system `ssh` binary requirement**, so it works the same on the host install and inside the container images. It authenticates with your normal SSH setup:

- **Keys / agent.** Use key-based auth; load the key into your agent (`ssh-add`) and make sure `SSH_AUTH_SOCK` is set in the server's environment (or place the key at the default `~/.ssh/id_*` path).
- **Known hosts.** paramiko verifies the host key against `~/.ssh/known_hosts` and **rejects an unknown host**. Add the host key only after verifying its fingerprint through a trusted channel — connect once interactively with `ssh user@remote-host` and confirm the prompt, or compare `ssh-keyscan remote-host | ssh-keygen -lf -` against a known-good fingerprint before appending it. Avoid blindly piping `ssh-keyscan` straight into `known_hosts`, which trusts whatever key is returned (including a MITM's).
- **In a container.** Mount your SSH material read-only — `-v $HOME/.ssh:/root/.ssh:ro` (key + `known_hosts`) — or forward your agent socket; no socket mount and no `ssh` package needed.

CLI-backed tools (Compose, Buildx, Context, Scout) shell out to the `docker` CLI, which would otherwise use the *system* `ssh` binary over an `ssh://` endpoint. Instead, `run_docker()` detects `DOCKER_HOST=ssh://...` and transparently starts a per-call local TCP proxy (`docker_mcp/tools/_ssh_proxy.py`) that opens the same paramiko connection docker-py would, runs `docker system dial-stdio` over it, and points the CLI subprocess at `tcp://127.0.0.1:<ephemeral port>` for the duration of that one call. So the CLI-backed tools authenticate with the exact same credentials and host-key policy as the docker-py-backed tools above — **no system `ssh` binary for the direct connection**, identical on the host install and inside the container images. The one exception is a `ProxyCommand` in `~/.ssh/config` (bastion/jump-host setups): paramiko runs that command as-given, and it's commonly `ssh -W %h:%p ...`, so a jump-host hop still shells out to the system `ssh` client even though the direct connection does not.

That ephemeral `127.0.0.1` listener bridges to the remote (root-equivalent) daemon with your SSH credentials for the duration of a single CLI call, so any process sharing the same loopback could reach it during that brief window. The exposure is narrow — localhost-only and torn down when the call returns — and inside a container it's narrower still, reachable only by processes within that container's network namespace. The daemon remains the trust boundary either way (see [Security considerations](#security-considerations)).

### Managing several daemons

Everything above targets one daemon. To manage **several in a single session** — e.g. local dev plus a remote production daemon — set **`DOCKER_MCP_SERVER_HOSTS`** to a comma-separated list of `name=endpoint` pairs:

```json
"env": {
  "DOCKER_MCP_SERVER_HOSTS": "local=auto, prod=ssh://ops@prod.example.com(ro)"
}
```

- **`endpoint`** is `auto` (your default context/socket, as [above](#talking-to-a-remote-daemon)), `local` (the platform-local socket, ignoring contexts), or a `unix://` / `tcp://` / `ssh://` / `npipe://` URL. `ssh://` is the recommended remote transport (per-host auth via your SSH keys, no TLS cert plumbing). A `tcp://` daemon over TLS takes a `(tls=<dir>)` marker pointing at a cert directory, e.g. `prod=tcp://prod:2376(tls=/etc/docker/prod)`. That directory must hold **`ca.pem`** (the daemon is always verified against it — so a self-signed daemon works, you just pin its cert here); add **`cert.pem`** and **`key.pem`** only if the daemon requires a client certificate (mutual TLS). There is no unverified-TLS mode — a TLS connection always authenticates the daemon, so encryption never comes without verification.
- **`(ro)`** after an endpoint marks that host **read-only**: mutating and destructive tools refuse to act on it. This is a per-host guard enforced at call time, independent of the server-wide `DOCKER_MCP_SERVER_READONLY` switch — mark production `(ro)` and the agent can inspect it all day but can't change it, while local stays read-write.
- **Single daemon, simpler form.** A bare value with no `name=` is shorthand for one host — `DOCKER_MCP_SERVER_HOSTS=ssh://ops@prod` (or `auto`, or blank). So this one field also covers the single-remote case. `DOCKER_HOST` keeps working when `DOCKER_MCP_SERVER_HOSTS` is unset, but **`DOCKER_MCP_SERVER_HOSTS` takes over when set** (`DOCKER_HOST` is then ignored, with a one-time notice to stderr).

**How the agent drives it.** With two or more hosts, every daemon-targeting tool gains an optional **`host`** argument constrained to your configured names: read-only tools default to the first host when you omit it, while mutating and destructive tools **require** an explicit `host` (so the agent can't change the wrong daemon by accident). `host_list` (and the `docker-mcp://hosts` resource) report the configured hosts and which is the default; the container/service/node observability resources become host-aware — the **default** host's index is `docker:///containers` (note the empty authority) and a **named** host's is `docker://{host}/containers` (likewise `docker-logs:///{id}` vs `docker-logs://{host}/{id}`, and the same pattern for `docker://services`/`service-logs://`/`service-tasks://` and `docker://nodes`); the single-host bare forms (`docker://containers`, `docker://services`, `docker://nodes`, …) are not registered once several hosts are configured. The `survey_hosts` prompt sweeps every host read-only. The `auto`/`local` endpoints are resolved to concrete URLs and **pinned at startup**, so the SDK and CLI always agree on which daemon a name means — restart to re-resolve after changing a Docker context.

### What the agent can do

Once loaded, the agent gets MCP tools grouped by Docker domain. A few examples:

- **Containers** — `container_run`, `container_list` (`managed_only=True` to list only what this server created — see [Provenance labels](#provenance-labels)), `container_exec`, `container_logs`, `container_stop`, `container_commit`, `container_wait` (block until exit, `until="healthy"` to poll a healthcheck, or `until="log-match"` to poll for a log line containing `pattern`), `container_export` / `container_archive_get_to_file` / `container_archive_put` (stream tar archives to/from a host path)
- **Images** — `image_build`, `image_pull`, `image_push`, `image_tag`, `image_prune`, `image_save` / `image_load` (stream image tarballs to/from a host path via `dest_path` / `from_file`)
- **Networks / Volumes** — `network_create`, `network_connect`, `volume_create`, `volume_prune`
- **Swarm** — `swarm_init`, `swarm_join_tokens` (close the init → join loop), `swarm_update` (rotate join tokens / unlock key), `service_create`, `service_scale`, `service_rollback` (re-apply the previous service spec), `service_wait` (block until tasks converge, or a rolling update completes), `node_list`, `node_wait` (block until a node reaches a target state — e.g. `ready` after joining), `node_remove`, `secret_create`, `config_create`
- **System** — `system_ping`, `system_info`, `system_version`, `system_df`, `system_events`, `host_list` (the configured daemons and which is the default — see [Managing several daemons](#managing-several-daemons)), `system_login` / `system_logout` (cache or clear registry credentials), `system_reconnect` (rebuild a host's SDK client to recover a wedged connection)
- **Compose** — `compose_up`, `compose_down`, `compose_stop`, `compose_start`, `compose_restart`, `compose_pause` / `compose_unpause`, `compose_kill`, `compose_ps`, `compose_list`, `compose_images`, `compose_top`, `compose_port`, `compose_logs`, `compose_config`, `compose_build`, `compose_pull`, `compose_run`, `compose_exec`, `compose_cp`, `compose_wait` *(wraps the `docker compose` CLI plugin)*
- **Stacks** — `stack_deploy`, `stack_list`, `stack_ps`, `stack_services`, `stack_remove` *(deploy a Compose file to a swarm as a stack; wraps the `docker stack` CLI — requires a swarm manager)*
- **Contexts** — `context_list`, `context_inspect`, `context_create`, `context_use`, `context_remove` *(wraps the `docker context` CLI)*
- **Registry / Hub** — `registry_tags`, `registry_tag_wait` (block until a specific tag lands — e.g. waiting on a CI push), `registry_manifest`, `registry_image_config` (read an image's env/entrypoint/labels without pulling), `hub_tags`, `hub_repo_info`, `hub_rate_limit` (remaining pull budget) *(HTTPS to OCI v2 registries and the Docker Hub API — no daemon required; transparent retry on a brief 429)*
- **Buildx** — `buildx_build`, `buildx_bake`, `buildx_imagetools_inspect`, `buildx_imagetools_create`, `buildx_list`, `buildx_inspect`, `buildx_du`, `buildx_history_list` / `buildx_history_inspect` (drill into past build records), `buildx_prune`, `buildx_create`, `buildx_use`, `buildx_remove` *(wraps the `docker buildx` CLI plugin). Use `buildx_imagetools_*` in place of `docker manifest` — that command is in maintenance mode and lacks support for OCI image indexes and attestations.*
- **Scout** — `scout_cves`, `scout_quickview`, `scout_recommendations`, `scout_compare`, `scout_sbom` *(wraps the `docker scout` CLI plugin; most features benefit from `docker login` on the host running this server).*

The SDK-backed surface mirrors the [Docker SDK reference](https://docker-py.readthedocs.io/en/stable/) — if it's documented there, it's available here. The Compose and Context surfaces follow the [Compose CLI](https://docs.docker.com/reference/cli/docker/compose/) and [docker context](https://docs.docker.com/reference/cli/docker/context/) references.

The server also publishes the Docker SDK for Python reference and selected Docker CLI / registry references as MCP resources so the agent can consult them at runtime: read `docker-docs://contents` for the section index, then `docker-docs://<section>` (e.g. `docker-docs://containers`, `docker-docs://compose`, `docker-docs://oci-distribution-spec`, `docker-docs://dockerfile`, `docker-docs://build-best-practices`, `docker-docs://engine-security`, `docker-docs://engine-api`) for the rendered page. For MCP clients that can't read resources (e.g. Claude Desktop, Cursor), the `docs_lookup` tool mirrors the same content — call it with no arguments for the section index, or `docs_lookup(section=...)` for a page; it's always available regardless of `DOCKER_MCP_SERVER_DISABLE`. A further resource, `docker-mcp://tool-catalog`, lists every tool this server knows about with its domain, mutation category, and whether the active configuration registered it — useful for confirming the blast radius of a tool, or why one is absent from the live list.

Container, service, and node observability are also exposed as resources, so a client can attach live state as context without a tool call: read **`docker://containers`** for an index of every container (running and stopped) with its status and per-container resource URIs, then **`docker-logs://<id-or-name>`** for a bounded tail of a container's logs (readable even after it exits — handy for diagnosing why) and **`docker-stats://<id-or-name>`** for a computed resource-usage summary (CPU %, memory, network and block I/O) of a running container. **`docker://services`** is the same pattern for swarm services — **`service-logs://<id-or-name>`** for a bounded log tail and **`service-tasks://<id-or-name>`** for a computed task/rollout summary (running vs. desired task counts, failing tasks, and the current rolling-update state). **`docker://nodes`** is index-only (state, availability, role, and manager reachability per node) — useful for noticing a node flapping between ready/down without re-querying `node_list`. These complement the equivalent tools (`container_logs`/`container_stats`, `service_logs`, `node_list`) and are hidden when their domain (`containers`/`services`/`nodes`) is disabled.

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
/monitor_container_fleet
/triage_incident window_minutes=30
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
/deploy_swarm_stack stack_name=web compose_file=/srv/myapp/docker-stack.yml
/audit_docker_contexts
/find_latest_image_tag image=ghcr.io/org/repo
```

**Auditing, security, and host operations**

```
/review_dockerfile dockerfile_path=/srv/myapp/Dockerfile
/audit_container_security
/debug_container_networking source=web target=db
/investigate_disk_usage
/backup_volume volume=pgdata dest_path=/backups/pgdata.tar
/restore_volume volume=pgdata source_path=/backups/pgdata.tar
/audit_swarm_health
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
> We're tight on disk — show `system_df`, prune stopped containers and dangling images, then show `system_df` again. Skip volumes.
> Bring up the compose project in `/srv/myapp`, but show me the rendered config and pull the images before starting anything.
> List my Docker contexts and tell me which daemon this MCP server is currently talking to.
> Find the most recent stable tag for `ghcr.io/org/repo` without pulling it, and tell me which platforms it supports.

## Configuration

> **Env var naming.** The server's environment variables are namespaced `DOCKER_MCP_SERVER_*` to match the published package name. The pre-2.0 `DOCKER_MCP_*` alias spellings are no longer honored — see `MIGRATION-2.0.md` for the full 1.x → 2.0 change list.

To choose **which daemon(s)** the server talks to, see [Talking to a remote daemon](#talking-to-a-remote-daemon) and [Managing several daemons](#managing-several-daemons) (`DOCKER_MCP_SERVER_HOSTS` / `DOCKER_HOST`). The variables below instead restrict **which tools** are registered.

Three environment variables restrict which tools are registered when the server starts. Because they drop tools at registration time, a disabled tool never appears in the client's tool list — this is a server-side guarantee, not a client-side prompt. Set the two boolean switches to `1` / `true` / `yes` / `on`:

- **`DOCKER_MCP_SERVER_READONLY`** — register only read-only tools (queries, log/data reads, scans). Every tool that changes state is omitted. Use this for monitoring or inspection agents that must not be able to modify anything.
- **`DOCKER_MCP_SERVER_NO_DESTRUCTIVE`** — register everything *except* destructive tools (`remove_*`, `prune_*`, `container_kill`, `compose_down`, `swarm_leave`, `context_remove`, `buildx_prune`, `buildx_remove`). A "no data loss" mode that still allows creating and starting resources. `DOCKER_MCP_SERVER_READONLY` is stricter and wins if both are set.
- **`DOCKER_MCP_SERVER_DISABLE`** — a comma-separated list of *domains* (feature areas) to drop wholesale, regardless of category: e.g. `DOCKER_MCP_SERVER_DISABLE=swarm,services,nodes,configs,secrets` removes the entire swarm surface from a single-host server, and `DOCKER_MCP_SERVER_DISABLE=scout,buildx` trims build/scan tooling an agent will never use. A domain is a tool module's name — `containers`, `images`, `networks`, `volumes`, `compose`, `stack`, `context`, `buildx`, `scout`, `registry`, `swarm`, `services`, `nodes`, `plugins`, `configs`, `secrets`, `system`. Names are case-insensitive; an unrecognized name is ignored (and surfaced as `unknown_disabled_domains` in the tool catalog, see below). This stacks with the category switches — a tool registers only if its category survives *and* its domain is enabled. Disabling a domain drops more than its tools: the matching workflow **prompts** are skipped (so the agent isn't handed a prompt that drives a feature area this server no longer exposes — e.g. disabling `scout` removes the `audit_image_cves` prompt that would otherwise tell the agent to call a tool that isn't registered) and the matching documentation **resources** are hidden from `docker-docs://contents` (e.g. the `scout` / `scout-cli` sections). The tool catalog's `prompts` list and `disabled_doc_sections` field make both auditable. Trimming domains an agent doesn't need also cuts the tool-list size the client has to reason about, which matters at this server's ~150-tool scale.

Independently, every registered tool carries [MCP `ToolAnnotations`](https://modelcontextprotocol.io/) — `readOnlyHint` on queries and `destructiveHint` on destructive operations (plus `idempotentHint` on the prune family) — so a client like Claude Code can auto-allow safe reads and gate destructive calls. The classification lives in `TOOL_CATEGORIES` in `docker_mcp/server.py`. To see the full picture at runtime — every tool with its domain, category, and whether the active switches registered it — read the **`docker-mcp://tool-catalog`** MCP resource.

For private registries, the HTTPS-backed `registry_*` tools fall back to **`DOCKER_MCP_SERVER_REGISTRY_USERNAME`** / **`DOCKER_MCP_SERVER_REGISTRY_PASSWORD`** from the server's environment when no explicit `username`/`password` arguments are passed (explicit arguments win; the env pair is only used when both arguments are unset). Setting credentials in the environment keeps them out of tool arguments, which many MCP clients log verbatim — the password may be a personal-access token.

### Provenance labels

Every Docker object the agent **creates** through this server — containers, networks, volumes, swarm services, configs, and secrets — is stamped with a small set of `docker-mcp-server.*` labels recording that this server made it (`docker-mcp-server.managed=true`), the server version, the originating tool, and a creation timestamp. This lets you (or a cleanup job) later enumerate exactly the footprint the agent created with a single `docker ... --filter label=docker-mcp-server.managed=true`; the `managed_only=True` argument on `container_list`, `network_list`, `volume_list`, and `service_list` is the in-tool shortcut (it combines with any other `filters` you pass). The stamping is additive (a label you pass yourself always wins on a key collision) and uniquely namespaced, so it's safe by default; **`DOCKER_MCP_SERVER_NO_LABELS=1`** turns it off entirely. Image builds are deliberately **not** stamped, because a build label changes the resulting image digest.

To tear down only what the server created — and nothing else — use the **`prune_managed`** workflow prompt, which scopes every removal step to the `docker-mcp-server.managed=true` label (volumes only when you pass `include_volumes=True`, and only after confirmation).

### Example: a read-only monitoring server

All of these go in the `env` block of the server entry in your MCP client config (the same place as `DOCKER_HOST` above). For example, a read-only inspection server against a remote daemon:

```json
{
  "mcpServers": {
    "docker-mcp-server-readonly": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/L337-org/docker-mcp.git",
        "docker-mcp-server"
      ],
      "env": {
        "DOCKER_HOST": "tcp://staging-host:2376",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_MCP_SERVER_READONLY": "1"
      }
    }
  }
}
```

Swap `DOCKER_MCP_SERVER_READONLY` for `DOCKER_MCP_SERVER_NO_DESTRUCTIVE` to allow create/start/deploy while still making `remove_*` / `prune_*` / `container_kill` impossible. You can also register the same server twice under different names — a full-access entry you enable when needed and a read-only entry for everyday use. With `claude mcp` (Claude Code), the equivalent is:

```bash
claude mcp add docker-mcp-server-readonly \
  --env DOCKER_MCP_SERVER_READONLY=1 \
  -- uvx --from git+https://github.com/L337-org/docker-mcp.git docker-mcp-server
```

## Security considerations

Connecting this server to an AI agent grants it the same level of access as a local Docker CLI session against the configured daemon. That is broad: the daemon's socket is effectively root-equivalent on the host running it. Treat the agent as a privileged user and weigh the risks below before enabling the server.

- **Use a scoped daemon.** Prefer pointing `DOCKER_HOST` at a daemon dedicated to workloads the agent is allowed to touch (a development VM, a remote sandbox, Docker Desktop, a rootless install) rather than your production socket. The daemon is the trust boundary — there is no per-tool authorization layer.
- **Running as a container.** Mounting `/var/run/docker.sock` into the container grants it the same root-equivalent access to that daemon as the uvx install has — no more, no less, but now explicit in the `docker run` line. The same scoped-daemon advice applies: prefer mounting a socket for, or pointing `DOCKER_HOST` at, a daemon the agent is allowed to control. Note that when containerized the file-path tools read and write *the container's* filesystem, so they can only reach host directories you bind-mount in (see [Run as a container](#run-as-a-container)). As an accident guard, the destructive container-lifecycle tools (`container_remove`, `container_kill`, `container_stop`, `container_restart`, `container_pause`) refuse to act on the server's *own* container so the agent can't end its own session mid-call; this is convenience, not a security boundary (it's bypassable with `DOCKER_MCP_SERVER_ALLOW_SELF_TERMINATE=1`, and a human can always recover the container from the host shell), and it does not constrain the many other ways a daemon-privileged agent can affect the host.
- **Privileged containers and host mounts.** `container_run` accepts `privileged=True` and arbitrary `volumes`. A privileged container, or one that bind-mounts `/` from the host, can trivially escape to the host filesystem. Avoid letting the agent set these unless you have reviewed the request. Compose files can declare the same — review the rendered `compose_config` output before approving `compose_up` on an unfamiliar project.
- **Pass-through `extra_kwargs` / `updates` bypass the visible schema.** `container_run`, `container_create`, `service_create` (`extra_kwargs`) and `container_update`, `service_update` (`updates`) forward an arbitrary dict straight into the Docker SDK. A client that gates on, say, `privileged=False` in the tool's declared parameters can still be bypassed via `extra_kwargs={"privileged": True, "pid_mode": "host"}`. These escape hatches are consistent with the "daemon is the trust boundary" model, but any allow/deny policy you build at the MCP-client layer must account for them rather than trusting the named parameters alone.
- **Registry credentials.** Many MCP clients log tool calls verbatim, so treat any password or `auth_config` you pass through a tool as exposed.
  - **SDK-backed tools** (`system_login`, `image_push`, `image_registry_data`) accept credentials directly *and* can reuse credentials cached by `docker login` in `~/.docker/config.json`. Prefer running `docker login` once on the host running this MCP server and leaving the credential parameters unset. (Note: this is the host running the server, not the daemon — relevant when `DOCKER_HOST` points at a remote daemon.) A credential passed to `system_login` is cached in the server's memory for the life of the client; `system_logout` clears that in-memory cache (all registries, or one) without touching `~/.docker/config.json`, and `system_close` / `system_reconnect` clear it by discarding the client. There is no daemon-side session to end — the Engine's `/auth` endpoint only validates.
  - **HTTPS-backed registry tools** (`registry_tags`, `registry_tag_wait`, `registry_manifest`, `registry_image_config`, `hub_tags`, `hub_repo_info`, `hub_rate_limit`) talk to the registry directly over HTTPS and do NOT read `~/.docker/config.json`. The `registry_*` tools accept `username` / `password` for private registries — or, better, read `DOCKER_MCP_SERVER_REGISTRY_USERNAME` / `DOCKER_MCP_SERVER_REGISTRY_PASSWORD` from the server's environment so credentials never transit tool arguments (see [Configuration](#configuration)); the `hub_*` tools currently support public Hub repositories only. If passing credentials as arguments, use a per-invocation token with the minimum required scope rather than a long-lived password. When a registry answers with a `Bearer` auth challenge, the server validates the token `realm` it points at before sending anything: the scheme must be http/https, plaintext http to a non-local host is rejected, and a public registry is not allowed to redirect the credentialed token request at a private/loopback address (an SSRF guard). A genuinely local dev registry (e.g. `localhost:5000`) may still use a local realm.
- **Swarm secret material transits tool calls too.** Beyond registry credentials, several swarm tools carry secret material through arguments or return values that MCP clients may log: `secret_create(data=...)` and `config_create(data=...)` take the payload as an argument, `secret_inspect` / `config_inspect` return the stored object, `swarm_join(join_token=...)` and `swarm_unlock(key=...)` take cluster join/unlock secrets, and `swarm_unlock_key` and `swarm_join_tokens` *return* cluster credentials (rotation via `swarm_update` invalidates old tokens) — a manager join token lets its holder join the swarm as a manager (root-equivalent on the cluster). Treat all of these as exposed in any client that records tool traffic, and prefer provisioning swarm secrets and reading join tokens out-of-band on the host rather than through the agent. If an agent never needs to admit nodes, drop the whole surface with `DOCKER_MCP_SERVER_DISABLE=swarm` (see [Configuration](#configuration)).
- **`container_exec`, `compose_exec`, and `compose_run` run arbitrary commands.** When any part of the command is derived from agent-controlled input, use an exec-form argv list that does not invoke a shell (e.g. `["python", "-V"]`). A list like `["sh", "-c", template]` that invokes a shell will interpret shell metacharacters in the untrusted substrings.
- **Container archive paths.** `container_archive_get` and `container_archive_put` forward the supplied path verbatim to the daemon. The container is the trust boundary — if you do not trust its filesystem, do not assume `..` traversal will be rejected.
- **File-path payload tools read and write the server host's filesystem.** `image_save`, `container_export` (with `dest_path`), and `container_archive_get_to_file` write to a `dest_path` on the host running this MCP server (refusing to overwrite an existing file unless `overwrite=True`); `image_load` and `container_archive_put` (with `from_file`) read a host path; `compose_cp` copies between a service container and a host path in either direction. These run as the server's user, so the agent can write any file that user can write and read any file it can read. Prefer the in-band byte tools (capped at 32 MiB) when you don't trust the agent with host filesystem access. `DOCKER_MCP_SERVER_READONLY` also drops the host-writing tools — but note it is not targeted at them: it registers *only* read-only tools, so `image_load` and `container_archive_put` (and every other mutating/destructive tool) go too. There is no switch that drops just the file-writers.
- **Destructive operations have no built-in confirmation.** `prune_*`, `remove_*`, `container_kill`, `swarm_leave`, `compose_down(volumes=True)`, `compose_kill`, `stack_remove` (tears down every service in a stack), `buildx_prune` (always runs with `--force`), and `buildx_remove` execute immediately. These tools carry the `destructiveHint` annotation, so a client like Claude Code can gate them, and the shipped `clean_environment` prompt asks the agent to confirm before pruning volumes — but tool calls themselves are not gated by the server. For a hard guarantee, run with `DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1` (drops them entirely) or `DOCKER_MCP_SERVER_READONLY=1` (see [Configuration](#configuration)); for an approval step, configure it at the MCP client.
- **CLI shell-out attack surface.** Compose, Context, Buildx, and Scout tools spawn `docker` subprocesses on the host running this MCP server. Every invocation passes arguments as a list (no shell, no metacharacter interpretation), resolves the binary via `shutil.which`, and runs against a scrubbed environment (DOCKER_HOST and related vars only). Positional values (image refs, service / context / builder names, build contexts) are additionally rejected if they start with `-`, so an argument can't be smuggled in as a CLI flag (e.g. a service named `--output=…`); the one deliberate exception is the trailing command in `compose_exec` / `compose_run`, which is meant to be an arbitrary argv. Filesystem paths supplied to `compose_*` (project_dir, files) are read by the docker CLI on the server host — passing an unfamiliar path can expose any compose file the server's user can read.
- **The daemon set is fixed at startup; pick it deliberately.** When `DOCKER_HOST` / `DOCKER_MCP_SERVER_HOSTS` are unset, the server's *initial* SDK connection follows your active Docker context (`DOCKER_CONTEXT` / `currentContext`) — the same daemon your `docker` CLI targets — so if that context points at a remote or production daemon, the agent connects there too. Set `DOCKER_MCP_SERVER_HOSTS` (or `DOCKER_HOST`, or select a scoped context) before starting the server to pin the target(s) deliberately; with `DOCKER_MCP_SERVER_HOSTS` the `auto`/`local` endpoints are resolved and **pinned at startup**, so they can't drift if a context changes later. After startup, `context_use` only changes the CLI default for subsequent CLI-backed tools; SDK-backed tools keep using the daemon their pooled client connected to. **There is no runtime way to introduce or retarget a daemon at an arbitrary endpoint** — `system_reconnect` only *rebuilds* an already-configured host's client (to recover a wedged connection), it can't point it elsewhere; to add or change a daemon, edit `DOCKER_MCP_SERVER_HOSTS` and restart. This deliberately closes a trust-expansion vector (an agent can't move the root-equivalent boundary to an unvetted endpoint mid-session). `context_create(skip_tls_verify=True)` disables TLS verification for a context; use only against trusted local daemons.
- **Per-host read-only is an accident guard, not a security boundary.** A host marked `(ro)` in `DOCKER_MCP_SERVER_HOSTS` makes mutating/destructive tools refuse to act on it at call time (and, with several hosts, writes require naming the target host explicitly — so the agent can't change the wrong daemon by omission). Like `guard_not_self`, this is in-process convenience: it constrains the agent through this server's tools, but the daemon itself is still the trust boundary, so for a host the agent must never modify, prefer pointing it at a genuinely read-only or scoped daemon over relying on the marker alone.

## Packages and listings

| Channel | Link |
|---------|------|
| PyPI | [docker-mcp-server](https://pypi.org/project/docker-mcp-server/) |
| GHCR (container) | [ghcr.io/l337-org/docker-mcp-server](https://github.com/L337-org/docker-mcp/pkgs/container/docker-mcp-server) |
| Docker Hub (container) | [gavinlucas/docker-mcp-server](https://hub.docker.com/r/gavinlucas/docker-mcp-server) |
| Desktop Extension (.mcpb) | [GitHub Releases](https://github.com/L337-org/docker-mcp/releases) |
| Official MCP Registry | [io.github.L337-org/docker-mcp-server](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.L337-org%2Fdocker-mcp-server/versions) |
| Glama | [docker-mcp-server](https://glama.ai/mcp/servers/L337-org/docker-mcp) |
| mcp.so | [docker-mcp-server](https://mcp.so/server/docker-mcp-server/GavinLucas) |
| awesome-mcp-servers | [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers#cloud-platforms) |

## Privacy Policy

docker-mcp-server collects no data, sends no telemetry, and has no author-operated backend. It runs
locally and talks only to the Docker daemon and container registries **you** point it at, as part of
the operations you request. The full statement is in [PRIVACY.md](https://github.com/L337-org/docker-mcp/blob/main/PRIVACY.md).

## Contributing

Contributions are welcome. The project values a tight mapping between the Docker SDK's public surface and the MCP tools we expose. See [CONTRIBUTING.md](https://github.com/L337-org/docker-mcp/blob/main/CONTRIBUTING.md) for the project layout, tool conventions, the checklist for adding a new tool module, and local development setup.
