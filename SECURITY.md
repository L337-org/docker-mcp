# Security Policy

## Reporting a vulnerability

If you believe you have found a security issue in `docker-mcp`, please open a
private vulnerability report via GitHub's
[security advisory flow](https://github.com/GavinLucas/docker-mcp/security/advisories/new)
rather than filing a public issue. That keeps the discussion private until a
fix is available.

## Threat model and operational risks

The trust boundary, credential-handling guidance, and known risks
(privileged containers, host bind mounts, `container_exec`,
`compose_exec` / `compose_run`, container archive paths, destructive
operations, secret material in tool traffic — registry credentials,
swarm secrets/configs, and swarm join/unlock tokens (the manager join
token is cluster-root-equivalent) — the CLI shell-out attack surface,
the startup-fixed daemon set, and the per-host read-only guard) are
documented in
[README.md → Security considerations](README.md#security-considerations).
Read that section before connecting an AI agent to this server.

In short: the Docker daemon's socket is effectively root-equivalent on its
host, and this server exposes the full Docker SDK surface plus selected
docker CLI features (Compose, Context) and direct registry HTTPS access.
There is no per-tool authorization layer — the daemon (and, for CLI-backed
tools, the host running this server) is the trust boundary. Treat the
agent as a privileged user, and prefer pointing the server at a scoped
daemon (development VM, remote sandbox, Docker Desktop, rootless install)
rather than a production socket — via `DOCKER_MCP_SERVER_HOSTS` (or
`DOCKER_HOST`). The daemon set is fixed at startup (no runtime retarget to
an arbitrary endpoint), and a host listed `(ro)` in `DOCKER_MCP_SERVER_HOSTS`
makes the agent refuse to mutate it — an accident guard, not a substitute
for a genuinely scoped daemon.
