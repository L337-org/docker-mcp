# AGENTS.md

The shared instruction file for this repository. Every assistant reads this one; `CLAUDE.md` and
`.github/copilot-instructions.md` are pointers to it.

`docker-mcp` is a Python MCP server (requires Python >=3.14) managed with `uv` that exposes the
Docker SDK for Python as MCP tools. The entry point is the `docker_mcp` package, run with
`python -m docker_mcp` or via the installed console script.

## Review priorities

Ordered by where real defects in this project have actually come from. Spend review effort in this
order.

1. **Claims in prose that nothing type-checks.** Docstrings, docs and comments asserting a method,
   flag, route or identifier semantics that does not exist. Three `scout` tools shipped a `--format`
   flag the subcommand does not define; a docstring claimed buildx resolves `--file` against the build
   context when it resolves against the CWD. Verify against the primary source, not recollection.
2. **A guard that silently stops guarding.** See the invariant list below. These pass CI, pass review
   at a glance, and fail in production or never fail loudly at all.
3. **Enumerations that drift.** Any list of domains, tools, channels or files that is copied rather
   than derived. One list was wrong in six places across a single feature branch.
4. **Unbounded reads of anything external.** Daemon streams, registry bodies, staged files, CLI output.
5. **Docstring quality on any touched `@tool()`** - the ratchet in the checklist below.
6. **Everything else.**

## Area checks

Read the linked file before judging a substantive change to that area.

| Area | Read | Watch for |
|---|---|---|
| `server.py`, `docker_mcp/tools/` | [architecture/server.md](architecture/server.md) | `_slim_schema` or `_apply_host_schema` changing validation rather than display; a disabled capability registered-then-refusing instead of absent |
| `_hosts.py`, host selection | [architecture/hosts.md](architecture/hosts.md) | `use_context=False` dropped; `system_reconnect` gaining the ability to retarget an arbitrary URL; a write path that no longer requires an explicit `host` in multi-host mode |
| `_cli.py`, `_ssh_proxy.py`, CLI-backed tools | [architecture/cli-shell-out.md](architecture/cli-shell-out.md) | `subprocess.run` called directly; `shell=True`; a missing `timeout=`; the remote-exec fallback preferred over a usable local CLI; staging consequences absent from the docstring |
| any `@tool()` docstring | [architecture/tool-descriptions.md](architecture/tool-descriptions.md) | the checklist below |
| code calling `docker` | [architecture/docker-sdk.md](architecture/docker-sdk.md) | an unverified method; a hand-built route that is not in the Engine API spec; a reach-in past the public SDK introduced without recorded sign-off |
| `Dockerfile`, `manifest.json`, `server.json`, release workflows | [architecture/distribution.md](architecture/distribution.md) | version drift across the four files; a registry ownership marker no longer matching `server.json`'s `name` |
| `skills/l337-docker/` | [architecture/agent-skill.md](architecture/agent-skill.md) | the skill described as equivalent to the server; tool-permission frontmatter added; a hand-edited figure in `MCP_VS_SKILLS.md` |
| `.github/workflows/` | [architecture/ci.md](architecture/ci.md) | a `uses:` naming a tag or branch; a new job without `timeout-minutes` where the workflow requires one |

## Project

`docker-mcp` is a Python MCP server (requires Python >=3.14) managed with `uv` that exposes the Docker
SDK for Python as MCP tools. The entry point is the `docker_mcp` package, run with `python -m
docker_mcp` or via the installed console script.

Five channels ship from this repo, all detailed in
[architecture/distribution.md](architecture/distribution.md):

- **PyPI** as `docker-mcp-server` (the `docker-mcp` name was taken). The import package stays
  `docker_mcp` and the repo stays `…/docker-mcp`. Two console scripts, `docker-mcp` and
  `docker-mcp-server`, both target `docker_mcp:main`.
- **GHCR** (`ghcr.io/l337-org/docker-mcp-server`), mirrored to Docker Hub
  (`gavinlucas/docker-mcp-server`) when the opt-in `DOCKERHUB_*` release secrets are configured.
- **A Claude Desktop Extension (`.mcpb`)** attached to each GitHub Release.
- **A Homebrew tap** in `L337-org/homebrew-tap`, currently **paused** — do not re-enable it without
  reading the four-step re-enable procedure in the architecture note.
