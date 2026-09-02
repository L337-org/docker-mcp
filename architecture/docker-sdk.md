<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Docker SDK Policy

Deep detail behind the summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing any code that calls the `docker` package.

**Before writing or modifying any code that calls the Docker SDK (`docker` package), you MUST run `/docker-sdk` (or `/docker-sdk <topic>`) to:**
1. Verify exact method signatures from the live Docker SDK for Python documentation
2. Confirm parameter names and return types before writing code
3. Never use a `docker` module method that has not been confirmed in the docs

Do not assume any method exists because it sounds plausible. If you cannot confirm it from the documentation, say so and do not use it.

When the high-level SDK has no method for an operation (e.g. service rollback, or swarm task inspection, which has no model collection at all), drop to the low-level **`APIClient` via `_get_client(host).api`** - its methods (`update_service`, `inspect_service`, `inspect_task`, ...) are documented at https://docker-py.readthedocs.io/en/stable/api.html and must be verified the same way. Prefer the high-level object API when it exists; reach for `client.api` only for the gaps.

**Pass `host` through - always.** `_get_client(host: str | None = None)` makes it optional, so `_get_client()` compiles, reads as idiomatic and silently talks to the default daemon, discarding the caller's host selection. That is the one failure this server's whole multi-host design exists to prevent (see [hosts.md](hosts.md)). Every low-level call site in `docker_mcp/tools/` passes it, and a new one must not be the first that does not. The single deliberate exception is `startup_preflight`, which pings the default host before any caller exists (see [hosts.md](hosts.md)); nothing in a tool body should use the bare form.

**Verify against the source, not just the rendered docs, and treat "the method exists" as separate from "the method works."** The rendered docs show a method's docstring, not the URL it builds, so a method can be documented, importable, and still non-functional. `plugin_push` is the standing example: docker-py's `Plugin.push()` / `APIClient.push_plugin()` both POST to `/plugins/{name}/pull`, a route the Engine does not define (push is `POST /plugins/{name}/push`), so they 404 against every daemon - a copy-paste from the pull method present since 2017 and still in `main`, surviving because upstream has no test covering it. Where a *documented* method is provably broken, the fallback ladder is: (1) another public SDK path, (2) the correct endpoint through docker-py's private request helpers (`_url`/`_post`/`_raise_for_status`/`_stream_helper`), resolved via `getattr` and guarded so a missing helper raises an actionable message rather than an `AttributeError` - the same treatment as `system_logout`'s `api._auth_configs` reach-in and `stage_build_context`'s use of docker-py's `tar`/`exclude_paths`, (3) a CLI shell-out, if the domain is already CLI-backed. Each such reach-in must name the bug and the escape hatch in the tool's docstring, so it can be removed when upstream fixes it.

**Rungs (2) and (3) are not an agent's call to make.** Rung (1) is ordinary work; anything below it leaves the supported surface, so an agent - a routine, or anyone implementing from an audit issue - must **stop, write up what it found and why the public path fails, and escalate for a human decision** rather than implementing it. This is not a formality: it is how `plugin_push` was actually settled. The draft-PR routine hit the broken method, declined to reach past the public SDK, shipped the rest, and *documented the omission*; that write-up is what prompted the investigation that found the real endpoint and the human judgement to take it. A routine that had "helpfully" hand-rolled the call instead would have made a trust decision nobody asked it to make, and one that silently dropped the candidate would have buried it. **Document and escalate is the correct behaviour, not a failure to finish the job** - a parked candidate with a clear rationale is a better outcome than an autonomous workaround. Sign-off is needed once, when the reach-in is introduced: the ones listed here are already blessed, so touching or refactoring them later needs no fresh approval.

