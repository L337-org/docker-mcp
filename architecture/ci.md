<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Continuous integration

Deep detail behind the summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing `.github/workflows/`.

The documentation set is no longer checked mechanically. `AGENTS.md` is the single instruction
file, so there is no pair to keep in step, and the job that watched for one-sided edits was
removed with the rule it enforced.

Nothing mechanical watches the documentation set now, and that is deliberate rather than a gap: a
check could only see that a file was touched, never that the right rule reached it, so it produced
green ticks it could not justify. A rule belongs in `AGENTS.md`, or in the `architecture/` note
that owns the detail, and which of those it is remains a judgement made at review.

An `Action pins are immutable` job fails the build when any `uses:` reference in
`.github/workflows` or `.github/actions` names a tag or branch rather than a full 40-hex commit SHA.
A tag can be repointed by its owner at any time, so `@v7` runs whatever they last pushed to it - and
`publish.yaml` mints an OIDC token for PyPI Trusted Publishing (`id-token: write`), pushes to GHCR
(`packages: write`) and uploads release assets (`contents: write`), so a repointed tag would execute
inside the jobs holding this project's strongest credentials. Trusted Publishing has no stored token
for an attacker to steal; executing in that job is the whole attack. First-party `actions/*` get no
exemption, and the pin costs nothing to hold because Dependabot bumps the SHA and rewrites the
trailing `# vX.Y.Z` comment. Local `./` actions are exempt (this repo's own code at its own commit),
and bare, quoted and uppercase SHAs are all accepted - hex is case-insensitive and GitHub resolves an
uppercase ref, so rejecting one would fail a legitimately pinned action. The trailing version comment
is convention, not enforced. The job is a port of the identically-named one in
[L337-org/apt](https://github.com/L337-org/apt), which is the reference implementation;
`send-to-influx` carries the third copy. **Keep the three in step** - the logic is deliberately
identical, and the uppercase-SHA fix had to be chased across all three because it was written once
and copied twice.

An `mcp<2` cap existed briefly: mcp 2.0.0 removed `mcp.server.fastmcp`, which `server.py` imported
`FastMCP` from, and an uncapped 2.2.0 shipped dead on arrival at import while every CI job stayed
green, because CI installs `--locked` against a lockfile pinning mcp 1.x; 2.2.1 hotfixed the cap.
`server.py` has since been ported to `mcp.server.mcpserver.MCPServer` and the cap lifted. Rather
than re-adding a cap for the next major (there is no known 3.x incompatibility to guard against),
`tests/test_pyproject_pins.py::test_the_declared_mcp_bound_matches_what_the_code_imports` is a
living guard: it fails whenever the installed mcp stops providing the import path `server.py`
actually uses, with no reliance on remembering to add a cap first. When adding a direct dependency
whose *import surface* we touch, consider whether a cap or a guard like this belongs with it.

**A required `Check fresh resolve still imports` job** (`premerge.yaml`) closes the blind spot that
let the 2.2.0 incident through: every job above installs with `uv sync --locked`, so none of them
ever resolve what a fresh `uvx`/`pip install` actually gets from the bare `pyproject.toml`
specifiers - only the pinned, known-good set in `uv.lock`. This job does, via `uv pip install`
(the pip-compatible interface, which never reads or writes `uv.lock`) into a throwaway venv, then
runs `import docker_mcp` and `docker-mcp-server --version` against that install. It reports a
resolution failure (a specifier no longer satisfiable) and an import failure (resolved fine, but
broke on import) as distinct errors, since they call for different fixes. This is a PR/push gate,
not a schedule - it complements rather than replaces the weekly canary's published-package install
smoke below, which exercises the actual shipped artefact rather than a hypothetical resolve of the
current tree.

A **weekly canary** (`.github/workflows/canary.yaml`, Mondays + dispatch) hunts platform/ecosystem
drift premerge CI can't see: wheels-only (`--only-binary :all:`) dependency resolution for Intel
macOS / ARM macOS / Windows against both the repo `pyproject.toml` and the latest published PyPI
release (the check that would have caught cryptography 49 dropping its x86_64-macOS wheel), plus
real install smokes of the published package on `macos-latest`, `macos-15-intel` (Intel runner
label retires Aug 2027), and `windows-latest` - `import docker_mcp` and `uvx docker-mcp-server
--version`. **PRs into main also run the repo-pyproject resolution leg** (the only part that
exercises PR content), so a dependency change that breaks a platform is caught before merge; the
published-package legs and issue filing stay schedule/dispatch-only. Failures on unattended runs
file a deduplicated `ci-failure` + `wf:canary` issue via `.github/actions/file-failure-issue`.
`main()` handles `--version` (print the installed version, exit) before any daemon/network
contact - the canary's entry-point smoke depends on it.
