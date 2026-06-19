# Privacy Policy

**docker-mcp-server** does not collect, store, or transmit any personal data, usage analytics, or
telemetry. There is no tracking of any kind, and nothing is ever sent back to the author or any
third party.

The server runs entirely on your own machine (or wherever you choose to run it) as a local process.
Its network activity is limited to the endpoints **you** direct it at:

- **Your Docker daemon** — over the local socket, a TCP endpoint, or an `ssh://` host, as configured
  by `DOCKER_HOST` / your Docker context. This is the daemon you ask it to manage.
- **Container registries and Docker Hub** — only when you invoke a tool that pulls, pushes, scans, or
  queries an image (e.g. `pull_image`, `registry_list_tags`, `scout_cves`, `hub_repo_info`). These
  requests go directly to the registry you target, authenticated with credentials you supply; they
  are a normal part of the Docker operation you requested, not a data-collection mechanism.

No credentials, image contents, command output, or daemon data pass through any author-operated
service. There is no author-operated service.

Because the server is invoked by an MCP client (such as Claude Desktop), that client and the
AI provider behind it have their own privacy policies governing the conversation and any tool
results surfaced to the model. This policy covers only the behaviour of docker-mcp-server itself.

## Contact

Questions about this policy: open an issue at
<https://github.com/GavinLucas/docker-mcp/issues>.