**Confirm the real route from the Engine API spec (`moby/moby`'s `api/swagger.yaml`) before writing a hand-built path, and note which kind of bet it is** - the two are not equivalent risks:

- **A published endpoint reached through private client plumbing** (what `plugin_push` does): `POST /plugins/{name}/push` is in the Engine API spec and is what `docker plugin push` itself calls, so the *contract* is stable and unlikely to move; only docker-py's `_url`/`_post` internals are unofficial, which is what the `getattr` guard covers. This is the acceptable shape.
- **An endpoint that is not in the spec at all** is a different proposition - no compatibility promise, no deprecation cycle, nothing to pin the behaviour. Do not use one, even guarded, without explicit human sign-off recorded in the PR; never on an agent's own initiative.

## SDK audit exclusions (deliberate non-candidates)

A recurring cloud routine audits the docker-py surface for coverage gaps and for low-level
`client.api.*` calls a high-level method could replace. The decisions below were made once, on the
merits, and are **not** to be re-proposed - a periodic audit has no memory of last time, so without
this list it re-files the same rejected candidates forever. Removing an entry is a real decision;
say why. **Anything deliberately not wrapped, or wrapped in an unobvious way, belongs here.**

- **`Plugin.push()` / `APIClient.push_plugin()`** - never migrate `plugin_push` onto these. They are
  broken upstream (wrong URL, see above); our hand-built endpoint call is the working path, not
  technical debt to be tidied away. Revisit only if upstream fixes the URL, at which point the
  reach-in should be replaced by the public method.
- **`Container.attach` / `attach_socket` / `resize`** - real methods, deliberately unwrapped: they
  open an interactive bidirectional stream/TTY, which does not fit a request/response tool call.
  `container_exec` covers scripted one-shot execution.
- **`service_rollback`'s `api.inspect_service` + `api.update_service`** - stays low-level
  permanently. The high-level `Service`/`ServiceCollection` expose no `rollback`.
- **`system_logout`'s `api._auth_configs`** - stays low-level permanently. There is no `logout`
  anywhere in the SDK and no server-side session to end, so there is nothing to migrate to.
- **`swarm_task_list` / `swarm_task_inspect`'s `api.tasks()` / `api.inspect_task()`** - stay
  low-level permanently. docker-py has no task collection at all (there is no `client.tasks`, and
  `docker/models/` has no `tasks.py`), so these documented `APIClient` methods are the only public
  path. Nothing to migrate to; do not propose one.
- **`image_import`'s `api.import_image_from_{file,data,url,image}`** - stay low-level permanently.
  `ImageCollection` has no import method at all, so there is no high-level path to migrate onto.
  Note the call-site comment explaining why the per-source methods are used rather than
  `import_image(src=...)`: `import_image_from_file`, `import_image_from_url` and
  `import_image_from_image` all `return self.import_image(...)`, so choosing them does **not** avoid
  its "unreadable path is retried as a URL" behaviour - that is a documented hazard guarded at our
  call site, not a fix. (`import_image_from_data` is the one exception: it posts directly via
  `self._result`.)
- **`plugin_privileges`'s `api.plugin_privileges`** - stays low-level permanently. `PluginCollection`
  exposes no privileges call; docker-py's own `install` calls this same low-level method internally.
- **`swarm_update`'s `api.update_swarm`** - stays low-level permanently. The high-level
  `Swarm.update()` builds its request body from docker-py kwargs via `create_swarm_spec`, so it
  cannot resubmit the daemon's own spec document; against a replace-semantics endpoint that silently
  clears manager autolock, cluster labels, the default task log driver and any
  Raft/Orchestration/Dispatcher tuning. Round-tripping the document through those kwargs is lossy in
  its own right: `SwarmSpec` drops the whole `Raft` block unless one of its five values is truthy,
  and knows nothing of Engine fields added later. `APIClient.update_swarm` is the only faithful path.
- **`DockerClient.from_context()`** (new public surface in docker-py 7.2.0) - never adopt it. It
  resolves the daemon from the local CLI context, which is exactly what `use_context=False` and
  `_hosts.resolve_auto()` exist to prevent: this server selects its daemon per call from its own host
  registry, not from whatever context the machine happens to be on. It reads as an obvious uncovered
  candidate precisely because it is new and plausible, which is why it is recorded here.
- **`ServiceSpec.Networks` on `create_service` / `update_service`** - not a deprecation we are
  exposed to, and not to be re-raised. `networks` is in `TASK_TEMPLATE_KWARGS`
  (`docker/models/services.py`), so `ServiceCollection.create` - the high-level path behind
  `service_create` - moves `networks` into `TaskTemplate` before calling `create_service`. That call
  then sends `"Networks": convert_service_networks(None)`, and `convert_service_networks` returns its
  falsy input unchanged, so the deprecated top-level field goes out empty.
  `update_service` writes `data['TaskTemplate']['Networks']` on anything from API v1.25 upwards.
  Two audit runs reached opposite conclusions on this in the same week; the above is what the 7.2.0
  source actually does.

The audit must also **check the latest published docker-py, not the pinned one**: `uv.lock` is
routinely behind what `pyproject.toml`'s floor lets a fresh `uvx`/`pip install` resolve, so auditing
the installed tree alone misses whatever published users are already running. And it should flag
**deprecated** surface we still depend on, not only missing coverage - e.g. `image_prune_builds`'s
`keep_storage`, which the Engine renamed `reserved-space` at API v1.48 - so a migration happens on
our schedule rather than when removal breaks us.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html  
Docker SDK low-level API: https://docker-py.readthedocs.io/en/stable/api.html  
Docker SDK GitHub: https://github.com/docker/docker-py
