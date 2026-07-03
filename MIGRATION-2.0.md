# Migrating to docker-mcp-server 2.0

2.0 renames the entire tool surface to one convention, merges duplicate tools, removes two dead
ones, standardizes parameter names, and drops the deprecated `DOCKER_MCP_*` env-var aliases. It is
a **clean break**: no old name — tool, parameter, or env var — is honored. Nothing else changes:
transports, safety classifications (beyond the two noted below), resources, prompts, and
provenance labels all behave as in 1.x.

## The naming convention

Every tool is named `<management-command>_<verb>`, anchored to the docker CLI's own
management-command structure (`docker container ls` → `container_list`), with long-form verbs
(`list`/`remove`/`inspect` — never `ls`/`rm`/`get`). Names never encode the backing implementation
(SDK vs CLI), so tools can move between backends without renaming. Read-only fetches may be
noun-form (`container_logs`, `registry_tags`).

## Tool renames (old → new)

**system** (domain renamed from `client` — update `DOCKER_MCP_SERVER_DISABLE=client` to `system`):

| 1.x | 2.0 |
|---|---|
| `ping` | `system_ping` |
| `version` | `system_version` |
| `info` | `system_info` |
| `df` | `system_df` |
| `events` | `system_events` |
| `login` | `system_login` |
| `logout` | `system_logout` |
| `close` | `system_close` |
| `reconnect` | `system_reconnect` |
| `list_hosts` | `host_list` |

**containers:**

| 1.x | 2.0 |
|---|---|
| `list_containers` | `container_list` |
| `run_container` | `container_run` |
| `create_container` | `container_create` |
| `get_container` | `container_inspect` |
| `start_container` | `container_start` |
| `stop_container` | `container_stop` |
| `restart_container` | `container_restart` |
| `kill_container` | `container_kill` |
| `pause_container` | `container_pause` |
| `unpause_container` | `container_unpause` |
| `remove_container` | `container_remove` |
| `rename_container` | `container_rename` |
| `update_container` | `container_update` |
| `exec_in_container` | `container_exec` |
| `commit_container` | `container_commit` |
| `prune_containers` | `container_prune` |
| `wait_container` | `container_wait` (merged, see below) |
| `wait_for_container_healthy` | `container_wait(until="healthy")` |
| `follow_container_logs` | `container_logs(follow=True)` |
| `export_container` | `container_export` |
| `export_container_to_file` | `container_export(dest_path=...)` |
| `get_container_archive` | `container_archive_get` |
| `get_container_archive_to_file` | `container_archive_get_to_file` |
| `put_container_archive` | `container_archive_put` |
| `put_container_archive_from_file` | `container_archive_put(from_file=...)` |
| `resize_container` | removed (TTY resize; no agent value) |

**images:**

| 1.x | 2.0 |
|---|---|
| `list_images` | `image_list` |
| `pull_image` | `image_pull` |
| `push_image` | `image_push` |
| `build_image` | `image_build` |
| `get_image` | `image_inspect` |
| `remove_image` | `image_remove` |
| `tag_image` | `image_tag` |
| `save_image` | `image_save` |
| `save_image_to_file` | `image_save(dest_path=...)` |
| `load_image` | `image_load` |
| `load_image_from_file` | `image_load(from_file=...)` |
| `search_images` | `image_search` |
| `prune_images` | `image_prune` |
| `get_registry_data` | `image_registry_data` |

**networks / volumes / configs / secrets / nodes** (uniform pattern):

| 1.x | 2.0 |
|---|---|
| `list_networks` / `create_network` / `get_network` / `remove_network` / `prune_networks` | `network_list` / `network_create` / `network_inspect` / `network_remove` / `network_prune` |
| `connect_network` / `disconnect_network` | `network_connect` / `network_disconnect` |
| `list_volumes` / `create_volume` / `get_volume` / `remove_volume` / `prune_volumes` | `volume_list` / `volume_create` / `volume_inspect` / `volume_remove` / `volume_prune` |
| `list_configs` / `create_config` / `get_config` / `remove_config` | `config_list` / `config_create` / `config_inspect` / `config_remove` |
| `list_secrets` / `create_secret` / `get_secret` / `remove_secret` | `secret_list` / `secret_create` / `secret_inspect` / `secret_remove` |
| `list_nodes` / `get_node` / `update_node` / `remove_node` | `node_list` / `node_inspect` / `node_update` / `node_remove` |

**services:**

| 1.x | 2.0 |
|---|---|
| `list_services` | `service_list` |
| `create_service` | `service_create` |
| `get_service` | `service_inspect` |
| `update_service` | `service_update` |
| `force_update_service` | `service_update(force=True)` |
| `remove_service` | `service_remove` |
| `scale_service` | `service_scale` |
| `rollback_service` | `service_rollback` |

**swarm:**

