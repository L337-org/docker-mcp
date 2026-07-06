# library of mcp prompt templates that guide the agent through common docker workflows

import docker_mcp._hosts as _hosts
from docker_mcp.server import prompt


def _host_targeting_note() -> str:
    """A trailing block (multi-host only) correcting the resource URIs and explaining host targeting.

    The prompt bodies use the single-host `docker://containers` / `docker-…://{name}` forms, which are
    NOT registered in multi-host mode (the index becomes empty-authority/host-qualified), so this note
    redirects the agent to the right forms and to the URIs the index already carries per entry.
    """
    if not _hosts.is_multi():
        return ""
    return (
        "\nMulti-host: the bare `docker://containers` / `docker-logs://{name}` / `docker-stats://{name}` "
        "forms above are single-host only. Here the container index is `docker:///containers` for the "
        "default host or `docker://{host}/containers` for a named one; pass `host=<name>` to the tools. "
        "Don't build `docker-logs`/`docker-stats` URIs yourself — follow the `logs`/`stats` URIs each "
        "index entry carries, which are already in the correct (empty-authority or host-qualified) form. "
        "See `docker-mcp://hosts` for the configured hosts."
    )


@prompt(description="Read the Docker SDK for Python documentation for a section before writing code that uses it.")
def lookup_docker_docs(section: str) -> str:
    """
    Ask the agent to consult the Docker SDK for Python documentation for a specific section.

    args: section - SDK section name (e.g. "containers", "images", "swarm")
    returns: str - A prompt instructing the agent to read the docker-docs resource and summarize the API
    """
    return (
        f"Read the MCP resource `docker-docs://{section}` and summarize the public methods, their signatures, "
        f"and return types. Highlight anything that is easy to misuse (parameters that look similar, surprising "
        f"defaults, methods that return iterators vs. lists). Do not assume any method exists unless it is "
        f"present in that resource."
    )


@prompt(description="Verify that a specific Docker SDK method exists before relying on it.")
def verify_docker_method(method: str, section: str) -> str:
    """
    Ask the agent to verify a method of the `docker` module against the live SDK docs.

    args: method - The method name to verify (e.g. "containers.run")
    args: section - The SDK section to check (e.g. "containers")
    returns: str - A prompt instructing the agent to confirm the method's signature from the docs
    """
    return (
        f"Read the MCP resource `docker-docs://{section}` and confirm whether `{method}` exists. "
        f"If it does, quote its full signature, list each parameter with its type, and describe the return value. "
        f"If it does not exist, say so explicitly and suggest the closest documented alternative."
    )


@prompt(
    description="Deploy a containerized application end-to-end: image, network, volume, container.", domain="containers"
)
def deploy_container(image: str, name: str) -> str:
    """
    Generate a step-by-step plan for deploying a container with supporting resources.

    args: image - The image reference to deploy (e.g. "nginx:1.27")
    args: name - The container name to assign
    returns: str - A prompt instructing the agent to walk through deployment using the MCP tools
    """
    return (
        f"Deploy the image `{image}` as a container named `{name}` using the docker MCP tools. Follow this order:\n"
        f"1. Call `image_pull` to ensure the image is present locally.\n"
        f"2. Decide whether the workload needs a dedicated network or named volume; create them with "
        f"`network_create` / `volume_create` if so.\n"
        f"3. Call `container_run` with sensible defaults: `detach=True`, a restart policy, and any port or volume "
        f"mappings the image requires.\n"
        f'4. Call `container_wait(id_or_name=name, until="healthy")` to confirm it started successfully. '
        f"`met=True` means the image's HEALTHCHECK passed; `health: null` with `met=False` just means the image "
        f'has no HEALTHCHECK (not that something is wrong) — check `status == "running"` instead. Follow up '
        f"with `container_logs` if anything looks off.\n"
        f"Report the final container ID and any resources you created. Stop and ask before destroying existing "
        f"resources that share the same name."
    )


@prompt(description="Troubleshoot a misbehaving container by gathering logs, state, and stats.", domain="containers")
def troubleshoot_container(container: str) -> str:
    """
    Generate a diagnostic plan for an unhealthy or failing container.

    args: container - Container name or ID to investigate
    returns: str - A prompt instructing the agent to gather logs, inspect state, and propose a fix
    """
    return (
        f"Diagnose what is wrong with container `{container}`:\n"
        f"1. Use `container_inspect` to read its current state, exit code, and restart count.\n"
        f"2. Use `container_logs` (with `tail=200`) to capture recent stdout/stderr.\n"
        f"3. If the container is running, use `container_stats` for CPU/memory pressure and `container_top` "
        f"for the process tree.\n"
        f"4. If a config file or process check is needed, use `container_exec`.\n"
        f"Summarize the root cause in one paragraph, then propose a concrete fix (config change, image bump, "
        f"resource limit) before making any changes."
    )


@prompt(
    description="Sweep every running container for health and resource pressure (read-only monitoring).",
    domain="containers",
)
def monitor_container_fleet(top: int = 5) -> str:
    """
    Generate a read-only monitoring sweep across all containers, ranked by resource pressure.

    Unlike `troubleshoot_container` (which needs a named target), this starts from the whole fleet and
    surfaces what is unhealthy or under load — the entry point when you don't yet know what's wrong.
    It leans on the observability resources: `docker://containers` to enumerate, then `docker-stats://`
    and `docker-logs://` per container.

    args: top - How many heaviest containers to drill into with logs (default 5)
    returns: str - A prompt instructing the agent to enumerate, sample stats, and rank by pressure
    """
    return (
        "Take a read-only health and load snapshot of every container on this host. Change nothing:\n"
        "1. Read the MCP resource `docker://containers` to enumerate every container. Each entry carries "
        "its `status`, an `exit_code` when it has exited, and per-container `logs`/`stats` resource URIs.\n"
        "2. Immediately flag the non-healthy set: anything `exited` with a non-zero `exit_code`, "
        "`restarting` (a likely crash loop), `paused`, or `dead`. List those first — they matter more "
        "than load.\n"
        "3. For each container whose `status` is `running`, read its `docker-stats://{name}` resource for "
        "a CPU%, memory used/limit/%, and net/block I/O snapshot. Note that this is a single instantaneous "
        "sample, not an average — a one-off spike is not the same as sustained pressure.\n"
        f"4. Rank the running containers by resource pressure and drill into the top {top}: read each "
        "one's `docker-logs://{name}` resource for a recent tail and surface any error/restart lines. "
        "Cross-reference memory% near 100 (OOM-kill risk) and CPU pinned at a limit.\n"
        "5. For any container flagged unhealthy in step 2, read its `docker-logs://{name}` (logs are "
        "readable even on a stopped container) to capture why it exited or is looping.\n"
        "Render one table: name, status, CPU%, mem%, and a one-line health note per container, sorted "
        "with problems on top. End with a one-paragraph verdict naming the single container most worth "
        "attention and point at `troubleshoot_container` for a deep dive on it. Recommend nothing "
        "destructive." + _host_targeting_note()
    )


