"""Entry point for the MCPB (Claude Desktop Extension) bundle — referenced as `entry_point` in
manifest.json. The bundle is a `uv`-type extension: the host's managed uv resolves the dependencies
declared in pyproject.toml and runs this file, which hands off to the same `main()` as the console
scripts and `python -m docker_mcp`. Kept at the bundle root (not reusing docker_mcp/__main__.py) so
`import docker_mcp` resolves whether the host installs the project into the uv env or just runs this
script from the bundle root."""

from docker_mcp import main

main()
