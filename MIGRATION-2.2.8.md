# Upgrading to docker-mcp-server 2.2.8

2.2.8 removes seven tools to cut what every session pays for the server's tool list. Six of them
did jobs another tool now does, so an agent can still do everything it could before; it simply
calls a different tool, and finds it the same way it finds any other. Only one capability is gone:
creating and publishing plugins, which is a build-pipeline job for the `docker plugin` CLI.

An agent needs nothing from you: tool definitions are fetched afresh each session. What may need
updating is configuration that names a tool or a domain, listed at the end.

## Removed tools

| Removed | Do this instead |
|---|---|
| `swarm_task_list` | `service_ps` with no service: every task in the swarm, with the same filters (`node`, `service`, `desired-state`, ...) |
| `swarm_task_inspect` | `service_ps(filters={"id": ...})` for a full id or id prefix, or `{"name": ...}` for the full `<service>.<slot>.<taskid>` name. Both match by prefix, so an ambiguous prefix returns several tasks rather than an error, and only container tasks come back unless `runtime` is filtered |
| `swarm_task_logs` | `service_logs(task=...)`, with the same `tail`, `since` and `max_bytes` applying to that one task |
| `plugin_privileges` | `plugin_install(remote, dry_run=True)` before an install, or `plugin_upgrade(name, dry_run=True)` before an upgrade, which grants new privileges the same way |
| `image_prune_builds` | `buildx_prune`, which prunes through the Engine API itself when the buildx plugin is missing. On that path it refuses `builder` and the space limits rather than ignoring them |
| `plugin_create`, `plugin_push` | No replacement. Use `docker plugin create` and `docker plugin push` |

## Changed tools

- **`service_ps`**: `id_or_name` is optional. Omit it for every task in the swarm.
- **`service_logs`**: takes `task` as an alternative to `id_or_name`; pass exactly one.
- **`plugin_install`, `plugin_upgrade`**: take `dry_run`, which returns `{"remote", "privileges"}`
  and changes nothing.
- **`buildx_prune`**: works without the buildx plugin, through the Engine API, and then returns the
  Engine's `{"CachesDeleted", "SpaceReclaimed"}` instead of the CLI result.
- **`tool_list`**: `keyword` may be several words. A tool matching any of them is returned, those
  matching the most first.

## Configuration that may need updating

- **Permission allow-lists** that name a removed tool, for example in a client's settings, do not
  cover the tool that replaces it: the client will ask, or refuse where nobody can answer. Add the
  replacement from the table above.
- **`DOCKER_MCP_SERVER_DISABLE`**: reading swarm tasks now belongs to the `services` domain, the
  domain of `service_ps` and `service_logs`, not to `swarm`. `DOCKER_MCP_SERVER_DISABLE=swarm` no
  longer hides it; `DOCKER_MCP_SERVER_DISABLE=services` does. `tool_list(domain="swarm")` no longer
  lists it either.
- **`DOCKER_MCP_SERVER_READONLY`**: previewing a plugin's privileges is a dry run of
  `plugin_install` / `plugin_upgrade`, which a read-only server does not register, so it is no
  longer available there.
- **Scripts** that call a removed tool by name fail with an unknown-tool error.