@prompt(
    description="Triage a host-wide incident from symptoms when you don't yet know which container is at fault.",
    domain="containers",
)
def triage_incident(window_minutes: int = 30) -> str:
    """
    Generate a symptom-first incident-triage plan that narrows from the whole host to a suspect.

    The on-call entry point: start from what just changed (`system_events`) and the current fleet state
    (`docker://containers`), narrow to the likely culprit, then hand off to `troubleshoot_container`.

    args: window_minutes - How far back to pull the daemon event log (default 30)
    returns: str - A prompt instructing the agent to correlate recent events with current state
    """
    return (
        f"Triage a docker incident on this host. Work from what changed in the last {window_minutes} "
        "minutes toward a single suspect. This is diagnosis — change nothing:\n"
        "1. Pull the recent daemon event log so you have a timeline of what broke and when. The "
        "`system_events` tool's `since` takes an absolute timestamp (a Unix epoch integer or an RFC3339 "
        f"string), not a relative duration — compute the timestamp for {window_minutes} minutes ago "
        f"and pass it: `system_events(since=<unix epoch for {window_minutes} minutes ago>, "
        'filters={"type": "container"}, limit=200)`. Scan for `die`, `oom`, `kill`, '
        "`health_status: unhealthy`, and tight `start`/`die` cycles (a crash loop).\n"
        "2. Read the MCP resource `docker://containers` for current state. Reconcile it with the event "
        "timeline: a container that has a recent `die` event and is now `restarting` or `exited` with a "
        "non-zero `exit_code` is the prime suspect.\n"
        "3. Separate cause from symptom. A host under memory or disk pressure takes down healthy "
        "containers too — read `docker-stats://{name}` for the running set to spot a resource hog, and "
        "call `system_df` if you suspect the daemon itself is out of disk. If many unrelated containers failed "
        "at once, suspect the host or daemon, not any one container.\n"
        "4. For the prime suspect, read `docker-logs://{name}` for the error that preceded the first "
        "`die` in the timeline.\n"
        "State, in two sentences, the most likely root cause and the blast radius (one container, or "
        "host-wide). Then hand off: name the container for a `troubleshoot_container` deep dive, or flag "
        "the host-level fix. Propose remediation but do not perform it." + _host_targeting_note()
    )


@prompt(
    description="Replace a running container with a new image while preserving its configuration.", domain="containers"
)
def migrate_container(container: str, new_image: str) -> str:
    """
    Generate a plan for swapping a container's image without losing its configuration.

    args: container - Existing container name or ID
    args: new_image - The new image reference to deploy
    returns: str - A prompt instructing the agent to perform a safe migration
    """
    return (
        f"Migrate container `{container}` to image `{new_image}` without losing its configuration, "
        f"keeping the old container as an instant rollback until the new one is proven healthy:\n"
        f"1. Use `container_inspect` to capture the current name, env vars, mounts, ports, network, and restart "
        f"policy. Show this captured config back to the user before changing anything.\n"
        f"2. Use `image_pull` to fetch `{new_image}`.\n"
        f"3. Use `container_stop` on `{container}`, then `container_rename` it to `{container}-old` "
        f"(instead of removing it) so the original name is free and the old container survives as a "
        f"rollback target.\n"
        f"4. Use `container_run` to start a new container under the original name `{container}` with the "
        f"captured config but the new image.\n"
        f'5. Call `container_wait(id_or_name="{container}", until="healthy")` to confirm the replacement is '
        f'healthy — `health: null` means no HEALTHCHECK is defined (not unhealthy); treat `status == "running"` '
        f'as success in that case. If it instead comes back `health == "unhealthy"`, or the container exited '
        f"before becoming healthy, roll back: `container_stop`/`container_remove` the new one and "
        f"`container_rename` `{container}-old` back to `{container}`, then `container_start`.\n"
        f"6. Only once the replacement is confirmed healthy, `container_remove` `{container}-old`. Ask "
        f"the user before this final removal — it discards the rollback path."
    )


@prompt(description="Reclaim disk space by pruning unused docker resources.")
def clean_environment(scope: str = "stopped") -> str:
    """
    Generate a plan for safely pruning unused docker resources.

    args: scope - "stopped" (default) for containers + dangling images, or "all" to also prune networks and volumes
    returns: str - A prompt instructing the agent to inventory and prune unused resources
    """
    base = (
        "Reclaim docker disk usage safely:\n"
        "1. Use `system_df` to capture current disk usage as a before snapshot. Note the `BuildCache` total — "
        "on a machine that builds images it is frequently the single largest reclaimable chunk, and "
        "neither `container_prune` nor `image_prune` touches it.\n"
        "2. Use `container_prune` to remove stopped containers.\n"
        "3. Use `image_prune` (without `filters={'dangling': False}`) to remove dangling images only.\n"
        "4. Use `buildx_prune` to reclaim BuildKit build cache. It always runs with `--force`; if the "
        "`system_df` from step 1 showed a large `BuildCache`, this is where most of the space comes back. "
        "Mention that an immediately-following rebuild will be slower with a cold cache.\n"
    )
    if scope == "all":
        base += (
            "5. Use `network_prune` to remove unused user-defined networks.\n"
            "6. Use `volume_prune` ONLY after explicitly confirming with the user — volumes can hold "
            "irreplaceable data.\n"
        )
    base += (
        "If the goal is to clean up only what was created through this server (rather than every unused "
        "resource), prefer `container_list(managed_only=True)` — or filter on the "
        "`docker-mcp-server.managed=true` label — to scope the inventory before removing anything.\n"
    )
    base += "Finish with `system_df` again and report the before/after delta and total space reclaimed."
    return base