- **A Claude Code agent skill** in `skills/l337-docker/`, attached to each Release as a `.tar.gz`.
  Deliberately **not** a channel for the server: it is a CLI-only *alternative* to it. Its guards are
  conventions a model can skip, not refusals at a call boundary — never describe it as equivalent.

The `docker` dependency is pulled with its `[ssh]` extra (paramiko), so `DOCKER_HOST=ssh://…` works
through a pure-Python transport — no system `ssh` binary, on the host and in the container images
alike. CLI-backed tools shell out to `docker`, which would otherwise use the *system* `ssh`; they are
routed through a per-call paramiko proxy instead, and fall back to running on the remote host when
there is no local `docker` at all. That machinery, and the IPv6-to-IPv4 fallback both transports
share, is in [architecture/cli-shell-out.md](architecture/cli-shell-out.md) and
[architecture/hosts.md](architecture/hosts.md).

## Commands

```bash
# Install dependencies
uv sync

# Run the project
uv run python -m docker_mcp

# Add a dependency
uv add <package>

# Run unit tests (integration tests are excluded by default)
uv run pytest -v

# Run integration tests (require a real Docker daemon)
uv run pytest -m integration -v

# Lint and format
uv run ruff check .
uv run ruff format .

# Type-check
uv run pyright

# Install pre-commit hooks (one-time)
uv run pre-commit install

# Regenerate the measured figures MCP_VS_SKILLS.md quotes (reports only; add --json for the
# structured form). Self-contained: pins its own tiktoken, installs the working tree editable.
uv run scripts/measure-comparison-figures.py
```

## Architecture

The package is `docker_mcp`: `__init__.py` defines `main()` and side-effect-imports `server` and
`tools`, which is what registers every `@tool()`. `server.py` owns the `MCPServer` singleton, the
central `TOOL_CATEGORIES` map, the domain derivation behind `DOCKER_MCP_SERVER_DISABLE`, the schema
slimming, and the `instructions` router. `docker_mcp/tools/` holds one module per Docker feature area,
each backed by docker-py, a `docker` CLI shell-out, or direct registry HTTPS.

That is the map, not the detail. **Before changing any of it, read the matching file from the table at
the top** — [server.md](architecture/server.md) for registration, the tools package, resources and
prompts; [hosts.md](architecture/hosts.md) for the multi-daemon registry;
[cli-shell-out.md](architecture/cli-shell-out.md) for the CLI and SSH machinery.

Two things about that surface are load-bearing enough to state here:

- **A new tool module is a new domain**, so it needs a `TOOL_CATEGORIES` entry per tool *and* a
  `_DOMAIN_BLURBS` entry, or the `instructions` router omits the domain and a lazy-loading client
  never learns the vocabulary that finds its tools. Both are caught by `tests/test_server.py` — the
  router pair asserts the advertised domains equal the registered ones — so this is a "do it up
  front" note, not a silent trap. Full checklist in [CONTRIBUTING.md](CONTRIBUTING.md).
- **Tool modules import `tool` from `docker_mcp.server`; prompt modules import `prompt`.** Only
  resource modules import `resource` — no module imports `mcp` to register with any more.

## CLI shell-out policy

Any tool wrapping a `docker` CLI feature (Compose, Stack, Buildx, Scout, Context) **MUST** go through
`docker_mcp/tools/_cli.py:run_docker` — never call `subprocess.run` directly from a tool module. That
helper is the single place holding binary resolution, `shell=False`, UTF-8 decoding, the output byte
cap, Windows console suppression and the environment allow-list. Always pass an explicit `timeout=`.
Never pass `shell=True`, never build paths by string concatenation, never expand `~` or globs yourself.

**Read [architecture/cli-shell-out.md](architecture/cli-shell-out.md) before touching any of it**, and
before adding a CLI-backed tool. It covers the SSH remote-exec fallback (which changes *which host* a
command runs on), the file-staging session, path-token reconciliation, and the deliberate two-style
error convention — action tools return the raw result dict and never raise on a non-zero exit, while
parsed-query tools raise `RemoteFailureError`. **That split is intentional; do not "unify" it.**

## Docker SDK Policy

**Before writing or modifying any code that calls the Docker SDK, run `/docker-sdk` (or `/docker-sdk
<topic>`)** to verify exact method signatures against the live documentation. Never use a `docker`
module method you have not confirmed in the docs. Do not assume a method exists because it sounds
plausible; if you cannot confirm it, say so and do not use it.

