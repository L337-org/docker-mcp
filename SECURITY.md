# Security Policy

## Reporting a vulnerability

If you believe you have found a security issue in `docker-mcp`, please open a
private vulnerability report via GitHub's
[security advisory flow](https://github.com/GavinLucas/docker-mcp/security/advisories/new)
rather than filing a public issue. That keeps the discussion private until a
fix is available.

## Threat model and operational risks

The trust boundary, credential-handling guidance, and known risks
(privileged containers, host bind mounts, `exec_in_container`, container
archive paths, destructive operations) are documented in
[README.md → Security considerations](README.md#security-considerations).
Read that section before connecting an AI agent to this server.

In short: the Docker daemon's socket is effectively root-equivalent on its
host, and this server exposes the full Docker SDK surface. There is no
per-tool authorization layer — the daemon is the trust boundary. Treat the
agent as a privileged user, and prefer pointing `DOCKER_HOST` at a scoped
daemon (development VM, remote sandbox, Docker Desktop, rootless install)
rather than a production socket.
