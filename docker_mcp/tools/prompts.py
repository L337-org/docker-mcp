# library of mcp prompt templates that guide the agent through common docker workflows

from docker_mcp.server import mcp


@mcp.prompt(description="Read the Docker SDK for Python documentation for a section before writing code that uses it.")
def lookup_docker_docs(section: str) -> str:
    """
    Ask the agent to consult the Docker SDK for Python documentation for a specific section.

    args: section: str - SDK section name (e.g. "containers", "images", "swarm")
    returns: str - A prompt instructing the agent to read the docker-docs resource and summarize the API
    """
    return (
        f"Read the MCP resource `docker-docs://{section}` and summarize the public methods, their signatures, "
        f"and return types. Highlight anything that is easy to misuse (parameters that look similar, surprising "
        f"defaults, methods that return iterators vs. lists). Do not assume any method exists unless it is "
        f"present in that resource."
    )


@mcp.prompt(description="Verify that a specific Docker SDK method exists before relying on it.")
def verify_docker_method(method: str, section: str) -> str:
    """
    Ask the agent to verify a method of the `docker` module against the live SDK docs.

    args: method: str - The method name to verify (e.g. "containers.run")
    args: section: str - The SDK section to check (e.g. "containers")
    returns: str - A prompt instructing the agent to confirm the method's signature from the docs
    """
    return (
        f"Read the MCP resource `docker-docs://{section}` and confirm whether `{method}` exists. "
        f"If it does, quote its full signature, list each parameter with its type, and describe the return value. "
        f"If it does not exist, say so explicitly and suggest the closest documented alternative."
    )


@mcp.prompt(description="Deploy a containerized application end-to-end: image, network, volume, container.")
def deploy_container(image: str, name: str) -> str:
    """
    Generate a step-by-step plan for deploying a container with supporting resources.

    args: image: str - The image reference to deploy (e.g. "nginx:1.27")
    args: name: str - The container name to assign
    returns: str - A prompt instructing the agent to walk through deployment using the MCP tools
    """
    return (
        f"Deploy the image `{image}` as a container named `{name}` using the docker MCP tools. Follow this order:\n"
        f"1. Call `pull_image` to ensure the image is present locally.\n"
        f"2. Decide whether the workload needs a dedicated network or named volume; create them with "
        f"`create_network` / `create_volume` if so.\n"
        f"3. Call `run_container` with sensible defaults: `detach=True`, a restart policy, and any port or volume "
        f"mappings the image requires.\n"
        f"4. Verify the container reached the running state with `list_containers` and `container_logs`.\n"
        f"Report the final container ID and any resources you created. Stop and ask before destroying existing "
        f"resources that share the same name."
    )


@mcp.prompt(description="Troubleshoot a misbehaving container by gathering logs, state, and stats.")
def troubleshoot_container(container: str) -> str:
    """
    Generate a diagnostic plan for an unhealthy or failing container.

    args: container: str - Container name or ID to investigate
    returns: str - A prompt instructing the agent to gather logs, inspect state, and propose a fix
    """
    return (
        f"Diagnose what is wrong with container `{container}`:\n"
        f"1. Use `get_container` to read its current state, exit code, and restart count.\n"
        f"2. Use `container_logs` (with `tail=200`) to capture recent stdout/stderr.\n"
        f"3. If the container is running, use `container_stats` for CPU/memory pressure and `container_top` "
        f"for the process tree.\n"
        f"4. If a config file or process check is needed, use `exec_in_container`.\n"
        f"Summarize the root cause in one paragraph, then propose a concrete fix (config change, image bump, "
        f"resource limit) before making any changes."
    )