Prefer the high-level object API; drop to `_get_client().api` only for documented gaps, verified the
same way. **"The method exists" and "the method works" are separate claims** — `plugin_push` is the
standing example of a documented, importable SDK method that 404s against every daemon.

**Reaching past the public SDK is not an agent's call to make.** Where the public path fails, stop,
write up what you found, and escalate for a human decision rather than implementing a workaround.
Document-and-escalate is the correct outcome, not a failure to finish. The fallback ladder, the
already-blessed reach-ins, and the **SDK audit exclusions that must not be re-proposed** are in
[architecture/docker-sdk.md](architecture/docker-sdk.md) — read it before responding to an SDK
coverage audit.

## Tool function format

Every `@tool()` function carries a docstring in one exact format: a one-line summary, a blank line, a
usage-guidance paragraph, then `args:` (one `name - description` line per parameter, never repeating
the type — the annotation already reaches the client in `inputSchema`) and a `returns:` line naming
the shape.

The docstring **is** the tool `description` the client pays context for on every session, and it is
scored externally on a six-dimension rubric. **Read
[architecture/tool-descriptions.md](architecture/tool-descriptions.md) before writing or changing
one** — it carries the format, the quality standard the ratchet applies to every touched docstring,
and the division of labour across the three discovery layers. Write it right the first time; four
cleanup rounds have chased the same failure.

## Conventions

- **Tool naming convention (2.0, permanent):** every tool is named `<management-command>_<verb>`, anchored
  to the docker CLI's management-command structure (`docker container ls` → `container_list`), with
  **long-form verbs** (`list`/`remove`/`inspect` — never `ls`/`rm`/`get`), applied uniformly even where the
  CLI lacks the long alias. Singular prefixes (`container_`, not `containers_`); read-only fetches may be
  noun-form (`container_logs`, `registry_tags`). Names never encode the backend (SDK vs CLI). Identifier
  params use one rule: `id_or_name` (daemon objects addressable by either), `name`/`names` (name-only
  resources: volumes, contexts, plugins, stacks, builders), `repository` (remote repo refs); durations are
  `timeout_seconds`. `tests/test_naming.py` enforces all of this (approved prefixes, banned short forms and
  1.x spellings, canonical shared-param descriptions) — a violating tool fails CI.
- New Docker functionality goes in the matching `docker_mcp/tools/<domain>.py` file, not in a new file.
- Every new `docker_mcp/tools/` file must be imported in `docker_mcp/tools/__init__.py` (private `_*.py` helpers excluded).
- Every new `docker_mcp/tools/<module>.py` must have a matching `tests/test_<module>.py`.
- Tool functions are decorated with `@tool()` (imported from `docker_mcp.server`) and must have a `TOOL_CATEGORIES` entry in `docker_mcp/server.py`.
- **Bound any externally-sourced bytes before buffering/parsing them, and parse safely.** CLI output is capped in `run_docker` (`MAX_CLI_OUTPUT_BYTES`); registry HTTP bodies are streamed and capped at `_MAX_RESPONSE_BYTES` (`registry.py`) since registries are agent-pointed/untrusted (the cap is on the *decoded* stream, so it also stops a decompression bomb). New code that reads an untrusted file or network body must apply a similar bound. Always `json.loads` (never `eval`); if YAML is ever parsed in Python use `yaml.safe_load` only — today no module parses YAML (Compose YAML is read by the `docker` CLI, not us).
- **A URL taken from a response body is pinned to the origin it came from; a URL the caller named is not.** The distinction is who chose the destination. A registry host in a tool argument is the caller's choice and the whole point of the `registry_*`/`hub_*` tools, so it is not restricted. A `next`/`Link` URL is the *response's* choice, so `_validate_hub_next` requires Hub pagination to stay on `_HUB_API_BASE`'s scheme/host/port, and the OCI tag-list path keeps only the path from a `Link` header and re-applies the registry already being queried. A new paginating or link-following tool does the same. **Cross-host redirects are deliberately followed** and must not be "hardened" away: registries answer blob fetches with a redirect to a CDN on another host as normal operation (Docker Hub answers a config-blob GET with `307` to `production.cloudfront.docker.com`), so refusing them breaks `registry_image_config`; httpx strips `Authorization` on any cross-origin redirect, and a redirect reaches nothing a tool argument could not reach directly. Note the read-only/no-destructive switches are **not** egress controls (the registry tools are read-only); `DOCKER_MCP_SERVER_DISABLE=registry` is.
- **A parameter whose legal values are a genuinely closed set is typed `Literal[...]`, not `str`.**
  Pydantic turns that into an `enum` in the advertised `inputSchema` (`_slim_schema` preserves it,
  proven by a test) so an out-of-set value fails validation before anything executes, and the
  docstring then drops the value list rather than repeating it. Verify the set against a primary
  source - the subcommand's own `--help`, or docker-py's documented value list - never from
  memory: `docker scout cves --only-severity CRITICAL` exits 0 reporting no vulnerabilities on an
  image with three critical CVEs, and the daemon records an unrecognised `--scope` verbatim, so a
  wrong value is not reliably rejected by Docker itself. Where the set is *not* provably closed
  (compose validates `--protocol` not at all; buildx drivers are pluggable; docker-py documents no
  values for `isolation`), leave it a `str` and record why at the parameter, so a later pass does
  not re-propose it. `tests/test_server.py::test_closed_value_sets_are_advertised_as_enums` pins
  the current set.
