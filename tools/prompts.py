# library of mcp prompt templates that guide the agent through common docker workflows

from server import mcp


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
        f"Migrate container `{container}` to image `{new_image}` without losing its configuration:\n"
        f"1. Use `get_container` to capture the current name, env vars, mounts, ports, network, and restart "
        f"policy.\n"
        f"2. Use `pull_image` to fetch `{new_image}`.\n"
        f"3. Use `stop_container` followed by `remove_container` on the old container.\n"
        f"4. Use `run_container` to start a new container with the captured config but the new image.\n"
        f"5. Verify with `list_containers` and `container_logs` that the replacement is healthy.\n"
        f"Show the captured config back to the user before recreating the container."
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
        "1. Use `df` to show current disk usage so the user sees a before/after.\n"
        "2. Use `prune_containers` to remove stopped containers.\n"
        "3. Use `prune_images` (without `filters={'dangling': False}`) to remove dangling images only.\n"
    )
    if scope == "all":
        base += (
            "4. Use `prune_networks` to remove unused user-defined networks.\n"
            "5. Use `prune_volumes` ONLY after explicitly confirming with the user — volumes can hold "
            "irreplaceable data.\n"
        )
    base += "Report the total space reclaimed at the end."
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
        f"4. If `{image}` is a Docker Hub image, also call `hub_repo_info` to surface star and pull counts so "
        f"the user can sanity-check the image's provenance.\n"
        f"Report the recommended tag, its digest, and the supported platforms. Do not pull the image."
    )
