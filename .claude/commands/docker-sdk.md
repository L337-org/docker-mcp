# Docker SDK Reference Check

This skill fetches and cross-references the Docker SDK for Python documentation to:
1. Verify that any SDK function or class you plan to use actually exists
2. Identify which SDK features are not yet exposed in this MCP server

## Instructions

When invoked (with or without a specific topic argument), do the following steps IN ORDER. Do not skip steps.

### Step 1 — Fetch the SDK documentation

Fetch the top-level API reference to get the list of available modules:
- https://docker-py.readthedocs.io/en/stable/index.html
- https://docker-py.readthedocs.io/en/stable/client.html
- https://docker-py.readthedocs.io/en/stable/containers.html
- https://docker-py.readthedocs.io/en/stable/images.html
- https://docker-py.readthedocs.io/en/stable/volumes.html
- https://docker-py.readthedocs.io/en/stable/networks.html
- https://docker-py.readthedocs.io/en/stable/services.html
- https://docker-py.readthedocs.io/en/stable/secrets.html
- https://docker-py.readthedocs.io/en/stable/configs.html
- https://docker-py.readthedocs.io/en/stable/nodes.html
- https://docker-py.readthedocs.io/en/stable/swarm.html
- https://docker-py.readthedocs.io/en/stable/plugins.html

If the user passed an argument (e.g., `/docker-sdk containers`), only fetch the page(s) relevant to that topic.

### Step 2 — Inventory what this MCP server currently exposes

The project structure maps SDK domains to tool files one-to-one:

| docker_mcp/tools/ file | tests/ file | Docker SDK domain |
|-------------|-------------|-------------------|
| `docker_mcp/tools/system.py` | `tests/test_system.py` | `DockerClient` |
| `docker_mcp/tools/containers.py` | `tests/test_containers.py` | Containers |
| `docker_mcp/tools/images.py` | `tests/test_images.py` | Images |
| `docker_mcp/tools/networks.py` | `tests/test_networks.py` | Networks |
| `docker_mcp/tools/volumes.py` | `tests/test_volumes.py` | Volumes |
| `docker_mcp/tools/configs.py` | `tests/test_configs.py` | Swarm Configs |
| `docker_mcp/tools/nodes.py` | `tests/test_nodes.py` | Swarm Nodes |
| `docker_mcp/tools/plugins.py` | `tests/test_plugins.py` | Plugins |
| `docker_mcp/tools/prompts.py` | `tests/test_prompts.py` | `@mcp.prompt()` templates for common docker workflows |
| `docker_mcp/tools/resources.py` | `tests/test_resources.py` | `@mcp.resource()` endpoints for the Docker SDK for Python docs |
| `docker_mcp/tools/secrets.py` | `tests/test_secrets.py` | Swarm Secrets |
| `docker_mcp/tools/services.py` | `tests/test_services.py` | Swarm Services |
| `docker_mcp/tools/swarm.py` | `tests/test_swarm.py` | Swarm |

Read each `docker_mcp/tools/*.py` file and list every `@mcp.tool` decorated function and the `docker` module methods it calls.

### Step 3 — Produce a gap analysis

Compare the SDK surface area from Step 1 against the MCP tool inventory from Step 2 and output a structured report:

```
## Docker SDK Coverage Report

### Currently Exposed
- <`docker` module method>() → <MCP tool name> (docker_mcp/tools/<file>.py)
...

### Not Yet Exposed (SDK features missing from MCP server)
- <`docker` module method>() — <one-line description> → should go in docker_mcp/tools/<file>.py
...

### Verification Notes
For any specific functions the user asked about: confirmed they exist (or do not exist)
in the live documentation, with the exact signature.
```

### Step 4 — Guard against hallucination

Before any code you write in this session uses a `docker` module method:
- Confirm the exact method name, parameter names, and return type from the fetched docs
- If the docs don't clearly show a method, state that it could not be verified and do not use it
- Never assume a method exists because it sounds plausible

## Structural rules (enforce these when writing code)

- All `@mcp.tool` functions import `mcp` from `server.py` — never import directly from `mcp`
- New functionality goes in the existing `docker_mcp/tools/<domain>.py` file that matches the SDK domain
- If a new `docker_mcp/tools/<file>.py` is ever created, it must be added to `docker_mcp/tools/__init__.py` and have a matching `tests/test_<file>.py`

## Usage examples

```
/docker-sdk                  # Full gap analysis — all SDK pages
/docker-sdk containers       # Only fetch containers docs, verify container methods
/docker-sdk volumes networks # Only fetch volumes + networks docs
```