@prompt(description="Tear down only the resources this MCP server created, leaving everything else untouched.")
def prune_managed(include_volumes: bool = False) -> str:
    """
    Generate a plan for removing only the resources stamped with this server's provenance label.

    Scopes every step to the `docker-mcp-server.managed=true` label, so nothing the agent (or anyone
    else) created outside this server is touched.

    args: include_volumes - Also remove managed volumes (data loss — defaults to False)
    returns: str - A prompt instructing the agent to inventory and remove only managed resources
    """
    base = (
        "Remove only the docker resources THIS server created — everything stamped with the "
        "`docker-mcp-server.managed=true` label — and leave all other resources alone:\n"
        "1. Inventory first, and show it before removing anything: `container_list(all=True, "
        "managed_only=True)`, `network_list(managed_only=True)`, `volume_list(managed_only=True)`, "
        "`service_list(managed_only=True)` (services only on a swarm manager). Always include volumes "
        "in the inventory even when not removing them, so the user can see what would be affected before "
        "confirming any volume prune. Report what you found as a table; if it's empty, stop and say so.\n"
        "2. Remove managed containers. `container_prune(filters={'label': "
        "'docker-mcp-server.managed=true'})` clears the *stopped* ones; a still-*running* managed "
        "container is left untouched by prune, so stop and remove those explicitly only after "
        "confirming with the user (`container_stop` then `container_remove`).\n"
        "3. Remove managed user-defined networks with `network_prune(filters={'label': "
        "'docker-mcp-server.managed=true'})`.\n"
    )
    if include_volumes:
        base += (
            "4. Remove managed volumes with `volume_prune(filters={'label': "
            "'docker-mcp-server.managed=true'})` — but ONLY after explicitly confirming with the user, "
            "since a volume may hold irreplaceable data even if this server created it.\n"
        )
    else:
        base += (
            "4. Do NOT remove volumes (include_volumes was not set). Mention that managed volumes were "
            "left in place and can be removed by re-running with include_volumes=True.\n"
        )
    base += (
        "Note: managed swarm services would need `service_remove` per service from the step-1 inventory "
        "(there is no service prune). Finish by re-running the step-1 inventory to confirm the managed "
        "footprint is gone, and report what was removed."
    )
    return base


@prompt(description="Inspect every docker resource that shares a label.")
def inspect_stack(label: str) -> str:
    """
    Generate a plan for inspecting all resources tagged with a given label.

    args: label - Label key or key=value pair to filter on (e.g. "com.example.app=web")
    returns: str - A prompt instructing the agent to enumerate containers, networks, and volumes by label
    """
    return (
        f"Enumerate every docker resource carrying the label `{label}`:\n"
        f"1. `container_list(all=True, filters={{'label': '{label}'}})` for containers.\n"
        f"2. `network_list(filters={{'label': '{label}'}})` for networks.\n"
        f"3. `volume_list(filters={{'label': '{label}'}})` for volumes.\n"
        f"Render the result as a single table grouped by resource type, with name, ID, and creation time. "
        f"Do not modify anything."
    )


@prompt(description="Plan a multi-container application from an informal description.", domain="compose")
def plan_compose_stack(description: str) -> str:
    """
    Generate a plan for translating an informal app description into docker resources.

    args: description - Free-form description of the app to deploy (e.g. "wordpress + mysql with a shared volume")
    returns: str - A prompt instructing the agent to design and deploy the stack with MCP tools
    """
    return (
        f"Design a multi-container deployment for: {description}\n\n"
        f"First, before calling any tool, produce a plan that lists:\n"
        f"- Each container (image, name, role, exposed ports)\n"
        f"- Networks (name, driver, which containers attach)\n"
        f"- Volumes (name, mount path inside each container)\n"
        f"- Any required env vars or secrets (use `secret_create` for swarm, env for plain containers)\n"
        f"- Startup order if dependencies exist\n\n"
        f"Wait for the user to approve the plan, then create the resources in dependency order using "
        f"`network_create`, `volume_create`, `image_pull`, and `container_run`. End with `container_list` "
        f"showing the running stack."
    )


@prompt(description="Bring up a Docker Compose project and verify it's healthy.", domain="compose")
def deploy_compose_project(project_dir: str, project_name: str | None = None) -> str:
    """
    Generate a plan for bringing up a compose project safely.

    args: project_dir - Filesystem path to the directory containing the compose file
    args: project_name - Optional compose project name override (defaults to the dir name)
    returns: str - A prompt instructing the agent to validate, deploy, and verify the project
    """
    name_clause = f" with project name `{project_name}`" if project_name else ""
    project_name_arg = f', project_name="{project_name}"' if project_name else ""
    return (
        f"Bring up the Docker Compose project at `{project_dir}`{name_clause}:\n"
        f'1. Call `compose_config(project_dir="{project_dir}"{project_name_arg}, format="json")` to render '
        f"the resolved config. Show the user which services, networks, and volumes will be created and ask for "
        f"approval before touching the daemon. Flag any service that declares `privileged: true`, host bind "
        f"mounts that escape the project, or env that looks like a secret.\n"
        f'2. After approval, call `compose_pull(project_dir="{project_dir}"{project_name_arg})` to fetch images '
        f"upfront so failures surface before containers start.\n"
        f'3. Call `compose_up(project_dir="{project_dir}"{project_name_arg}, wait=True)` so the call blocks '
        f"until services are healthy (or fails fast if a healthcheck is failing).\n"
        f"4. Verify with `compose_ps` that every service is in `running` state.\n"
        f"5. Tail recent logs with `compose_logs(tail=100)` and report anything that looks like an error.\n"
        f"Stop and ask before adding `--volumes` to a later `compose_down` — that removes persistent data."
    )


@prompt(description="Diagnose a misbehaving Docker Compose project.", domain="compose")
def troubleshoot_compose_project(project_dir: str, project_name: str | None = None) -> str:
    """
    Generate a diagnostic plan for a compose project that isn't behaving.

    args: project_dir - Filesystem path to the directory containing the compose file
    args: project_name - Optional compose project name override
    returns: str - A prompt instructing the agent to gather state and identify the root cause
    """
    project_name_arg = f', project_name="{project_name}"' if project_name else ""
    return (
        f"Diagnose the Docker Compose project at `{project_dir}`:\n"
        f'1. Call `compose_ps(project_dir="{project_dir}"{project_name_arg}, all=True)` to capture which '
        f"services are running, exited, or restarting. Note exit codes.\n"
        f"2. For every service that is not `running`, call `compose_logs(services=[<name>], tail=200, "
        f"timestamps=True)` and look for the last error line.\n"
        f'3. Call `compose_config(format="json")` to confirm the rendered config matches expectations '
        f"(healthcheck, depends_on, env, volumes).\n"
        f"4. If a service depends on another via `depends_on` with `condition: service_started`, check the "
        f"dependency's healthcheck state — a missing healthcheck means `started` only confirms the process "
        f"began, not that it's accepting connections.\n"
        f"Summarize the root cause in one paragraph and propose a fix (config edit, image change, network "
        f"adjustment) before changing anything."
    )