@mcp.prompt(description="Replace a running container with a new image while preserving its configuration.")
def migrate_container(container: str, new_image: str) -> str:
    """
    Generate a plan for swapping a container's image without losing its configuration.

    args: container: str - Existing container name or ID
    args: new_image: str - The new image reference to deploy
    returns: str - A prompt instructing the agent to perform a safe migration
    """
    return (
        f"Migrate container `{container}` to image `{new_image}` without losing its configuration, "
        f"keeping the old container as an instant rollback until the new one is proven healthy:\n"
        f"1. Use `get_container` to capture the current name, env vars, mounts, ports, network, and restart "
        f"policy. Show this captured config back to the user before changing anything.\n"
        f"2. Use `pull_image` to fetch `{new_image}`.\n"
        f"3. Use `stop_container` on `{container}`, then `rename_container` it to `{container}-old` "
        f"(instead of removing it) so the original name is free and the old container survives as a "
        f"rollback target.\n"
        f"4. Use `run_container` to start a new container under the original name `{container}` with the "
        f"captured config but the new image.\n"
        f"5. Verify with `list_containers` and `container_logs` that the replacement is healthy. If it is "
        f"NOT, roll back: `stop_container`/`remove_container` the new one and `rename_container` "
        f"`{container}-old` back to `{container}`, then `start_container`.\n"
        f"6. Only once the replacement is confirmed healthy, `remove_container` `{container}-old`. Ask "
        f"the user before this final removal — it discards the rollback path."
    )


@mcp.prompt(description="Reclaim disk space by pruning unused docker resources.")
def clean_environment(scope: str = "stopped") -> str:
    """
    Generate a plan for safely pruning unused docker resources.

    args: scope: str - "stopped" (default) for containers + dangling images, or "all" to also prune networks and volumes
    returns: str - A prompt instructing the agent to inventory and prune unused resources
    """
    base = (
        "Reclaim docker disk usage safely:\n"
        "1. Use `df` to capture current disk usage as a before snapshot. Note the `BuildCache` total — "
        "on a machine that builds images it is frequently the single largest reclaimable chunk, and "
        "neither `prune_containers` nor `prune_images` touches it.\n"
        "2. Use `prune_containers` to remove stopped containers.\n"
        "3. Use `prune_images` (without `filters={'dangling': False}`) to remove dangling images only.\n"
        "4. Use `buildx_prune` to reclaim BuildKit build cache. It always runs with `--force`; if the "
        "`df` from step 1 showed a large `BuildCache`, this is where most of the space comes back. "
        "Mention that an immediately-following rebuild will be slower with a cold cache.\n"
    )
    if scope == "all":
        base += (
            "5. Use `prune_networks` to remove unused user-defined networks.\n"
            "6. Use `prune_volumes` ONLY after explicitly confirming with the user — volumes can hold "
            "irreplaceable data.\n"
        )
    base += "Finish with `df` again and report the before/after delta and total space reclaimed."
    return base


@mcp.prompt(description="Inspect every docker resource that shares a label.")
def inspect_stack(label: str) -> str:
    """
    Generate a plan for inspecting all resources tagged with a given label.

    args: label: str - Label key or key=value pair to filter on (e.g. "com.example.app=web")
    returns: str - A prompt instructing the agent to enumerate containers, networks, and volumes by label
    """
    return (
        f"Enumerate every docker resource carrying the label `{label}`:\n"
        f"1. `list_containers(all=True, filters={{'label': '{label}'}})` for containers.\n"
        f"2. `list_networks(filters={{'label': '{label}'}})` for networks.\n"
        f"3. `list_volumes(filters={{'label': '{label}'}})` for volumes.\n"
        f"Render the result as a single table grouped by resource type, with name, ID, and creation time. "
        f"Do not modify anything."
    )


@mcp.prompt(description="Plan a multi-container application from an informal description.")
def plan_compose_stack(description: str) -> str:
    """
    Generate a plan for translating an informal app description into docker resources.

    args: description: str - Free-form description of the app to deploy (e.g. "wordpress + mysql with a shared volume")
    returns: str - A prompt instructing the agent to design and deploy the stack with MCP tools
    """
    return (
        f"Design a multi-container deployment for: {description}\n\n"
        f"First, before calling any tool, produce a plan that lists:\n"
        f"- Each container (image, name, role, exposed ports)\n"
        f"- Networks (name, driver, which containers attach)\n"
        f"- Volumes (name, mount path inside each container)\n"
        f"- Any required env vars or secrets (use `create_secret` for swarm, env for plain containers)\n"
        f"- Startup order if dependencies exist\n\n"
        f"Wait for the user to approve the plan, then create the resources in dependency order using "
        f"`create_network`, `create_volume`, `pull_image`, and `run_container`. End with `list_containers` "
        f"showing the running stack."
    )


