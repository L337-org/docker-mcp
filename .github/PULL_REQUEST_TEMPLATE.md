## Summary

<!-- What does this change do, and why? -->

## Test plan

<!-- How did you verify this? e.g. `uv run pytest -v`, `uv run ruff check . && uv run ruff format --check .`, `uv run pyright`, manual steps. -->

## Checklist

- [ ] `uv run pytest -v`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pyright` all pass locally
- [ ] If this adds or changes a tool: the ["Checklist when adding a new tool module"](https://github.com/GavinLucas/docker-mcp/blob/main/CONTRIBUTING.md#checklist-when-adding-a-new-tool-module) in `CONTRIBUTING.md` has been followed (tests, prompts, resources, README, naming convention)
- [ ] If this changes project structure, conventions, env vars, or the tool/prompt/resource surface: both `CLAUDE.md` and `.github/copilot-instructions.md` are updated (see the mirror rule at the top of `CLAUDE.md`)
- [ ] If this changes a dependency: `uv.lock` is updated (`uv lock`) and committed alongside `pyproject.toml`
