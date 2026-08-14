# Privacy Policy

**docker-mcp-server** does not collect, store, or transmit any personal data, usage analytics, or
telemetry. There is no tracking of any kind, and nothing is ever sent back to the author or any
third party.

The server runs entirely on your own machine (or wherever you choose to run it) as a local process.
Its network activity is limited to the endpoints **you** direct it at:

- **Your Docker daemon(s)**: over the local socket, a TCP endpoint, or an `ssh://` host, as
  configured by `DOCKER_MCP_SERVER_HOSTS` (or, when that is unset, `DOCKER_HOST` / your Docker
  context). These are the daemons you ask it to manage. For an `ssh://` daemon the server also makes
  the SSH connection itself, using your own SSH configuration, keys and agent.
- **Container registries and Docker Hub**: only when you invoke a tool that pulls, pushes, or
  queries an image (e.g. `image_pull`, `registry_tags`, `hub_repo_info`). These requests go directly
  to the registry you target, authenticated with credentials you supply; they are a normal part of
  the Docker operation you requested, not a data-collection mechanism.
- **Docker Scout's service**: the `scout_*` tools shell out to Docker's own `scout` CLI plugin,
  which contacts Docker's Scout backend to analyse an image rather than working purely locally. That
  is Docker's service under Docker's privacy policy, not ours, and those tools are absent unless the
  plugin is installed. `DOCKER_MCP_SERVER_DISABLE=scout` removes them.
- **Docker's documentation sites**: the `docs_lookup` tool and the `docker-docs://` resources fetch
  reference documentation from a fixed list of documentation hosts (docs.docker.com,
  docker-py.readthedocs.io, distribution.github.io and github.com). The list is built into the
  server; the agent chooses which section to read, never the host. Nothing about your daemon, images
  or containers is sent with these requests.

No credentials, image contents, command output, or daemon data pass through any author-operated
service. There is no author-operated service.

Because the server is invoked by an MCP client (such as Claude Desktop), that client and the
AI provider behind it have their own privacy policies governing the conversation and any tool
results surfaced to the model. This policy covers only the behaviour of docker-mcp-server itself.

## Contact

Questions about this policy: open an issue at
<https://github.com/L337-org/docker-mcp/issues>.