@mcp.prompt(description="Bring up a Docker Compose project and verify it's healthy.")
def deploy_compose_project(project_dir: str, project_name: str | None = None) -> str:
    """
    Generate a plan for bringing up a compose project safely.

    args: project_dir: str - Filesystem path to the directory containing the compose file
    args: project_name: str - Optional compose project name override (defaults to the dir name)
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


@mcp.prompt(description="Diagnose a misbehaving Docker Compose project.")
def troubleshoot_compose_project(project_dir: str, project_name: str | None = None) -> str:
    """
    Generate a diagnostic plan for a compose project that isn't behaving.

    args: project_dir: str - Filesystem path to the directory containing the compose file
    args: project_name: str - Optional compose project name override
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


@mcp.prompt(description="Review available Docker contexts and the one this MCP server is targeting.")
def audit_docker_contexts() -> str:
    """
    Generate a plan for inventorying contexts and confirming the daemon target.

    returns: str - A prompt instructing the agent to enumerate contexts and confirm the active target
    """
    return (
        "Audit the Docker context configuration on this host:\n"
        "1. Call `context_ls` and present the table of contexts (name, current, daemon endpoint, description).\n"
        "2. Highlight which context is `Current=true`. That's the one the docker CLI uses, but note that the\n"
        "   long-lived docker-py client behind SDK-backed tools (e.g. `list_containers`) was bound at server\n"
        "   startup — it does not retarget when `context_use` is called later.\n"
        "3. Call `info` and report `Name`, `ServerVersion`, and `OperatingSystem`. Compare against the\n"
        "   `DockerEndpoint` of the current context.\n"
        "4. If multiple contexts point at different hosts, ask the user whether the active one is the\n"
        "   intended target before any mutating operation."
    )


@mcp.prompt(description="Audit the health of a docker swarm: nodes, services, and task convergence.")
def audit_swarm_health() -> str:
    """
    Generate a plan for assessing whether a swarm and its services are healthy.

    returns: str - A prompt instructing the agent to enumerate nodes/services and flag problems
    """
    return (
        "Audit the health of this docker swarm. Do not change anything — this is read-only:\n"
        "1. Call `list_nodes` and flag any node whose `Status.State` is not `ready` or whose "
        "`Spec.Availability` is `drain`/`pause`. Note manager nodes (`Spec.Role == manager`) and "
        "whether `ManagerStatus.Reachability` is `reachable` — an unreachable manager threatens "
        "quorum. Call out if you have an even number of managers or only one (no fault tolerance).\n"
        "2. Call `list_services`. For each service, compare desired vs running replicas: read the "
        "mode from `Spec.Mode` (Replicated count vs Global), then call `service_tasks(service_id, "
        "filters={'desired-state': 'running'})` to drop tasks the orchestrator has already retired, "
        "and count how many of the returned tasks have `Status.State == 'running'`. The filter keys "
        "on *desired* state, so a returned task can still be failed/rejected — don't count it as "
        "running just because it matched.\n"
        "3. For any service that is under-replicated, call `service_tasks` without the filter and "
        "look for tasks stuck in `rejected`/`failed`, or rapidly cycling through `shutdown` -> "
        "`starting` (a crash loop). Pull `Status.Err` and `Status.Message` off the failing task.\n"
        "4. For a service that is crash-looping, call `service_logs(service_id, tail=200, "
        "timestamps=True)` and surface the last error.\n"
        "5. If a node is drained and should be removed, note that `remove_node` (force only if it is "
        "still reachable) is the follow-up — but do not call it as part of this audit.\n"
        "Summarize as a table: nodes (state/availability/role/reachability) and services (desired vs "
        "running, status). End with a one-paragraph health verdict and the single most urgent fix."
    )