- **The `@tool()` decorator is generic (`def tool[F: Callable[..., Any]](...) -> Callable[[F], F]`)
  so pyright checks arguments at every tool call site**, in tests and at internal callers alike.
  Annotating it as a bare `Callable` erases the parameter list and silently disables that checking
  everywhere: a wrong type, an unknown keyword and a value outside a `Literal` all passed the gate
  until this was fixed. `tests/test_server.py::test_pyright_still_checks_arguments_at_tool_call_sites`
  runs pyright over those three deliberate errors, so it also fails if only the return annotation is
  loosened while the type parameter stays. A test that must pass a deliberately invalid value marks
  that one call `# pyright: ignore[reportArgumentType]` with a reason, rather than being softened to
  a legal one.
- Line length limit: 120 characters (enforced by ruff and flake8).
- **Prose that ships is British English in plain ASCII punctuation.** A tool docstring ships: the server
  advertises it verbatim as the tool's `description`, which is what a model reads when choosing between
  164 tools. So do the README, the agent skill, prompts and resources, comments, commit messages and PR
  descriptions — the test is whether it ships, not who reads it. **Never `—` or `–`**: use `-`, or `:`
  where what follows explains what came before. Likewise `...` not `…`, `x` not `×`, straight quotes, and
  `10-15%` for ranges.
- **Tool descriptions are strict ASCII, with no symbol exception**, and CI enforces exactly that:
  `tests/test_docs.py::test_advertised_tool_descriptions_are_plain_ascii` asserts it against what
  `list_tools()` advertises, so a violating tool fails. There is no allow-list because none of the 164 has
  needed one - the three arrows and one ellipsis found during the sweep all read better as words. Elsewhere
  in shipped prose a symbol carrying meaning (`≥`, or `→` inside a table) is still fine; that is the one
  place the general rule and this check deliberately differ.
- **Scope stops at tool descriptions.** Comments, tests and workflow files still hold em dashes; none of
  them ship, and sweeping them is a separate decision.
- **`AGENTS.md` is exempt** - a working instruction file that never ships - which is why it still uses
  em dashes freely.

## Provenance labels

Resources this server **creates** are stamped with `docker-mcp-server.*` provenance labels (`.managed=true`, `.version`, `.tool`, `.created`) via `docker_mcp/tools/_labels.py`, so the agent/operator can later enumerate that footprint — the `managed_only=True` arg on `container_list` / `network_list` / `volume_list` / `service_list`, or `--filter label=docker-mcp-server.managed=true`. The `prune_managed` prompt tears down only the managed footprint. Stamping is **on by default** and additive (a caller-supplied label always wins on a key collision); `DOCKER_MCP_SERVER_NO_LABELS=1` turns it off. The prefix is the bare project name (deliberately not reverse-DNS) and is a single constant in `_labels.py`.