@prompt(
    description="Review this server's configured hosts and Docker contexts, and the daemon it targets.",
    domain="context",
)
def audit_docker_contexts() -> str:
    """
    Generate a plan for inventorying the host registry + CLI contexts and confirming the daemon target.

    returns: str - A prompt instructing the agent to report configured hosts, enumerate contexts, and confirm the target
    """
    return (
        "Audit what daemon(s) this server targets — its own host registry first, then the host's Docker contexts:\n"
        "1. Call `host_list` (or read `docker-mcp://hosts`) for the hosts configured via DOCKER_MCP_SERVER_HOSTS:\n"
        "   each `name`, resolved `url`, `read_only`/`tls` flags, and which is the `default` (used when a\n"
        "   tool's `host` is omitted). With a single host this is just the one resolved daemon. These URLs are\n"
        "   resolved (auto/local/context) and pinned at server startup — a later `docker context use` does not\n"
        "   move them; restart to re-resolve.\n"
        "2. Call `context_list` and present the table of contexts (name, current, daemon endpoint, description).\n"
        "3. Highlight which context is `Current=true`. That's the one the docker CLI uses by default, but note\n"
        "   it only affects a host configured as `auto`/`local` — an explicit URL or a multi-host registry pins\n"
        "   its own endpoint and ignores the ambient context.\n"
        "4. Call `system_info` (pass `host=<name>` to pick a host when several are configured) and report `Name`,\n"
        "   `ServerVersion`, and `OperatingSystem`. Compare against the host's resolved `url` / the current\n"
        "   context's `DockerEndpoint`.\n"
        "5. If the configured hosts or contexts point at different daemons, ask the user whether the intended\n"
        "   target is selected before any mutating operation."
    )


@prompt(description="Audit the health of a docker swarm: nodes, services, and task convergence.", domain="swarm")
def audit_swarm_health() -> str:
    """
    Generate a plan for assessing whether a swarm and its services are healthy.

    returns: str - A prompt instructing the agent to enumerate nodes/services and flag problems
    """
    return (
        "Audit the health of this docker swarm. Do not change anything — this is read-only:\n"
        "1. Call `node_list` and flag any node whose `Status.State` is not `ready` or whose "
        "`Spec.Availability` is `drain`/`pause`. Note manager nodes (`Spec.Role == manager`) and "
        "whether `ManagerStatus.Reachability` is `reachable` — an unreachable manager threatens "
        "quorum. Call out if you have an even number of managers or only one (no fault tolerance).\n"
        "2. Call `service_list`. For each service, compare desired vs running replicas: read the "
        "mode from `Spec.Mode` (Replicated count vs Global), then call `service_ps(id_or_name, "
        "filters={'desired-state': 'running'})` to drop tasks the orchestrator has already retired, "
        "and count how many of the returned tasks have `Status.State == 'running'`. The filter keys "
        "on *desired* state, so a returned task can still be failed/rejected — don't count it as "
        "running just because it matched.\n"
        "3. For any service that is under-replicated, call `service_ps` without the filter and "
        "look for tasks stuck in `rejected`/`failed`, or rapidly cycling through `shutdown` -> "
        "`starting` (a crash loop). Pull `Status.Err` and `Status.Message` off the failing task.\n"
        "4. For a service that is crash-looping, call `service_logs(id_or_name, tail=200, "
        "timestamps=True)` and surface the last error.\n"
        "5. If a node is drained and should be removed, note that `node_remove` (force only if it is "
        "still reachable) is the follow-up — but do not call it as part of this audit.\n"
        "Summarize as a table: nodes (state/availability/role/reachability) and services (desired vs "
        "running, status). End with a one-paragraph health verdict and the single most urgent fix. "
        "If anything looks mid-convergence (a node that just joined, a service you'd expect to still "
        "be scaling or rolling out), mention `node_wait` / `service_wait` as the way to block on it "
        "settling, rather than re-running this audit on a timer — but this audit itself stays read-only."
    )


@prompt(description="Find the latest tag for an image without pulling it.", domain="registry")
def find_latest_image_tag(image: str) -> str:
    """
    Generate a plan for picking the right tag of an image from a registry.

    args: image - Image reference, e.g. "alpine", "ghcr.io/org/repo"
    returns: str - A prompt instructing the agent to query the registry and recommend a tag
    """
    return (
        f"Find the most appropriate tag for `{image}` without pulling it:\n"
        f'1. Call `registry_tags(image="{image}", limit=200)` to enumerate available tags.\n'
        f"2. Filter out floating tags (`latest`, `edge`, `nightly`) and pre-release suffixes (`-rc`, `-beta`, "
        f"`-alpha`). Pick the highest stable semantic-version tag.\n"
        f'3. Call `registry_manifest(image="{image}", reference=<picked-tag>)` to confirm the tag '
        f"exists and capture the digest. If the response is an OCI image index, list the supported platforms.\n"
        f'4. Call `registry_image_config(image="{image}", reference=<picked-tag>)` to read the image config '
        f"without pulling — surface the labels (e.g. `org.opencontainers.image.source`/`revision`), the "
        f"exposed ports, entrypoint, and user so the user can vet what the tag actually contains. If step 3 "
        f"showed the tag is a multi-platform index, pass `platform=<one of the platforms it listed>` "
        f"(the default is `linux/amd64`, which errors if the image doesn't publish it).\n"
        f"5. If `{image}` is a Docker Hub image, also call `hub_repo_info` to surface star and pull counts so "
        f"the user can sanity-check the image's provenance. If you intend to pull afterwards, call "
        f"`hub_rate_limit` first to confirm there's pull budget left.\n"
        f"Report the recommended tag, its digest, the supported platforms, and key config/labels. Do not "
        f"pull the image."
    )


