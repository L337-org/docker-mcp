# GitHub Copilot Instructions

This file drives GitHub Copilot's review of every PR in this repository. It is the reviewer's half of
a mirror: `CLAUDE.md` carries the same rules written for an implementer, and `architecture/` plus
`CONTRIBUTING.md` carry the rule-level detail both point at. Read the matching `architecture/` file
before judging a change to that area — the tables below say which.

**MIRROR RULE.** `CLAUDE.md`, this file, and the detail layer (`architecture/`, `CONTRIBUTING.md`) are
one documentation set. A change to project structure, conventions, env vars, the tool/prompt/resource
surface or distribution channels must reach every layer that carries the rule. Flag a PR that updates
one but not the others. The `Check docs mirror` job prompts for this but is a touch-test: it cannot
see whether the *same rule* reached each file, so an unrelated edit to the other mirror in the same PR
masks a genuine one-sided change. Check by reading, not by trusting the green tick. The inverse also
applies: if a CI test already enforces the rule mechanically, neither mirror should carry it as prose.

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

## Invariants that fail silently

The centre of this file. Each of these is a rule whose violation produces no error, no failing test
and no obvious symptom.

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
- **Externally-sourced bytes are bounded on the decoded stream, before buffering or parsing.** CLI
  output via `MAX_CLI_OUTPUT_BYTES`, registry bodies via `_MAX_RESPONSE_BYTES`, in-band payloads via
  `join_bounded(stream, max_bytes, what)` (default `MAX_PAYLOAD_BYTES`, 32 MiB), staged files via
  `_MAX_STAGE_BYTES` / `_MAX_STAGE_FILES`. Bounding the *compressed* stream is not bounding it.
- **A tool iterating a potentially endless stream needs a wall-clock bound** - the `threading.Timer`
  plus `CancellableStream.close()` watchdog in `system_events` and `container_logs` follow mode.
- **Archive extraction uses `tarfile`'s `filter="data"`.** That, not the size and count bounds, is what
  stops an escaping member.
- **`buildx_build` must refuse a filesystem `dest=` in `output`/`cache_to`, a local `src=` in
  `cache_from`, and any `ssh=`** when the remote-exec fallback is in play. Each would resolve on the
  remote machine: losing the output, writing cache to the wrong disk, silently building uncached (a
  missing cache import is non-fatal to BuildKit), or reading the remote user's agent. `dest=-` is
  stdout and passes.
- **External values in log and error messages use `!r`.** An unescaped value containing a newline
  forges a log line.
- **Emitted-data names are an interface.** Metric fields, columns, event types, returned dict keys.
  Nothing type-checks them, so a rename silently breaks charts and queries. A changed unit or meaning
  under an unchanged name is worse than a rename.
- **Config written at install time is never rewritten by an upgrade.** New config is optional with a
  safe default. Widening a value is safe; narrowing it is breaking.
- **`server.json`'s version must stay in step with `pyproject.toml`** (`tests/test_pyproject_pins.py`
  catches this one). The release job restamps it anyway, so a stale value could never reach the
  registry - it is kept current because a stale value reads as drift to every reader and every
  scheduled audit.

## Area checks

Read the linked file before judging a substantive change to that area.

| Area | Read | Watch for |
|---|---|---|
| `server.py`, `docker_mcp/tools/` | [architecture/server.md](../architecture/server.md) | `_slim_schema` or `_apply_host_schema` changing validation rather than display; a disabled capability registered-then-refusing instead of absent |
| `_hosts.py`, host selection | [architecture/hosts.md](../architecture/hosts.md) | `use_context=False` dropped; `system_reconnect` gaining the ability to retarget an arbitrary URL; a write path that no longer requires an explicit `host` in multi-host mode |
| `_cli.py`, `_ssh_proxy.py`, CLI-backed tools | [architecture/cli-shell-out.md](../architecture/cli-shell-out.md) | `subprocess.run` called directly; `shell=True`; a missing `timeout=`; the remote-exec fallback preferred over a usable local CLI; staging consequences absent from the docstring |
| any `@tool()` docstring | [architecture/tool-descriptions.md](../architecture/tool-descriptions.md) | the checklist below |
| code calling `docker` | [architecture/docker-sdk.md](../architecture/docker-sdk.md) | an unverified method; a hand-built route that is not in the Engine API spec; a reach-in past the public SDK introduced without recorded sign-off |
| `Dockerfile`, `manifest.json`, `server.json`, release workflows | [architecture/distribution.md](../architecture/distribution.md) | version drift across the four files; a registry ownership marker no longer matching `server.json`'s `name` |
| `skills/l337-docker/` | [architecture/agent-skill.md](../architecture/agent-skill.md) | the skill described as equivalent to the server; tool-permission frontmatter added; a hand-edited figure in `MCP_VS_SKILLS.md` |
| `.github/workflows/` | [architecture/ci.md](../architecture/ci.md) | a `uses:` naming a tag or branch; a new job without `timeout-minutes` where the workflow requires one |

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
   inspect `returncode`/`stderr`" versus "raises `RuntimeError` on CLI failure". Do not let a docstring
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
   moved out of `CLAUDE.md`, which is exempt and so carries Americanisms and em dashes that become
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
  It was briefly listed here as an invariant that fails silently, which was simply wrong. Per the
  MIRROR RULE, a rule a CI test already enforces does not belong in either instruction file.

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
  on a non-zero exit; parsed-query tools raise `RuntimeError`. `compose_ps` and `compose_config` are
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
- **`CLAUDE.md` and this file are exempt from the prose style** - British English *and* ASCII
  punctuation - because they are working instructions that never ship. That is why they use em dashes
  and American spellings freely. `architecture/`, `CONTRIBUTING.md`, the README, the skill and every
  tool docstring are **not** exempt. Comments, tests and workflow files still hold em dashes; none of
  them ship, and sweeping them is a separate decision.
- **Glama's rubric dimension names are quoted verbatim and keep their American spelling** -
  "Behavioral Transparency" in `architecture/tool-descriptions.md`. Renaming an external rubric's
  dimension would misname it. Labelled at the line; do not flag it.