| 1.x | 2.0 |
|---|---|
| `init_swarm` | `swarm_init` |
| `join_swarm` | `swarm_join` |
| `leave_swarm` | `swarm_leave` |
| `update_swarm` | `swarm_update` |
| `unlock_swarm` | `swarm_unlock` |
| `reload_swarm` | `swarm_inspect` |
| `get_swarm_unlock_key` | `swarm_unlock_key` |
| `get_swarm_join_tokens` | `swarm_join_tokens` |
| `rotate_swarm_join_token` | `swarm_join_token_rotate` |

**plugins:**

| 1.x | 2.0 |
|---|---|
| `list_plugins` | `plugin_list` |
| `install_plugin` | `plugin_install` |
| `get_plugin` | `plugin_inspect` |
| `enable_plugin` | `plugin_enable` |
| `disable_plugin` | `plugin_disable` |
| `configure_plugin` | `plugin_configure` |
| `upgrade_plugin` | `plugin_upgrade` |
| `remove_plugin` | `plugin_remove` |
| `push_plugin` | removed (plugin-author tooling) |

**CLI domains** (only the short-form-verb names changed):

| 1.x | 2.0 |
|---|---|
| `compose_ls` | `compose_list` |
| `stack_ls` / `stack_rm` | `stack_list` / `stack_remove` |
| `context_ls` / `context_rm` | `context_list` / `context_remove` |
| `buildx_ls` / `buildx_rm` / `buildx_history_ls` | `buildx_list` / `buildx_remove` / `buildx_history_list` |

All other `compose_*`, `stack_*`, `context_*`, `buildx_*`, and `scout_*` names are unchanged.

**registry / hub:**

| 1.x | 2.0 |
|---|---|
| `registry_list_tags` | `registry_tags` |
| `registry_inspect_manifest` | `registry_manifest` |
| `registry_get_config` | `registry_image_config` |
| `hub_list_tags` | `hub_tags` |

## Merged tools — behavior notes

- **`container_logs`**: `follow=True` replaces `follow_container_logs`; `limit_lines` and
  `timeout_seconds` apply only in follow mode, `until` only in snapshot mode.
- **`container_wait`**: one contract for every mode — `until` is `"not-running"` (default) /
  `"next-exit"` / `"removed"` / `"healthy"`, and **timeouts no longer raise**: every outcome
  returns `{container, until, met, timed_out, status_code, error, health, status,
  waited_seconds}`. The 1.x `timeout=None` wait-forever escape hatch is gone (`timeout_seconds`
  defaults to 600).
- **`service_update`**: pass exactly one of `updates` or `force=True` (the
  `docker service update --force` redeploy).
- **`container_archive_put` / `image_load`**: pass exactly one of `data` (in-band bytes) or
  `from_file` (server-host path).
- **`container_export` / `image_save`**: optional `dest_path` streams to a server-host file;
  without it the capped in-band bytes return as before. Both tools are now classified
  **MUTATING** (they can write host files), so they no longer register under
  `DOCKER_MCP_SERVER_READONLY`. `container_archive_get` / `container_archive_get_to_file` remain
  a split pair: the READ_ONLY streaming half is the only way to read a file out of a container
  under READONLY.

## Parameter renames

- Identifier params follow one rule: `id_or_name` for daemon objects addressable by either
  (was `network_id`, `service_id`, `config_id`, `secret_id`, `node_id`, and images' `name`/
  `image`); `name`/`names` for name-only resources (was `volume_id`, `stack_name`,
  `stack_names`); `repository` for remote repo refs (was `image` on `registry_*`, `name` on
  `image_registry_data`/`image_list`).
- `timeout` → `timeout_seconds` everywhere (`container_stop`, `container_restart`,
  `plugin_enable`).
- `container_remove(v=...)` → `volumes`.
- `buildx_imagetools_create(files=...)` → `descriptor_files`.

## Environment variables

The pre-rename `DOCKER_MCP_*` alias spellings are no longer honored. Use the canonical
`DOCKER_MCP_SERVER_*` names: `READONLY`, `NO_DESTRUCTIVE`, `DISABLE`, `HOSTS`,
`REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `ALLOW_SELF_TERMINATE`, `IN_CONTAINER`, `NO_LABELS`,
`NAME`. And note the domain rename: `DOCKER_MCP_SERVER_DISABLE=client` becomes
`DOCKER_MCP_SERVER_DISABLE=system`.

## Provenance labels

Resources created by 2.0 stamp the new tool names into the `docker-mcp-server.tool` label
(`container_run`, `volume_create`, ...). Resources created by 1.x keep the old names in their
labels — harmless: `managed_only` filtering keys off `docker-mcp-server.managed=true`, not the
tool name.

## Client permission allowlists

If your MCP client pins tool names in a permission allowlist (e.g. Claude Code's
`mcp__docker-mcp-server__list_containers`), update them to the new names or use a server-level
wildcard.