@prompt(description="Plan and run a multi-platform image build with buildx.", domain="buildx")
def plan_multiarch_build(image: str, platforms: str = "linux/amd64,linux/arm64", context: str = ".") -> str:
    """
    Generate a plan for building and pushing a multi-platform image with buildx.

    args: image - Target image reference, e.g. "ghcr.io/org/app:v1"
    args: platforms - Comma-separated platform list (default "linux/amd64,linux/arm64")
    args: context - Build context path (default ".")
    returns: str - A prompt instructing the agent to plan, build, and verify a multi-arch image
    """
    platforms_list = ", ".join(f'"{p.strip()}"' for p in platforms.split(",") if p.strip())
    return (
        f"Build and push `{image}` for multiple platforms ({platforms}) using buildx:\n"
        f"1. Call `buildx_list` and confirm a non-`docker` driver is active (the default `docker` driver "
        f"cannot do multi-platform; you need `docker-container` or another buildx driver). If only `docker` "
        f"is available, call `buildx_create(name='multi', driver='docker-container', use=True, bootstrap=True)`.\n"
        f'2. Call `buildx_imagetools_inspect(image="<base-image>", raw=True)` on each `FROM` reference to '
        f"confirm every base image actually publishes the requested platforms — multi-arch builds silently "
        f"fall back to slow QEMU emulation when a platform is missing.\n"
        f'3. Call `buildx_build(context="{context}", tags=["{image}"], platforms=[{platforms_list}], '
        f'push=True, provenance="mode=max", sbom="true")` to build, attest, and push in one step. The '
        f"`--load` flag cannot be combined with multi-platform; results live only in the registry.\n"
        f'4. After the build, call `buildx_imagetools_inspect(image="{image}", raw=True)` and confirm the '
        f"published manifest list contains every requested platform.\n"
        f"Surface any platform that was skipped or built via emulation before declaring success."
    )


@prompt(description="Audit an image's CVE posture with Docker Scout.", domain="scout")
def audit_image_cves(image: str) -> str:
    """
    Generate a plan for walking through Scout's CVE reporting for an image.

    args: image - Image reference to scan
    returns: str - A prompt instructing the agent to scan, prioritize, and report
    """
    return (
        f"Audit `{image}` for known vulnerabilities using Docker Scout:\n"
        f'1. Call `scout_quickview(image="{image}")` first to get a one-screen summary of total CVE counts '
        f"by severity. Stop here if everything is `0` and the user just needs reassurance.\n"
        f'2. Call `scout_cves(image="{image}", only_severity=["critical", "high"], only_fixed=True)` to '
        f"list actionable CVEs (high+critical with a fix available). Ignore lower-severity findings unless "
        f"the user asks for them.\n"
        f'3. Call `scout_cves(image="{image}", only_severity=["critical", "high"], ignore_base=True)` to '
        f"separate CVEs introduced by the application image from those inherited from the base. CVEs that "
        f"only appear in the unfiltered call are base-image issues — the right fix is a base bump, not a "
        f"package patch.\n"
        f"4. For each remaining CVE, report the package, installed version, fixed version, and CVE ID. "
        f"Recommend the smallest patch that addresses the high-priority findings.\n"
        f"Note: Scout's most useful data requires `docker login` on the host running this MCP server. If the "
        f"output looks sparse, ask the user whether the host is authenticated."
    )


@prompt(description="Compare two image versions and report the CVE delta.", domain="scout")
def compare_image_versions(old_image: str, new_image: str) -> str:
    """
    Generate a plan for comparing two image references via Scout.

    args: old_image - The baseline image reference
    args: new_image - The candidate image reference
    returns: str - A prompt instructing the agent to compare and report
    """
    return (
        f"Compare `{old_image}` against `{new_image}` and report the security delta:\n"
        f'1. Call `scout_compare(image="{new_image}", to="{old_image}", ignore_unchanged=True, '
        f'only_severity=["critical", "high"])` to get the CVE diff filtered to actionable severities.\n'
        f"2. Categorize the diff into:\n"
        f"   - Resolved CVEs (present in old, absent in new)\n"
        f"   - New CVEs (absent in old, present in new) — these are regressions worth flagging\n"
        f"   - Carried-forward CVEs (unchanged)\n"
        f"3. If there are new high/critical CVEs in the candidate, recommend whether to proceed, hold, "
        f"or wait for a base-image refresh. Use `scout_recommendations` to check whether a different "
        f"base tag would resolve them.\n"
        f"Render the result as a short table; stop and ask before any rebuild or rollback."
    )


@prompt(description="Recommend a safer base image via Docker Scout.", domain="scout")
def recommend_base_image(image: str) -> str:
    """
    Generate a plan for picking a better base image using Scout.

    args: image - Image reference whose base should be reviewed
    returns: str - A prompt instructing the agent to fetch and present recommendations
    """
    return (
        f"Recommend a safer base image for `{image}`:\n"
        f'1. Call `scout_recommendations(image="{image}")` to fetch Scout\'s base-image suggestions. '
        f"Distinguish `refresh` recommendations (same major/minor, newer patches) from `update` "
        f"recommendations (a different major/minor release).\n"
        f'2. For each viable candidate base, call `scout_compare(image=<candidate>, to="{image}", '
        f'only_severity=["critical", "high"])` to confirm it actually resolves more CVEs than it '
        f"introduces. A refresh that fixes 3 highs and introduces 4 is not progress.\n"
        f"3. Verify the candidate exists on the registry and supports the platforms you build for: call "
        f"`buildx_imagetools_inspect(image=<candidate>, raw=True)` (which accepts a full ref like "
        f"`python:3.13-slim`) and check the platforms list in the returned manifest. Avoid "
        f"`registry_manifest` here — its `image` argument strips any `:tag`/`@digest`, so a full "
        f"candidate ref would need to be split into separate `image` and `reference` arguments.\n"
        f"Report the recommended base, the CVEs it resolves, the CVEs it introduces (if any), and the "
        f"single-line Dockerfile change required. Do not modify any Dockerfile."
    )


@prompt(description="Inspect a multi-arch manifest list / OCI image index without pulling.", domain="buildx")
def inspect_multiarch_manifest(image: str) -> str:
    """
    Generate a plan for inspecting an image's manifest list.

    Use this when reaching for `docker manifest inspect` — that command is in maintenance mode
    and lacks support for OCI image indexes and attestations. `buildx_imagetools_inspect` is
    the path forward.

    args: image - Image reference (tag or digest), e.g. "alpine:3.19"
    returns: str - A prompt instructing the agent to inspect and interpret the manifest
    """
    return (
        f"Inspect the manifest for `{image}` without pulling it:\n"
        f'1. Call `buildx_imagetools_inspect(image="{image}", raw=True)` to fetch the raw manifest JSON. '
        f"This replaces `docker manifest inspect` and handles both single-platform manifests and "
        f"multi-platform manifest lists / OCI image indexes.\n"
        f"2. Identify the response shape:\n"
        f"   - `application/vnd.oci.image.manifest.v1+json` or `…/docker.distribution.manifest.v2+json` "
        f"=> single-platform image; report the architecture, OS, and layer count.\n"
        f"   - `application/vnd.oci.image.index.v1+json` or `…/docker.distribution.manifest.list.v2+json` "
        f"=> multi-platform index; report each entry's platform and digest.\n"
        f"3. If the index also lists `attestation-manifest` entries (provenance / SBOM), call "
        f"`buildx_imagetools_inspect` again on each attestation digest to surface those payloads.\n"
        f"Render the result as a single table; do not pull or modify the image."
    )