When adding a new create tool that accepts a `labels` dict, route it through `_labels.py:with_provenance(labels, "<tool_name>")` (it accepts the dict/list/None shapes the SDK accepts and returns `None` — feed it through `drop_none` — when stamping is off and the caller passed nothing). The seven stamped creators today are `container_run`, `container_create`, `network_create`, `volume_create`, `service_create` (service-level `labels` only, not `container_labels`), `config_create`, `secret_create`. **Image builds are intentionally NOT stamped** — a build label changes the resulting image digest. Compose/stack containers (created via CLI shell-out) are also unstamped, as is `plugin_create` — the Engine's plugin-create call accepts no labels field, so there is nothing to stamp. The rule is conditional on the tool *accepting a `labels` dict*: a creator with nowhere to put a label is an expected exception, not an oversight, and should say so in its docstring. New `managed_only`-style label filters go through `_labels.py:managed_filter`.

## Invariants that fail silently

The centre of this file. Each of these is a rule whose violation produces no error, no failing test
and no obvious symptom.

- **Raise a `DockerMcpError` subclass for a failure the caller can act on; leave a bare
  `RuntimeError`/`ValueError` for one that means this server is broken.** The MCP SDK decides what a
  failure tells the client purely by type: `@tool()` translates `DockerMcpError` into the SDK's
  `ToolError`, whose message survives, and anything else is reported as `Error executing tool
  <name>` with the text withheld and a traceback logged. Get this backwards and either a refusal
  arrives saying nothing actionable, or a bug's internals go on the wire untraced. Never widen the
  translation to `Exception`.
- **Pick the subclass by what the caller does next**, since that is the only thing the distinction
  buys: `ToolInputError` (fix the argument), `ToolRefusalError` (this server declined; do not retry
  as-is), `RemoteFailureError` (the far end failed and said why; may be transient),
  `CapabilityError` (this host or install cannot, ever). Each type's docstring in
  `docker_mcp/exceptions.py` is the authority; a state error that says nothing a caller can use
  stays a builtin.
- **A failure raised by a library, not by us, is classified in `_LIBRARY_FAILURES`** (`server.py`).
  A daemon rejection, a registry status or a CLI timeout is as deliberate from the caller's side as
  anything this code raises, and its text is the useful part - without the table they reached the
  model as `Error executing tool <name>` with the explanation withheld. Order in that table is
  load-bearing: `NotFound` subclasses `APIError` subclasses `DockerException`, so the narrow entry
  goes first. Do not add `OSError`: `docker.errors.APIError` is one, so it would capture every
  daemon error along with whatever local problem you meant.
- **A bare `raise` of any builtin exception must appear in `DELIBERATE_CRASHES`**
  (`tests/test_server.py`) with the reason its text belongs only in the log. The list is exact, so
  adding a raise or removing one both fail until it is updated - which is the point: nothing else
  notices when a refusal is written as a builtin and stops reaching the model. The scan covers every
  builtin exception rather than a chosen few, because `raise FileNotFoundError("pass from_url
  instead")` is exactly as invisible as a bare `ValueError` - it named two classes once, and two
  actionable messages walked past it.
- **Register every tool through `@tool()` and every resource through `@resource()`**, never
  `@mcp.tool`/`@mcp.resource` directly. A direct registration
  returns the right payload and passes every behaviour test, and only stops explaining itself when
  something fails - so the guard is mechanical:
  `tests/test_server.py` walks the built server and fails any tool, resource or template whose
  callable is not translated, including one in a module that does not exist yet.
- **The `@tool()` decorator must stay generic** (`def tool[F: Callable[..., Any]](...) -> Callable[[F], F]`).
  Annotating it as a bare `Callable` erases the parameter list and silently disables pyright's argument
  checking at *every* tool call site. Flag any loosening, including loosening only the return
  annotation while keeping the type parameter.
- **`from_env` must always be passed `use_context=False`.** Without it docker-py resolves the active
  CLI context itself, reintroducing the mid-session `docker context use` drift that pinning exists to
  prevent, and can disagree with the endpoint the CLI shell-out targets for the same label.
- **A closed parameter value set must be `Literal[...]`, not `str`.** Docker does not reliably reject a
  wrong value: `docker scout cves --only-severity CRITICAL` exits 0 reporting no vulnerabilities on an
  image with three critical CVEs, and the daemon records an unrecognised `--scope` verbatim. Verify the
  set against the subcommand's `--help` or docker-py's documented list. Where the set is genuinely not
  closed, the parameter stays `str` **and says why at the parameter**, so a later pass does not
  re-propose it.
