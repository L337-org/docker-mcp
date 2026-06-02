# GitHub Copilot Instructions

This file provides guidance to GitHub Copilot when working with code in this repository.

## Project

`docker-mcp` is a Python MCP (Model Context Protocol) server that exposes the Docker SDK for Python as MCP tools. It requires Python >=3.14 and is managed with `uv`.

## Architecture

### Entry point
`docker_mcp.py` imports `server` and `tools`, then calls `mcp.run(transport="stdio")`.

### Server singleton (`server.py`)
`server.py` instantiates `FastMCP` and exports `mcp`. **Always import `mcp` from `server`**, never directly from the `mcp` package in tool files — doing so creates circular imports.

```python
from server import mcp
```

### Tools package (`tools/`)
Each file in `tools/` maps to one section of the Docker SDK documentation and contains `@mcp.tool()` decorated functions for that resource type.

| File | Docker SDK domain |
|------|-------------------|
| `tools/client.py` | `DockerClient` — connection and low-level client |
| `tools/containers.py` | Container lifecycle and management |
| `tools/images.py` | Image pull, build, push, inspect |
| `tools/networks.py` | Network create, connect, inspect |
| `tools/volumes.py` | Volume create, list, remove |
| `tools/configs.py` | Swarm configs |
| `tools/nodes.py` | Swarm nodes |
| `tools/plugins.py` | Plugin install and management |
| `tools/prompts.py` | `@mcp.prompt()` templates for common docker workflows |
| `tools/resources.py` | `@mcp.resource()` endpoints exposing the Docker SDK for Python docs |
| `tools/secrets.py` | Swarm secrets |
| `tools/services.py` | Swarm services |
| `tools/swarm.py` | Swarm init, join, leave |

`tools/__init__.py` re-exports all modules so `docker_mcp.py` only needs `import tools`.

### Tests (`tests/`)
Each `tools/<module>.py` has a corresponding `tests/test_<module>.py`. Tests use pytest. `tests/__init__.py` is intentionally empty. `tests/integration/` holds `@pytest.mark.integration` tests that require a real Docker daemon — excluded by default, run with `uv run pytest -m integration`.

## Conventions

- New Docker functionality goes in the matching `tools/<domain>.py` — do not create new tool files without a corresponding entry in `tools/__init__.py` and a matching test file.
- Tool functions must be decorated with `@mcp.tool` where `mcp` is imported from `server`.
- Line length limit: 120 characters.
- Do not add comments that describe what the code does — only add comments for non-obvious constraints or workarounds.

### Tool function format

All `@mcp.tool` functions must follow this exact docstring format:

```python
@mcp.tool()
def mcp_example(name: str):
    """
    Say hello to someone by name.

    args: name: str - The name to say hello to
    returns: str - The greeting
    """
    return f"Hello, {name}!"
```

- One-line summary sentence, then a blank line
- `args:` section lists each parameter as `name: type - description`
- `returns:` line documents the return type and what it contains

### MCP resources

`tools/resources.py` exposes `@mcp.resource(uri, mime_type=...)` endpoints (not tools) for read-only data — currently the Docker SDK for Python documentation under the `docker-docs://` URI scheme. Use the same docstring format as tools.

### MCP prompts

`tools/prompts.py` exposes `@mcp.prompt(description=...)` templates that return prompt strings to guide multi-step docker workflows (deploy, migrate, troubleshoot, prune, doc lookup). Use the same docstring format as tools.

## Docker SDK Policy

**Only use `docker` module methods that are documented in the official reference.**  
Always verify the exact method name, parameter names, and return type at https://docker-py.readthedocs.io/en/stable/ before writing or suggesting code. Do not suggest methods that sound plausible but are not in the docs.

Docker SDK docs: https://docker-py.readthedocs.io/en/stable/index.html  
Docker SDK GitHub: https://github.com/docker/docker-py