@prompt(description="Create a multi-arch manifest list from existing per-platform tags.", domain="buildx")
def create_multiarch_manifest(target_tag: str, source_tags: str) -> str:
    """
    Generate a plan for stitching per-platform tags into a manifest list.

    Use this when reaching for `docker manifest create` + `docker manifest push` —
    `buildx_imagetools_create` does both in one step and handles OCI image indexes.

    args: target_tag - The new combined tag, e.g. "org/app:v1"
    args: source_tags - Comma-separated source tags (each must already be pushed),
                             e.g. "org/app:v1-amd64,org/app:v1-arm64"
    returns: str - A prompt instructing the agent to create and verify the manifest list
    """
    source_list = ", ".join(f'"{s.strip()}"' for s in source_tags.split(",") if s.strip())
    return (
        f"Create the manifest list `{target_tag}` from {source_tags}:\n"
        f"1. Confirm each source tag is already pushed to the registry by calling "
        f"`buildx_imagetools_inspect` on each one — `imagetools create` only stitches; it cannot upload "
        f"missing image layers.\n"
        f'2. Call `buildx_imagetools_create(target="{target_tag}", sources=[{source_list}], dry_run=True)` '
        f"first to print the resulting manifest without pushing. Show the user which platforms will be "
        f"published under the combined tag.\n"
        f"3. After the user approves, repeat without `dry_run` to actually push. This replaces the "
        f"`docker manifest create && docker manifest push` pair in one operation.\n"
        f'4. Verify with `buildx_imagetools_inspect(image="{target_tag}", raw=True)` that the published '
        f"index contains every expected platform.\n"
        f"Report the digest of the combined manifest at the end."
    )


@prompt(description="Translate `docker manifest …` commands into buildx imagetools equivalents.", domain="buildx")
def migrate_from_docker_manifest() -> str:
    """
    Generate a reference table mapping each `docker manifest` subcommand to its
    buildx imagetools replacement. The standalone `docker manifest` command is in
    maintenance mode and lacks support for OCI image indexes, attestations, and
    annotations.

    returns: str - A prompt the agent can hand to the user as a migration cheat-sheet
    """
    return (
        "`docker manifest` is in maintenance mode. Use `buildx imagetools` for new work — it supports OCI "
        "image indexes, attestations, and richer annotations.\n\n"
        "Mapping:\n\n"
        "| `docker manifest …`                  | This MCP server                          |\n"
        "|--------------------------------------|------------------------------------------|\n"
        "| `inspect REF`                        | `buildx_imagetools_inspect(image=REF)`   |\n"
        "| `inspect --verbose REF`              | `buildx_imagetools_inspect(image=REF, raw=True)` |\n"
        "| `create NEW SRC…` + `push NEW`       | "
        "`buildx_imagetools_create(target=NEW, sources=[SRC…])` (push is implicit) |\n"
        "| `create --amend NEW SRC…`            | "
        "`buildx_imagetools_create(target=NEW, sources=[SRC…], append=True)` |\n"
        "| `annotate NEW SRC --os/--arch/--variant` | "
        "`buildx_imagetools_create(target=NEW, sources=[SRC…], annotations=[…])` (re-create from sources) |\n"
        "| `push NEW`                           | Not needed — `buildx_imagetools_create` pushes |\n"
        "| `rm NEW`                             | Not needed — `buildx_imagetools_create` overwrites |\n"
        "\nWhen in doubt, run `buildx_imagetools_inspect(image=REF, raw=True)` first to see the current shape."
    )


@prompt(description="Review a Dockerfile for security, correctness, and cache-efficiency issues.")
def review_dockerfile(dockerfile_path: str) -> str:
    """
    Generate a plan for reviewing a Dockerfile against Docker's reference and best practices.

    args: dockerfile_path - Filesystem path to the Dockerfile to review
    returns: str - A prompt instructing the agent to read the Dockerfile and the authoritative docs, then critique it
    """
    return (
        f"Review the Dockerfile at `{dockerfile_path}`. First read the authoritative docs so the review "
        f"reflects current guidance, not memory: read the MCP resources `docker-docs://dockerfile` "
        f"(instruction reference) and `docker-docs://build-best-practices`. Then read the Dockerfile and "
        f"check for:\n"
        f"1. Unpinned/floating base images (`FROM image:latest` or no tag) — recommend a specific tag or "
        f"a digest pin for reproducibility.\n"
        f"2. No `USER` directive (the image runs as root) — recommend creating and switching to a "
        f"non-root user.\n"
        f"3. Secrets baked into layers: credentials in `ENV`/`ARG`, `COPY`'d private keys, or tokens on "
        f"`RUN` lines. These persist in the image history even if later removed — flag every one.\n"
        f"4. Missing `HEALTHCHECK` for a long-running service image.\n"
        f"5. Cache-inefficient layer order — e.g. `COPY . .` before installing dependencies, so every "
        f"source change busts the dependency layer. Dependency manifests should be copied and installed "
        f"before the rest of the source.\n"
        f"6. `ADD` where `COPY` would do, `apt-get install` without `--no-install-recommends` or without "
        f"cleaning the apt lists in the same layer, and missing `.dockerignore` consequences.\n"
        f"Report findings grouped by severity (security first), each with the offending line and the "
        f"concrete fix. Do not modify the Dockerfile — propose the diff and let the user apply it."
    )


