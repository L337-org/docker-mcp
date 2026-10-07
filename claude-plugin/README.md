# docker-mcp-server

Manage one or more Docker daemons from Claude: containers, images, networks, volumes, Compose,
Swarm, Buildx, Scout and OCI registries, each exposed as its own typed MCP tool.  Reads are marked
read-only and destructive actions are flagged separately, so a client can auto-approve reads while
always confirming anything destructive.

## What this plugin runs

The plugin starts one local MCP server with `uvx docker-mcp-server==<version>`, pinned to the
release this plugin version was built from.  On first use `uv` downloads that release and its
dependencies from [PyPI](https://pypi.org/project/docker-mcp-server/) and caches them; after that
it runs from the cache.  You need [uv](https://docs.astral.sh/uv/) installed, and `uv` fetches a
suitable Python (3.14 or later) itself if you do not have one.  On Intel (x86_64) macOS, check the
[project requirements](https://github.com/L337-org/docker-mcp#requirements) first.

Once running, the server's network activity is limited to:

- the Docker daemons you configure, over the local socket, TCP, TLS or SSH (using your own SSH
  keys and agent);
- container registries and Docker Hub, only when you ask for a pull, push or image query;
- Docker's Scout service, through Docker's own `scout` CLI plugin, only for the Scout tools and
  only when that plugin is installed;
- a fixed list of documentation hosts (docs.docker.com, docker-py.readthedocs.io,
  distribution.github.io and github.com) for its built-in Docker reference lookups.

It sends no telemetry and has no backend - see the
[Privacy Policy](https://github.com/L337-org/docker-mcp/blob/main/PRIVACY.md).

## Settings

Set these under `/plugin` > **Installed** > **Configure**.  All are optional.

- **Docker host(s)**: blank uses your default Docker context or socket.  One endpoint
  (`ssh://user@host`, `tcp://host:2376(tls=/path/to/certs)`) manages a remote daemon; a
  comma-separated `label=endpoint` list manages several, with `(ro)` or `(nd)` after an endpoint
  to make that host read-only or non-destructive.
- **Read-only mode**: register only read-only tools.
- **Disable destructive tools**: register everything except removals, prunes and kills.
- **Disabled domains**: a comma-separated list of tool domains to drop, such as `swarm,scout`.

Full documentation, including remote daemons and the security model, is in the
[project README](https://github.com/L337-org/docker-mcp#readme).  Licensed MIT.