@mcp.prompt(description="Find the latest tag for an image without pulling it.")
def find_latest_image_tag(image: str) -> str:
    """
    Generate a plan for picking the right tag of an image from a registry.

    args: image: str - Image reference, e.g. "alpine", "ghcr.io/org/repo"
    returns: str - A prompt instructing the agent to query the registry and recommend a tag
    """
    return (
        f"Find the most appropriate tag for `{image}` without pulling it:\n"
        f'1. Call `registry_list_tags(image="{image}", limit=200)` to enumerate available tags.\n'
        f"2. Filter out floating tags (`latest`, `edge`, `nightly`) and pre-release suffixes (`-rc`, `-beta`, "
        f"`-alpha`). Pick the highest stable semantic-version tag.\n"
        f'3. Call `registry_inspect_manifest(image="{image}", reference=<picked-tag>)` to confirm the tag '
        f"exists and capture the digest. If the response is an OCI image index, list the supported platforms.\n"
        f'4. Call `registry_get_config(image="{image}", reference=<picked-tag>)` to read the image config '
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


@mcp.prompt(description="Plan and run a multi-platform image build with buildx.")
def plan_multiarch_build(image: str, platforms: str = "linux/amd64,linux/arm64", context: str = ".") -> str:
    """
    Generate a plan for building and pushing a multi-platform image with buildx.

    args: image: str - Target image reference, e.g. "ghcr.io/org/app:v1"
    args: platforms: str - Comma-separated platform list (default "linux/amd64,linux/arm64")
    args: context: str - Build context path (default ".")
    returns: str - A prompt instructing the agent to plan, build, and verify a multi-arch image
    """
    platforms_list = ", ".join(f'"{p.strip()}"' for p in platforms.split(",") if p.strip())
    return (
        f"Build and push `{image}` for multiple platforms ({platforms}) using buildx:\n"
        f"1. Call `buildx_ls` and confirm a non-`docker` driver is active (the default `docker` driver "
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


@mcp.prompt(description="Audit an image's CVE posture with Docker Scout.")
def audit_image_cves(image: str) -> str:
    """
    Generate a plan for walking through Scout's CVE reporting for an image.

    args: image: str - Image reference to scan
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


@mcp.prompt(description="Compare two image versions and report the CVE delta.")
def compare_image_versions(old_image: str, new_image: str) -> str:
    """
    Generate a plan for comparing two image references via Scout.

    args: old_image: str - The baseline image reference
    args: new_image: str - The candidate image reference
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


@mcp.prompt(description="Recommend a safer base image via Docker Scout.")
def recommend_base_image(image: str) -> str:
    """
    Generate a plan for picking a better base image using Scout.

    args: image: str - Image reference whose base should be reviewed
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
        f"`registry_inspect_manifest` here — its `image` argument strips any `:tag`/`@digest`, so a full "
        f"candidate ref would need to be split into separate `image` and `reference` arguments.\n"
        f"Report the recommended base, the CVEs it resolves, the CVEs it introduces (if any), and the "
        f"single-line Dockerfile change required. Do not modify any Dockerfile."
    )


@mcp.prompt(description="Inspect a multi-arch manifest list / OCI image index without pulling.")
def inspect_multiarch_manifest(image: str) -> str:
    """
    Generate a plan for inspecting an image's manifest list.

    Use this when reaching for `docker manifest inspect` — that command is in maintenance mode
    and lacks support for OCI image indexes and attestations. `buildx_imagetools_inspect` is
    the path forward.

    args: image: str - Image reference (tag or digest), e.g. "alpine:3.19"
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


@mcp.prompt(description="Create a multi-arch manifest list from existing per-platform tags.")
def create_multiarch_manifest(target_tag: str, source_tags: str) -> str:
    """
    Generate a plan for stitching per-platform tags into a manifest list.

    Use this when reaching for `docker manifest create` + `docker manifest push` —
    `buildx_imagetools_create` does both in one step and handles OCI image indexes.

    args: target_tag: str - The new combined tag, e.g. "org/app:v1"
    args: source_tags: str - Comma-separated source tags (each must already be pushed),
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


@mcp.prompt(description="Translate `docker manifest …` commands into buildx imagetools equivalents.")
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


@mcp.prompt(description="Review a Dockerfile for security, correctness, and cache-efficiency issues.")
def review_dockerfile(dockerfile_path: str) -> str:
    """
    Generate a plan for reviewing a Dockerfile against Docker's reference and best practices.

    args: dockerfile_path: str - Filesystem path to the Dockerfile to review
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


@mcp.prompt(description="Audit running containers for risky runtime configuration (privilege, host access).")
def audit_container_security() -> str:
    """
    Generate a plan for sweeping running containers for dangerous runtime settings.

    returns: str - A prompt instructing the agent to inspect each container's HostConfig and flag risks
    """
    return (
        "Audit the security posture of running containers. This is read-only — do not change anything. "
        "For background on why these settings matter, the MCP resource `docker-docs://engine-security` "
        "covers the daemon's trust model.\n"
        "1. Call `list_containers` (running only) to get the set to audit.\n"
        "2. For each container, call `get_container` and inspect its `HostConfig` / `Config`, flagging:\n"
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


@mcp.prompt(description="Diagnose why one container cannot reach another over the network.")
def debug_container_networking(source: str, target: str) -> str:
    """
    Generate a plan for diagnosing container-to-container connectivity.

    args:
        source: str - The container that cannot connect (name or ID)
        target: str - The container it is trying to reach (name or ID)
    returns: str - A prompt instructing the agent to compare networks and test connectivity
    """
    return (
        f"Diagnose why container `{source}` cannot reach `{target}`. Work from the most common cause "
        f"(not on a shared network) outward:\n"
        f"1. Call `get_container` on both and compare `NetworkSettings.Networks`. If they share no "
        f"user-defined network, that is almost certainly the problem — containers on the default bridge "
        f"cannot resolve each other by name; they must share a user-defined network. Recommend "
        f"`connect_network` to attach them to a common one.\n"
        f"2. If they DO share a network, note the DNS alias `{target}` should resolve to (the service/"
        f"container name or a network alias on that shared network).\n"
        f"3. From inside `{source}`, use `exec_in_container` to test, preferring an exec-form argv: "
        f"resolve DNS (e.g. `['getent', 'hosts', '{target}']`) and test the port "
        f"(`['nc', '-z', '-w', '2', '{target}', '<port>']` if `nc` exists). Distinguish a DNS failure "
        f"(name doesn't resolve) from a connection failure (resolves but refused/timed out).\n"
        f"4. If DNS resolves but the connection is refused, check that `{target}` actually listens on "
        f"that port and on `0.0.0.0` rather than `127.0.0.1` — use `get_container` for its exposed "
        f"ports and `container_logs` to confirm the service started.\n"
        f"5. Distinguish container-to-container reachability from host-published ports: a missing "
        f"`-p`/`ports` mapping only affects access from the host, not between containers on a shared "
        f"network.\n"
        f"State the root cause in one sentence and the concrete fix; do not change anything without "
        f"showing it first."
    )


@mcp.prompt(description="Investigate what is consuming docker disk space before pruning.")
def investigate_disk_usage() -> str:
    """
    Generate a plan for attributing docker disk usage to a cause before reclaiming it.

    returns: str - A prompt instructing the agent to break down usage across images, containers, volumes, and cache
    """
    return (
        "Find out WHAT is consuming docker disk space before reclaiming any of it — this is read-only "
        "diagnosis, not cleanup:\n"
        "1. Call `df` for the top-line split across Images, Containers, Local Volumes, and Build Cache. "
        "Identify which bucket dominates — the fix differs for each.\n"
        "2. If Images dominate: call `list_images` and sort by size. For the largest, call "
        "`image_history` to see which layers are heavy (a fat `COPY`, an un-cleaned package cache) and "
        "whether several images share base layers (so the on-disk cost is less than the sum of sizes).\n"
        "3. If Build Cache dominates: call `buildx_du` for the cache breakdown. This is reclaimable with "
        "`buildx_prune` and is invisible to `prune_images`.\n"
        "4. If Local Volumes dominate: call `list_volumes` and cross-reference with `list_containers"
        "(all=True)` to spot dangling volumes no container references — but do NOT assume a dangling "
        "volume is junk; it may hold data whose container is gone.\n"
        "5. If Containers dominate: look for stopped containers with large writable layers (`container_"
        "diff` shows what a container wrote on top of its image).\n"
        "Report a breakdown with the dominant cause, the specific offenders, and what each would reclaim "
        "— then point at the `clean_environment` prompt for the actual pruning. Recommend nothing "
        "destructive here."
    )


@mcp.prompt(description="Back up a named volume's contents to a tar file on the server host.")
def backup_volume(volume: str, dest_path: str) -> str:
    """
    Generate a plan for backing up a named volume using a throwaway container.

    args:
        volume: str - The named volume to back up
        dest_path: str - Host path (on the server) to write the tar archive to
    returns: str - A prompt instructing the agent to tar the volume out via a helper container
    """
    return (
        f"Back up the contents of volume `{volume}` to `{dest_path}` on the server host. Docker has no "
        f"native volume-export API, so mount the volume into a throwaway container and pull its "
        f"filesystem out through the Docker archive API:\n"
        f'1. Confirm the volume exists with `get_volume("{volume}")`.\n'
        f"2. Quiesce writers if integrity matters: if a running container has `{volume}` mounted and is "
        f"writing to it, a hot copy can be inconsistent — note which containers use it (cross-reference "
        f"`list_containers`) and offer to `stop_container` them first, or warn that the backup is "
        f"crash-consistent only.\n"
        f"3. Create a helper container with the volume mounted at `/data`, e.g. `create_container` from "
        f"`alpine` with `{volume}` mounted at `/data`. It does not need to run — the archive API reads "
        f"the volume through the mount whether or not the container is started; no `tar` binary in the "
        f"image is required.\n"
        f'4. Call `get_container_archive_to_file(<helper>, path="/data", dest_path="{dest_path}")` to '
        f"write the volume contents as a tar to `{dest_path}` (a path on the host running this MCP "
        f"server, written as the server's user). The archive is rooted at `data/` (the API names the "
        f"tar after the path's last component) — `restore_volume` relies on that, so don't repackage it.\n"
        f"5. Remove the helper container with `remove_container`, and restart anything you stopped in "
        f"step 2.\n"
        f"Report the archive path and size. `restore_volume` is the exact inverse."
    )


@mcp.prompt(description="Restore a named volume's contents from a tar file on the server host.")
def restore_volume(volume: str, source_path: str) -> str:
    """
    Generate a plan for restoring a named volume from a tar archive using a throwaway container.

    args:
        volume: str - The named volume to restore into
        source_path: str - Host path (on the server) to read the tar archive from
    returns: str - A prompt instructing the agent to untar an archive into a volume via a helper container
    """
    return (
        f"Restore the contents of `{source_path}` into volume `{volume}`. This is the inverse of "
        f"`backup_volume` and is destructive to whatever `{volume}` currently holds — confirm with the "
        f"user before overwriting:\n"
        f"1. Check whether `{volume}` already exists with `get_volume`. There is no way to tell whether "
        f"a volume holds data without mounting it, so if the volume already exists, STOP and confirm the "
        f'overwrite regardless. If it does not exist, `create_volume("{volume}")`.\n'
        f"2. Ensure no running container is using `{volume}` — restoring underneath a live writer "
        f"corrupts state. Use `list_containers` to check and offer to `stop_container` them first.\n"
        f"3. Create AND start a helper container from `alpine` with `{volume}` mounted read-write at "
        f'`/data` (e.g. command `["sleep", "3600"]` so it stays up for the exec).\n'
        f'4. Clear stale files first with `exec_in_container(<helper>, ["sh", "-c", "rm -rf /data/* '
        f'/data/.[!.]* /data/..?* 2>/dev/null || true"])` — otherwise files not present in the archive '
        f"survive the restore.\n"
        f'5. Call `put_container_archive_from_file(<helper>, path="/", file_path="{source_path}")`. Use '
        f'`path="/"`, not `/data`: a `backup_volume` archive is rooted at `data/`, so extracting it at '
        f"the root lands the contents back in `/data` (extracting at `/data` would nest them in "
        f"`/data/data`). `{source_path}` is read from the host running this MCP server.\n"
        f"6. Remove the helper with `remove_container` and restart anything you stopped.\n"
        f'Verify with `exec_in_container(<helper>, ["ls", "/data"])` (before removing it) or a quick '
        f"`alpine ls` helper, confirming the expected files are present. Report what was restored."
    )