@prompt(
    description="Audit running containers for risky runtime configuration (privilege, host access).",
    domain="containers",
)
def audit_container_security() -> str:
    """
    Generate a plan for sweeping running containers for dangerous runtime settings.

    returns: str - A prompt instructing the agent to inspect each container's HostConfig and flag risks
    """
    return (
        "Audit the security posture of running containers. This is read-only — do not change anything. "
        "For background on why these settings matter, the MCP resource `docker-docs://engine-security` "
        "covers the daemon's trust model.\n"
        "1. Call `container_list` (running only) to get the set to audit.\n"
        "2. For each container, call `container_inspect` and inspect its `HostConfig` / `Config`, flagging:\n"
        "   - `Privileged: true` — the container can do almost anything the host can; the highest-"
        "severity finding.\n"
        "   - A bind mount of the Docker socket (`/var/run/docker.sock`) — equivalent to root on the "
        "host, since the container can drive the daemon.\n"
        "   - Host namespaces: `NetworkMode: host`, `PidMode: host`, `IpcMode: host` — these remove "
        "isolation from the host.\n"
        "   - Added capabilities (`CapAdd`), especially `SYS_ADMIN`/`NET_ADMIN`/`SYS_PTRACE`, and "
        "`SecurityOpt` entries that disable seccomp/apparmor (`seccomp=unconfined`).\n"
        "   - Writable bind mounts of sensitive host paths (`/`, `/etc`, `/var/run`, the user's home).\n"
        "   - Running as root: no `User` set in `Config` (note this is best-effort — the image's "
        "default user isn't always visible from inspect).\n"
        "   - No resource limits: `Memory` and `NanoCpus` of 0 mean the container can exhaust the host.\n"
        "Render a table: container name, each risk found, severity. Summarize the most exposed container "
        "and the single highest-priority remediation. Recommend, but do not perform, any changes."
    )


@prompt(description="Diagnose why one container cannot reach another over the network.", domain="networks")
def debug_container_networking(source: str, target: str) -> str:
    """
    Generate a plan for diagnosing container-to-container connectivity.

    args:
        source - The container that cannot connect (name or ID)
        target - The container it is trying to reach (name or ID)
    returns: str - A prompt instructing the agent to compare networks and test connectivity
    """
    return (
        f"Diagnose why container `{source}` cannot reach `{target}`. Work from the most common cause "
        f"(not on a shared network) outward:\n"
        f"1. Call `container_inspect` on both and compare `NetworkSettings.Networks`. If they share no "
        f"user-defined network, that is almost certainly the problem — containers on the default bridge "
        f"cannot resolve each other by name; they must share a user-defined network. Recommend "
        f"`network_connect` to attach them to a common one.\n"
        f"2. If they DO share a network, note the DNS alias `{target}` should resolve to (the service/"
        f"container name or a network alias on that shared network).\n"
        f"3. From inside `{source}`, use `container_exec` to test, preferring an exec-form argv: "
        f"resolve DNS (e.g. `['getent', 'hosts', '{target}']`) and test the port "
        f"(`['nc', '-z', '-w', '2', '{target}', '<port>']` if `nc` exists). Distinguish a DNS failure "
        f"(name doesn't resolve) from a connection failure (resolves but refused/timed out).\n"
        f"4. If DNS resolves but the connection is refused, check that `{target}` actually listens on "
        f"that port and on `0.0.0.0` rather than `127.0.0.1` — use `container_inspect` for its exposed "
        f"ports and `container_logs` to confirm the service started.\n"
        f"5. Distinguish container-to-container reachability from host-published ports: a missing "
        f"`-p`/`ports` mapping only affects access from the host, not between containers on a shared "
        f"network.\n"
        f"State the root cause in one sentence and the concrete fix; do not change anything without "
        f"showing it first."
    )


@prompt(description="Investigate what is consuming docker disk space before pruning.")
def investigate_disk_usage() -> str:
    """
    Generate a plan for attributing docker disk usage to a cause before reclaiming it.

    returns: str - A prompt instructing the agent to break down usage across images, containers, volumes, and cache
    """
    return (
        "Find out WHAT is consuming docker disk space before reclaiming any of it — this is read-only "
        "diagnosis, not cleanup:\n"
        "1. Call `system_df` for the top-line split across Images, Containers, Local Volumes, and Build Cache. "
        "Identify which bucket dominates — the fix differs for each.\n"
        "2. If Images dominate: call `image_list` and sort by size. For the largest, call "
        "`image_history` to see which layers are heavy (a fat `COPY`, an un-cleaned package cache) and "
        "whether several images share base layers (so the on-disk cost is less than the sum of sizes).\n"
        "3. If Build Cache dominates: call `buildx_du` for the cache breakdown. This is reclaimable with "
        "`buildx_prune` and is invisible to `image_prune`.\n"
        "4. If Local Volumes dominate: call `volume_list` and cross-reference with `container_list"
        "(all=True)` to spot dangling volumes no container references — but do NOT assume a dangling "
        "volume is junk; it may hold data whose container is gone.\n"
        "5. If Containers dominate: look for stopped containers with large writable layers (`container_"
        "diff` shows what a container wrote on top of its image).\n"
        "Report a breakdown with the dominant cause, the specific offenders, and what each would reclaim "
        "— then point at the `clean_environment` prompt for the actual pruning. Recommend nothing "
        "destructive here."
    )


@prompt(description="Back up a named volume's contents to a tar file on the server host.", domain="volumes")
def backup_volume(volume: str, dest_path: str) -> str:
    """
    Generate a plan for backing up a named volume using a throwaway container.

    args:
        volume - The named volume to back up
        dest_path - Host path (on the server) to write the tar archive to
    returns: str - A prompt instructing the agent to tar the volume out via a helper container
    """
    return (
        f"Back up the contents of volume `{volume}` to `{dest_path}` on the server host. Docker has no "
        f"native volume-export API, so mount the volume into a throwaway container and pull its "
        f"filesystem out through the Docker archive API:\n"
        f'1. Confirm the volume exists with `volume_inspect("{volume}")`.\n'
        f"2. Quiesce writers if integrity matters: if a running container has `{volume}` mounted and is "
        f"writing to it, a hot copy can be inconsistent — note which containers use it (cross-reference "
        f"`container_list`) and offer to `container_stop` them first, or warn that the backup is "
        f"crash-consistent only.\n"
        f"3. Create a helper container with the volume mounted at `/data`, e.g. `container_create` from "
        f"`alpine` with `{volume}` mounted at `/data`. It does not need to run — the archive API reads "
        f"the volume through the mount whether or not the container is started; no `tar` binary in the "
        f"image is required.\n"
        f'4. Call `container_archive_get_to_file(<helper>, path="/data", dest_path="{dest_path}")` to '
        f"write the volume contents as a tar to `{dest_path}` (a path on the host running this MCP "
        f"server, written as the server's user). The archive is rooted at `data/` (the API names the "
        f"tar after the path's last component) — `restore_volume` relies on that, so don't repackage it.\n"
        f"5. Remove the helper container with `container_remove`, and restart anything you stopped in "
        f"step 2.\n"
        f"Report the archive path and size. `restore_volume` is the exact inverse."
    )