- **A new create tool that accepts a `labels` dict must route it through
  `_labels.py:with_provenance(labels, "<tool_name>")`.** Missing it makes the resource invisible to
  `managed_only=True` and to the `prune_managed` teardown, which then reports a clean daemon while
  resources remain. A creator with nowhere to put a label is an expected exception and should say so
  in its docstring.
- **The agent skill's provenance label is `l337-docker-skill.*`, lowercase, and never the server's
  `docker-mcp-server.*`.** Docker label keys are case-sensitive, so a mixed-case key published in one
  place and filtered in lowercase elsewhere matches nothing, silently. Stamping the server's label
  would misattribute the skill's resources to the server.
- **A tool iterating a potentially endless stream needs a wall-clock bound** - the `threading.Timer`
  plus `CancellableStream.close()` watchdog in `system_events` and `container_logs` follow mode.
- **Archive extraction uses `tarfile`'s `filter="data"`.** That, not the size and count bounds, is what
  stops an escaping member.
- **`buildx_build` must refuse a filesystem `dest=` in `output`/`cache_to`, a local `src=` in
  `cache_from`, and any `ssh=`** when the remote-exec fallback is in play. Each would resolve on the
  remote machine: losing the output, writing cache to the wrong disk, silently building uncached (a
  missing cache import is non-fatal to BuildKit), or reading the remote user's agent. `dest=-` is
  stdout and passes.
- **`server.json`'s version must stay in step with `pyproject.toml`** (`tests/test_pyproject_pins.py`
  catches this one). The release job restamps it anyway, so a stale value could never reach the
  registry - it is kept current because a stale value reads as drift to every reader and every
  scheduled audit.

## Docstring review checklist

Applies to **every `@tool()` docstring added or modified in the PR** - a ratchet, not a sweep of
untouched neighbours. Push back on any of these:

1. **Summary** is a specific verb plus resource, with the distinguishing trait up front where a sibling
   could be confused.
2. **A usage-guidance paragraph is present** (1-5 sentences between the summary and `args:`), and
   carries at least one discriminator naming the sibling tool(s) an agent could reach for instead,
   **by exact tool name** - never "the kill tool". Preconditions, side effects and irreversibility must
   be in prose: `readOnlyHint` / `destructiveHint` annotations do not substitute.
3. **For a CLI-backed tool, the error style is stated** - "does not raise on a non-zero CLI exit,
   inspect `returncode`/`stderr`" versus "raises `RemoteFailureError` on CLI failure". Do not let a docstring
   promise "never raises": a missing binary or plugin, or a subprocess timeout, still raises.
4. **`args:` lines add what the schema cannot carry** - format, accepted values, defaults, units,
   interactions. A line echoing the parameter name ("name - The volume name") is a finding. The type is
   **not** repeated: the annotation already reaches the client in `inputSchema`.
5. **`returns:` names the shape**, not just the type. For a full engine inspect document, say which
   document it is rather than enumerating an arbitrary subset of its keys. "dict - The X's attrs"
   identifies neither form and is a finding.
6. **Tool descriptions are strict ASCII, no symbol exception**
   (`tests/test_docs.py::test_advertised_tool_descriptions_are_plain_ascii` enforces it).
   Shipped prose more widely is British English in ASCII punctuation - watch in particular for text
   moved out of `AGENTS.md`, which is exempt and so carries Americanisms and em dashes that become
   defects the moment they land in a shipping file.
7. **Every factual claim is verified** against docker-py docs or the Engine API spec.

The test: reading only this docstring while holding 164 tool names and nothing else, could an agent
pick this tool over its neighbours and call it correctly first time?

## Deliberate - do not flag

Decisions already taken on the merits. Raising these generates the same false positive on every PR.
Removing an entry is a real decision, not a tidy-up.

**SDK surface deliberately not wrapped, or wrapped unobviously.** A recurring audit routine
re-proposes these; it has no memory of last time.

- **`plugin_push`'s hand-built endpoint call.** docker-py's `Plugin.push()` / `APIClient.push_plugin()`
  both POST to `/plugins/{name}/pull`, a route the Engine does not define, so they 404 against every
  daemon. Our call is the working path, not debt. Revisit only if upstream fixes the URL.
- **`Container.attach` / `attach_socket` / `resize` unwrapped.** An interactive bidirectional stream
  does not fit a request/response tool call. `container_exec` covers scripted execution.
