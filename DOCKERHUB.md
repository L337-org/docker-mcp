# docker-mcp-server

More than just a fully featured [MCP](https://modelcontextprotocol.io) server that lets AI agents manage Docker — containers, images, networks, volumes, swarm services, secrets, configs, nodes, plugins, etc., it helps you create workflows to easily manage your Docker environments.

It can manage multiple Docker daemons, e.g. both your local dev environment and also a remote production environment over TCP, TLS or SSH in a single session. It can also be configured to mark some daemons as read-only, so that you can monitor them without the risk of making accidental changes. It also exposes things like logs and stats as resources so that you can easily monitor and triage your environments with a few prompts.

The server runs entirely on your machine and sends no telemetry. You are entirely in control — see the [Privacy Policy](https://github.com/L337-org/docker-mcp#privacy-policy).

This image is the container distribution of the project. **Full documentation, configuration, and source are on GitHub: <https://github.com/L337-org/docker-mcp>.**

> The same images are published to GHCR as [`ghcr.io/l337-org/docker-mcp-server`](https://github.com/L337-org/docker-mcp/pkgs/container/docker-mcp-server) — identical content, so use whichever registry you prefer.

## Quick start

The only host prerequisite is Docker. Point your MCP client at `docker run`:

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

`-i` is required (the server speaks MCP over stdio); `--rm` cleans up when the client disconnects. To pin a version, replace `:latest` with a release tag (e.g. `:1.5.1`).

## Image variants

Two multi-arch (linux/amd64 + linux/arm64) variants are published, both built from one `Dockerfile`. The CLI-backed domains (Compose, Stack, Buildx, Scout, Context) shell out to the `docker` CLI and its plugins.

| Variant | Tags | Approx. size | Includes |
|---------|------|-------------|----------|
| `full` *(default)* | `:latest`, `:<version>` | ~510 MB | docker CLI + compose + buildx + **scout** |
| `no-scout` | `:no-scout`, `:<version>-no-scout` | ~315 MB | docker CLI + compose + buildx |

Scout's plugin binary accounts for the ~195 MB difference. The `no-scout` image also defaults `DOCKER_MCP_SERVER_DISABLE=scout`, so the scout *tools* aren't registered — the agent sees a smaller, fully-working tool list rather than scout tools that error on every call.

## Reaching the daemon

The image defaults `DOCKER_HOST` to `unix:///var/run/docker.sock`, so mounting your host's socket onto that path is all that's needed. Where the host socket *is* varies by platform (the server prints a hint to stderr if it can't connect at startup):

- **Linux:** `-v /var/run/docker.sock:/var/run/docker.sock` (rootless: `-v $XDG_RUNTIME_DIR/docker.sock:/var/run/docker.sock`).
- **macOS (Docker Desktop):** usually `-v $HOME/.docker/run/docker.sock:/var/run/docker.sock`.
- **Windows (Docker Desktop / WSL2):** prefer `-e DOCKER_HOST=tcp://host.docker.internal:2375` (enable the TCP endpoint in Docker Desktop). That endpoint is **unauthenticated and unencrypted** — keep it bound to localhost, disable it when you're not using it, and use TLS or `DOCKER_HOST=ssh://...` for any remote daemon.
- **Remote / TLS / SSH daemon:** skip the socket mount and pass `-e DOCKER_HOST=...` (plus the TLS vars).

## Host filesystem access

Inside a container, the file-path tools (`image_save` / `container_export` with `dest_path`, `image_load` / `container_archive_put` with `from_file`, `container_archive_get_to_file`, and compose `project_dir` / `files`) resolve paths *inside the container*. Bind-mount any directory you want to exchange files through — using the **same path inside and out** keeps host and container paths identical:

```
-v $HOME/docker-work:$HOME/docker-work
```

If you call one of these tools with a path that isn't on a bind mount, the server refuses up front with a message telling you which `-v` to add. (The in-band byte tools, capped at 32 MiB, need no mount.)

## Configuration

Set these in the client's `env` block. Three switches restrict which tools are registered at startup (a disabled tool never appears in the client's tool list):

- **`DOCKER_MCP_SERVER_READONLY`** — register only read-only tools.
- **`DOCKER_MCP_SERVER_NO_DESTRUCTIVE`** — everything except destructive tools (`*_remove`, `*_prune`, `container_kill`, `compose_down`, …).
- **`DOCKER_MCP_SERVER_DISABLE`** — comma-separated *domains* to drop wholesale (e.g. `swarm,services,nodes,configs,secrets` for a single-host server).

`DOCKER_HOST` / `DOCKER_TLS_VERIFY` / `DOCKER_CERT_PATH` retarget the daemon (e.g. `-e DOCKER_HOST=tcp://remote-host:2375`).

See the [full configuration reference](https://github.com/L337-org/docker-mcp#configuration) for the complete list and examples.

## Security

Connecting this server to an AI agent grants it the same access as a local Docker CLI session against the configured daemon — the daemon's socket is effectively root-equivalent on its host. Prefer pointing it at a daemon dedicated to workloads the agent may touch (a dev VM, a remote sandbox, Docker Desktop, a rootless install) rather than your production socket. See the [Security considerations](https://github.com/L337-org/docker-mcp#security-considerations) on GitHub before enabling the server.

## Links

- **Source, full docs & issues:** <https://github.com/L337-org/docker-mcp>
- **Also on PyPI** (run without a container): `uvx docker-mcp-server` — see the [README](https://github.com/L337-org/docker-mcp#using-the-server).
- **License & contributing:** see the GitHub repository.