@prompt(description="Restore a named volume's contents from a tar file on the server host.", domain="volumes")
def restore_volume(volume: str, source_path: str) -> str:
    """
    Generate a plan for restoring a named volume from a tar archive using a throwaway container.

    args:
        volume - The named volume to restore into
        source_path - Host path (on the server) to read the tar archive from
    returns: str - A prompt instructing the agent to untar an archive into a volume via a helper container
    """
    return (
        f"Restore the contents of `{source_path}` into volume `{volume}`. This is the inverse of "
        f"`backup_volume` and is destructive to whatever `{volume}` currently holds — confirm with the "
        f"user before overwriting:\n"
        f"1. Check whether `{volume}` already exists with `volume_inspect`. There is no way to tell whether "
        f"a volume holds data without mounting it, so if the volume already exists, STOP and confirm the "
        f'overwrite regardless. If it does not exist, `volume_create("{volume}")`.\n'
        f"2. Ensure no running container is using `{volume}` — restoring underneath a live writer "
        f"corrupts state. Use `container_list` to check and offer to `container_stop` them first.\n"
        f"3. Create AND start a helper container from `alpine` with `{volume}` mounted read-write at "
        f'`/data` (e.g. command `["sleep", "3600"]` so it stays up for the exec).\n'
        f'4. Clear stale files first with `container_exec(<helper>, ["sh", "-c", "rm -rf /data/* '
        f'/data/.[!.]* /data/..?* 2>/dev/null || true"])` — otherwise files not present in the archive '
        f"survive the restore.\n"
        f'5. Call `container_archive_put(<helper>, path="/", from_file="{source_path}")`. Use '
        f'`path="/"`, not `/data`: a `backup_volume` archive is rooted at `data/`, so extracting it at '
        f"the root lands the contents back in `/data` (extracting at `/data` would nest them in "
        f"`/data/data`). `{source_path}` is read from the host running this MCP server.\n"
        f"6. Remove the helper with `container_remove` and restart anything you stopped.\n"
        f'Verify with `container_exec(<helper>, ["ls", "/data"])` (before removing it) or a quick '
        f"`alpine ls` helper, confirming the expected files are present. Report what was restored."
    )


@prompt(description="Deploy a Compose file to a swarm as a stack and verify the rollout.", domain="stack")
def deploy_swarm_stack(stack_name: str, compose_file: str) -> str:
    """
    Generate a plan for deploying a Compose file to a swarm as a stack and confirming it converged.

    args:
        stack_name - The stack name to create or update
        compose_file - Path to the Compose file to deploy
    returns: str - A prompt instructing the agent to validate, deploy, and verify the stack
    """
    return (
        f"Deploy `{compose_file}` to the swarm as stack `{stack_name}` and verify it converges:\n"
        f"1. Confirm the daemon is a swarm manager before anything else — call `system_info` and check "
        f"`Swarm.LocalNodeState == 'active'` and `Swarm.ControlAvailable == true`. `docker stack` only "
        f"works against a manager; if it isn't one, stop and tell the user (init with `swarm_init` or "
        f"point `DOCKER_HOST` at a manager).\n"
        f'2. Sanity-check the Compose file first: call `compose_config(files=["{compose_file}"], '
        f'format="json")` to render it and surface what will be created. Flag anything risky — services '
        f"with `privileged: true`, host bind mounts, or env that looks like a secret (prefer swarm "
        f"`secrets`/`configs`). Note that some Compose keys are ignored by the swarm orchestrator "
        f"(e.g. `depends_on`, `build`) — call those out.\n"
        f'3. Deploy with `stack_deploy(name="{stack_name}", compose_files=["{compose_file}"])`. Add '
        f"`with_registry_auth=True` if any image is private, and `prune=True` only if the user wants "
        f"services removed when they leave the Compose file. Check the returned `returncode`/`stderr`.\n"
        f'4. Verify convergence: call `stack_services(name="{stack_name}")` to get each service\'s full '
        f'name (`{stack_name}_<service>`), then `service_wait(id_or_name=<full-name>, until="running")` '
        f"per service to block until it converges instead of polling by hand. For any that comes back "
        f"`met=False`/`timed_out=True`, its `failed_tasks` already lists the stuck task ids/errors — or "
        f'call `stack_ps(name="{stack_name}", filters=["desired-state=running"])` for the same view.\n'
        f"5. Re-running `stack_deploy` with the same name updates the stack in place, so iterate on the "
        f"Compose file and redeploy rather than removing first. Mention `stack_remove` as the teardown, but "
        f"do not call it.\n"
        f"Report the per-service desired-vs-running replica counts and any task errors."
    )


@prompt(
    description="Survey every configured Docker host read-only and explain how to drive multi-host tools.",
    domain=None,
    multi_host=True,
)
def survey_hosts() -> str:
    """
    Generate a read-only sweep across every configured Docker host.

    Only registered when 2+ hosts are configured (DOCKER_MCP_SERVER_HOSTS); the cross-host entry point
    for "what's running where", and the place that explains the multi-host tool model.

    returns: str - A prompt instructing the agent to enumerate hosts and sweep each one read-only
    """
    return (
        "Survey every Docker host this server is configured for. Change nothing on any host:\n"
        "1. Read `docker-mcp://hosts` (or call `host_list`) for the configured hosts: each `name`, "
        "resolved `url`, `read_only`, `tls`, and which is the `default`.\n"
        "2. For each host, ping it and read `system_info` with `host=<name>`: report reachability, ServerVersion, "
        "OperatingSystem, and container/image counts. A host may be unreachable — note it and move on; the "
        "others are independent.\n"
        "3. For each reachable host, read its container index — the default host's is `docker:///containers`, "
        "a named host's is `docker://{host}/containers` — and summarize running vs stopped, flagging any "
        "`exited` with a non-zero `exit_code` or `restarting`.\n"
        "4. Render one table across all hosts: host, reachable?, version, #running, #stopped, #problems, "
        "read-only?.\n"
        "Driving multi-host tools: read-only tools take `host=<name>` (omit to use the default — the first "
        "configured); mutating/destructive tools REQUIRE an explicit `host`; a host marked read-only `(ro)` "
        "rejects every write. End with a one-line health verdict per host and name the one most worth "
        "attention. Recommend nothing destructive."
    )