- **`service_rollback`'s `api.inspect_service` + `api.update_service`.** The high-level
  `Service`/`ServiceCollection` expose no `rollback`. Permanently low-level.
- **`system_logout`'s `api._auth_configs`.** There is no `logout` anywhere in the SDK and no server-side
  session to end. Permanently low-level.
- **`swarm_task_list` / `swarm_task_inspect`'s `api.tasks()` / `api.inspect_task()`.** docker-py has no
  task collection at all. These documented `APIClient` methods are the only public path.

**Other settled decisions.**

- **A missing `_DOMAIN_BLURBS` entry is not a review item** - `tests/test_server.py`'s
  `test_router_domain_lines_track_registered_domains_under_every_switch` and
  `test_instructions_default_to_the_live_registered_surface` both fail and name the missing domain.
  It was briefly listed here as an invariant that fails silently, which was simply wrong: a rule a CI
  test already enforces does not belong in an instruction file at all.

- **Target runtime is Python >=3.14.** Assume current stable CPython grammar and stdlib are available
  and valid; do not flag 3.14-valid syntax as a bug. The example reviewers and older models get wrong:
  [PEP 758](https://peps.python.org/pep-0758/) (Status: Final, Python-Version: 3.14) makes parentheses
  optional in `except` / `except*` clauses, so `except OSError, ValueError:` is a valid two-exception
  handler, **not** a `SyntaxError`. Verify with
  `python3.14 -c "import ast; ast.parse('try:\n pass\nexcept OSError, ValueError:\n pass')"`.
  The `as`-binding form still requires parentheses (`except (OSError, ValueError) as e:`), and ruff
  (pyupgrade, 3.14 target) may rewrite to the unparenthesized form.

- **Image builds are not provenance-stamped.** A build label changes the resulting image digest.
  Compose and stack containers, and `plugin_create`, are unstamped for their own recorded reasons.
- **Cross-host redirects are followed on purpose.** Registries answer blob fetches with a redirect to a
  CDN on another host as normal operation, so refusing them breaks `registry_image_config`. httpx
  strips `Authorization` on any cross-origin redirect, and a redirect reaches nothing a tool argument
  could not reach directly. Do not propose "hardening" this.
- **The two CLI error styles are intentional.** Action tools return the raw result dict and never raise
  on a non-zero exit; parsed-query tools raise `RemoteFailureError`. `compose_ps` and `compose_config` are
  sanctioned hybrids. Do not propose unifying them.
- **`context.py` is permanently excluded from the SSH remote-exec fallback.** Its tools manage *this*
  host's CLI context registry, which a remote host knows nothing about. This is not an oversight
  pending work.
- **`mcp` carries no major-version cap.**
  `tests/test_pyproject_pins.py::test_the_declared_mcp_bound_matches_what_the_code_imports` is the
  guard instead, and needs no cap remembered in advance. Flag only a change that silently narrows that
  guard. The same question - cap or guard? - applies to any new direct dependency whose import surface
  this project touches.
- **No `cryptography` pin.** It is transitive, and a wheels-only Intel-macOS resolve landing on a
  vulnerable version is accepted. Do not propose pinning it or asserting it in CI.
- **`scripts/measure-comparison-figures.py` is deliberately not a CI gate.** Token figures move a few
  tokens on any docstring edit, so asserting them would fail constantly and signal nothing. A weekly
  routine watches for drift instead. Do not propose promoting it.
- **The Homebrew tap is paused, and `skip_clean "libexec"` in the formula template is an unverified
  candidate workaround, not the fix.** Review has twice caught a comment claiming otherwise.
  `Formula[...].opt_bin` is kept over the `formula_opt_bin` helper deliberately: the helper only exists
  from Homebrew 6.0.3.
- **`AGENTS.md` is exempt from the prose style** - British English *and* ASCII punctuation - because
  it is a working instruction file that never ships. That is why it uses em dashes and American
  spellings freely. `architecture/`, `CONTRIBUTING.md`, the README, the skill and every
  tool docstring are **not** exempt. Comments, tests and workflow files still hold em dashes; none of
  them ship, and sweeping them is a separate decision.
- **These American spellings are deliberate; do not flag them.** Shipped prose is British English,
  but four sets are excluded for cause, and a sweep has confirmed nothing else remains:
  - **`CODE_OF_CONDUCT.md` in full** - it is the Contributor Covenant 2.1, quoted from upstream.
    Anglicising it would make it stop matching the document it claims to be.
  - **"Behavioral Transparency"** in `architecture/tool-descriptions.md` - one of Glama's own rubric
    dimension names, quoted verbatim. Renaming an external rubric's dimension misnames it. Labelled
    at the line.
  - **"catalog"** wherever it names the `tool_catalog()` function, the `docker-mcp://tool-catalog`
    resource, or the OCI `_catalog` endpoint. Spelling the prose "catalogue" would disagree with the
    identifier it refers to.
  - **"dialog"** for a UI dialog box, which is the standard technical term in British usage too.

## Docstrings: two conventions, and which is which

**Advertised docstrings keep the format documented above** - a `@tool()`, `@prompt()` or
`@resource()` docstring is the description a client loads and a model reads, so `CS.6.14` hands
it to the AI-consumer rules rather than to the Python docstring convention. It wants what the
schema cannot already carry; an `Args:` block duplicates what the annotation already sends in
`inputSchema`, and that duplication is paid for on every session. `pyproject.toml`'s ruff
`ignore-decorators` exempts the decorated ones, and
`tests/test_server.py::test_the_docstring_exemption_names_the_decorators_in_use` fails if a
rename or a move ever makes that exemption stop matching.

**`ignore-decorators` matches decorator syntax only, so it misses a registration made by
calling.** `docker_mcp/tools/resources.py` registers several resources as `resource(...)(fn)`,
because each takes two URIs or sits behind a multi-host branch, and those docstrings are
advertised while being invisible to the exemption - and to the test above, which cannot see a
registration that uses no decorator. The four host-qualified templates carry `# noqa: D405` for
that reason, and the marker goes **after the closing quotes**: anywhere inside the docstring and
it becomes part of the description a client reads. Before trusting an exemption here, check the
advertised surface directly - `list_resource_templates()` is a separate call from
`list_resources()`, and a check that omits it reports identical while a whole category moves.

**Everything else is Google style** - `Args:` and `Returns:`, capitalised - which is `CS.6.12`'s
format for Python, enforced by ruff's pydocstyle rules rather than by review.

No docstring rule is ignored - `pyproject.toml`'s `ignore` list is empty - so there is no backlog
to work through and nothing to add to. A rule that fails is a change to make, not an entry to park.
The MCP surface and tests are exempt by name rather than by rule, as described above and below.

**Parameters are documented `name: description`, not `name - description`.** The dash is the
advertised surface's separator and it had leaked into back-end code, where ruff reads it as no
description at all: 142 parameters across 42 functions were documented and reported as
undocumented, which is why D417 looked like the largest entry in the backlog and was the smallest.

**`scripts/check-repo-hygiene.py` is vendored byte-identically into four repositories** and
self-verifies against a digest they share, so any change to it lands in all four at once with the
digest regenerated in each. Its own failure message says so when they drift.

Tests are exempt: a test's name is its documentation.

## The advertised surface has a budget

`AC.1.2` makes the size of what this server advertises a tracked metric rather than an
afterthought, and at 164 tools it is the dominant cost of connecting to this server at all.
`tests/test_surface_budget.py` holds the ceilings: per tool, per component, and in total.

**What is measured is the wire form, not the docstring.** A tool costs its name, its description
and its whole input schema, and the schema is usually the larger half - `buildx_build` is 5,091
bytes on the wire against 3,398 of description. A docstring-only budget would go green on a
change that added twenty parameters.

**Bytes, not tokens.** Tokens are what a model pays, but they need a tokenizer pinned to a model
that changes underneath us, so the figure moves without the surface moving. That is why
`scripts/measure-comparison-figures.py` reports tokens and is explicitly not a gate, and why
this is bytes.

**Raising a ceiling is a deliberate decision**, made in a pull request and explained in the
commit message - not a way to turn a red test green. The number exists to force the question
"is this worth what every session will pay for it?" to be asked once rather than never.

<!-- BEGIN GENERATED -->
## Read these when they apply

- Read `.agents/policy/review-context.md` always - these apply to every activity.
- Read `.agents/policy/testing.md` when writing or running tests, or adding behaviour that needs them.
- Read `.agents/policy/architecture.md` when changing module structure, public surface, docstrings, generated files, deprecation, or log levels.

<!-- END GENERATED -->
