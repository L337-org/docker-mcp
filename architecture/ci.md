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

The `Repository hygiene` and `Action pin check` jobs (`premerge.yaml`) each run a shared action
from [L337-org/github-workflows](https://github.com/L337-org/github-workflows), named at its
major-version tag, rather than a copy kept in this repository. What each check covers, and how to
run the hygiene check locally at that version, is in that repository's README; it is not restated
here because it would drift. A change to either check lands there and reaches this repository
when that repository moves the tag; a breaking change gets a new major tag, which this repository
moves to in its own pull request.

Third-party actions are pinned by full commit SHA. The shared workflows are the one exception, and
it holds only while that tag is protected by a ruleset in that repository, so that moving it takes
the same people as merging there. That repository's README, under "Versions and releases", states
the condition and is where keeping it true belongs. Whoever can move it reaches what the shared code runs with here:
the review's `pull-requests: write` permission and `CLAUDE_CODE_OAUTH_TOKEN`, the
`SLACK_BOT_TOKEN` passed to the reporter and the announcer, and the logic of the required
`Repository hygiene` and `Action pin check` jobs. `publish.yaml`, which holds the release
credentials, uses nothing from github-workflows.

The pin check matters most here because `publish.yaml` mints an OIDC token for PyPI Trusted
Publishing (`id-token: write`), pushes to GHCR (`packages: write`) and uploads release assets
(`contents: write`), so a repointed third-party tag would execute inside the jobs holding this
project's strongest credentials. Trusted Publishing has no stored token for an attacker to steal;
executing in that job is the whole attack. The shared action's default directories,
`.github/workflows` and `.github/actions`, are this repository's, so the local
`file-failure-issue` action is checked too.

A `Code review` workflow (`code-review.yaml`) calls the shared Claude review workflow at the same
version tag. The header of that file says when this repository asks for a review; the shared
README describes how the review itself runs.

**A failed run is posted to Slack.**  A failed run is otherwise one red entry in a list nobody
opens.  `.github/workflows/report-failures.yaml` names the workflows to watch and calls the
shared Slack reporter from github-workflows, at the same version tag as the actions above.
What the reporter posts and when is in that repository's README; it is not restated here
because it would drift.  This repository has it report runs started by `schedule`, `push`,
`release` or `pull_request`, since a failing pull request run's page is easy to miss.  A run
started by hand, or a review asked for by comment, is left out on purpose, because whoever
started it is watching.  The post goes to the project's Slack channel, named in the workflow's
`with:`, through the bot token in `SLACK_BOT_TOKEN`, which must also be set as a Dependabot
secret because a run Dependabot starts sees only Dependabot secrets.
This is in addition to the `ci-failure` issues the canary and the release file: the post says a
run failed, the issue is the deduplicated record of a failure stream.  To add a workflow
triggered by `schedule`, `push`, `release` or `pull_request`, give it a `name:` and add that
name to the `workflows:` list in `report-failures.yaml`, exactly as written, since GitHub
matches on it; the shared hygiene check enforces the list.

**A pull request that needs action is posted to Slack.**  `announce-pull-requests.yaml` calls
the shared announcer at the same version tag on a pull request being opened, reopened or marked
ready for review, and posts to the project's Slack channel through the same token, which must
also be a Dependabot secret so that Dependabot's pull requests are announced.  What it posts and
which pull requests it skips is in github-workflows' README.  It runs on `pull_request`, so it
is in the `workflows:` list above.

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

**The `Claude plugin rehearsal` jobs** (`premerge.yaml`) run `scripts/claude_plugin_release.py`, the
script the release's Claude plugin jobs run, against a wheel built from the checkout, so a change
that would break the post-release plugin publish fails on the pull request. They cover everything
except the PyPI half (that PyPI serves the release), which the release checks; the publish step is a
dry run against the live branch. Each must be a required check in the `main` ruleset (check with
`gh api repos/L337-org/docker-mcp/rulesets`), so renaming one means updating the ruleset in the
same change. [distribution.md](distribution.md) has the release side.

A **weekly canary** (`.github/workflows/canary.yaml`, Mondays + dispatch) hunts platform/ecosystem
drift premerge CI can't see: wheels-only (`--only-binary :all:`) dependency resolution for Intel
macOS / ARM macOS / Windows against both the repo `pyproject.toml` and the latest published PyPI
release (the check that would have caught cryptography 49 dropping its x86_64-macOS wheel), plus
real install smokes of the published package on `macos-latest`, `macos-15-intel` (Intel runner
label retires Aug 2027), and `windows-latest` - `import docker_mcp` and `uvx docker-mcp-server
--version`. **PRs into main also run the repo-pyproject resolution leg** (the only part that
exercises PR content), so a dependency change that breaks a platform is caught before merge; the
published-package legs and issue filing stay schedule/dispatch-only. Failures on unattended runs
file a deduplicated `ci-failure` + `wf:canary` issue via `.github/actions/file-failure-issue`,
and a failed scheduled or pull request run is also posted to Slack (above).
`main()` handles `--version` (print the installed version, exit) before any daemon/network
contact - the canary's entry-point smoke depends on it.
