# MCP server vs. agent skill

This repo ships two ways to give an AI agent control of Docker:

- **`docker-mcp-server`** - an MCP server exposing 160 typed tools, 30 prompts and a set of
  resources, over the Docker SDK plus the docker CLI.
- **`l337-docker`** - a Claude Code [agent skill](skills/l337-docker/) that drives the `docker`
  CLI directly, with no server process at all. Download it from the
  [releases page](https://github.com/L337-org/docker-mcp/releases).

## Why the skill exists

Agent skills and MCP servers overlap enough that "should this have been a skill?" is a fair
question to ask of any MCP server, including this one. Rather than assert an answer, we built the
skill properly - every command verified against a real daemon, its shell snippets executed by CI
so they cannot drift - and measured both.

**The skill turned out to be genuinely useful, and you should feel free to use it.** For a single
local daemon and everyday work it covers essentially the same ground, costs nothing to install,
and needs no Python. For some setups it is the better choice, and the numbers below show where.

It is also not a like-for-like replacement, and the gaps are not the ones people usually expect.
They are less about *what* you can do - coverage is near-total - and more about **enforcement,
output handling, and who needs shell access**. This document sets all of that out, including the
places the skill wins.

We have no commercial interest in either. Both are MIT-licensed and free.

## At a glance

| | MCP server | `l337-docker` skill |
|---|---|---|
| **Agent needs shell access** | **No** - calls typed tools; can run with no Bash tool at all | **Yes** - it is Bash all the way down |
| **Needs a local `docker` binary** | No for SDK-backed domains (talks to the socket/TCP/SSH). Compose/buildx/scout need the CLI, or an `ssh://` host to run it on | **Yes**, always |
| **Other prerequisites** | Python ≥3.14 + uv, or a container runtime | None |
| **Works with** | Any MCP client: Claude Code, Claude Desktop, Cursor, Zed, Continue, ... | Claude Code, claude.ai and GitHub Copilot; **no other MCP client loads them** |
| **Ships executable code** | Yes - a Python package you run | **No** - markdown only |
| **Choosing the right operation** | 160 names anchored to the CLI's own structure, and 99% of descriptions name a sibling to prefer or avoid | A router table points at one domain file, which carries prose discriminators and worked examples |
| **Getting the arguments right** | Every argument typed, 79% with an explicit default, **validated before the call runs** | The model composes a shell string; wrong flags surface only when Docker rejects them, at execution |
| **Searching the documentation** | Names are a complete always-in-context index, and `tool_list` answers structured queries (by domain, category or parameter) with a definitive negative; full descriptions are still fetched one at a time, through whatever search the client implements | The whole corpus is files on disk: greppable with standard tools, and readable end to end |
| **Output** | Structured JSON, typed, **capped** with a `truncated` flag | Raw text/NDJSON the model must parse; bounding is a written rule |
| **Read-only / no-destructive mode** | **Enforced** - tools are never registered | A written rule |
| **Per-daemon write protection** | **Enforced**: mark a host `(ro)` for read-only, or `(nd)` for non-destructive, which allows writes but refuses removals, kills and prunes | Not available |
| **Self-termination guard** | **Enforced** | A written rule |
| **Multi-daemon** | Labels resolved and **pinned at startup** | Ambient Docker contexts, which can move mid-session |
| **Runtime auditability** | `tool-catalog` resource reports exactly what is registered | None |
| **Updates** | Version-pinned via `uvx`/image tag | Manual re-download; goes stale silently |
| **Trim the surface to fit the job** | `DOCKER_MCP_SERVER_DISABLE` drops whole domains; read-only / no-destructive drop categories - **and the router and prompts shrink with them** | Not really - the router is one file, though you can delete reference files you'll never use |
| **Token cost, eager client, idle** | **~46,800** at full surface; **~18,200** trimmed to a triage-shaped config. Add up to ~2,400 for a client that also loads prompts, resources and resource templates | **~140** |
| **Token cost, lazy client, idle** | ~1,200 full; ~520 trimmed | ~140 |
| **Token cost, typical task** | **~1,500-2,700** (lazy) | ~5,500-7,800 |
| **Failure mode** | Server can fail to start / resolve deps | Cannot fail to "start"; a wrong command just errors |

Two rows deserve emphasis because they cut in opposite directions and are easy to skim past.

**The agent needs a shell for the skill.** That is the biggest practical difference, and it is not
about Docker at all. A skill is instructions telling the model to run commands, so it only works if
the agent already has Bash - and an agent with Bash can do anything the user can, not just Docker.
With the MCP server the agent can be given Docker capability and *no shell whatsoever*. If you are
building a constrained agent, that distinction is the whole ballgame.

**Skills reach less far than MCP, but they are not Claude-only.** Agent skills are an
[open specification](https://agentskills.io/specification) with more than one implementation.
GitHub Copilot reads them from `.github/skills`, `.agents/skills` or, notably, the very same
`.claude/skills` directory Claude Code uses, with the same required `name` and `description`
frontmatter. This skill is spec-compliant as it stands, so a Copilot user can drop it in and it
works: nothing here is Claude-specific.

MCP still reaches further. Claude Desktop, Cursor, Zed and Continue all speak it, and none of them
load skills. So the honest framing is that skills are narrower *today* and the gap is closing,
rather than that they are one vendor's format.

## Guiding the model to the right call

Unlike the token figures below, this section is reasoned from mechanism and from counting what each
side actually carries. It is **not** an empirical measurement of first-try accuracy, which would
need a proper evaluation harness. Treat it accordingly.

It splits into two jobs that do not come out the same way.

**Picking the operation is closer than you would expect.** The server has the more systematic
machinery: 158 of its 160 descriptions name at least one sibling tool by exact name, 1.9 on
average, saying which to prefer and when. On a lazy client that text is fetched at the exact moment
of choice, usually alongside the siblings it names, so the disambiguation arrives when it is
needed. The skill answers with a router table pointing at one domain file, plus 46 explicit
"prefer this over that" passages and 446 worked command lines.

What closes the gap is prior knowledge. `docker ps -a` is one of the most widely documented
commands in existence and a model reproduces it without reading anything, whereas
`container_list(all=True)` is a bespoke API it has never seen. The server hedges against this
deliberately, which is why its naming convention is anchored to the CLI's own structure
(`docker container ls` becomes `container_list`) rather than being invented freely. Call this one
roughly level, with the server ahead on unusual or easily-confused operations and the CLI ahead on
the common ones.

**Getting the arguments right is where the server is properly ahead**, and this is the substantial
half. Across 594 parameters, every one carries a declared type, 79% carry an explicit default and
123 are marked required. That schema is enforced by the client before the call reaches the server,
so a wrong argument name or type is rejected as a validation error with nothing executed. The skill
composes a shell string, and a wrong flag is caught by Docker itself at execution, which may be
after a side effect has already happened.

This document contains its own evidence. Every item in [Verified during
authoring](#verified-during-authoring) is an argument-level error in hand-written CLI that survived
review and was caught only by running it: `--until now` rejected outright, `timeout` absent on
macOS, `status` fatal as a variable name in zsh, `jq -s` applied to something that was already an
array. A JSON Schema makes that entire class of mistake unrepresentable. Nothing in the skill can,
because the CLI has no machine-readable description of its own flags.

One honest qualification: only 13 of those 594 parameters carry an `enum`. The other 98% have their
legal values documented in prose on both sides, so the server's advantage is mostly over argument
names, types and requiredness rather than over the set of legal values.

Where the values *are* constrained it matters more than it sounds, because a wrong CLI value is not
always rejected. `docker scout cves --only-severity CRITICAL` exits 0 and reports "No vulnerable
packages detected" for an image that `--only-severity critical` reports three critical CVEs for:
severity is matched case-sensitively, a non-matching value filters every finding out, and the
empty result is worded as a clean scan. `--exit-code` does not catch it either, since it counts
what survived the filter. `docker network create --scope bogus` likewise succeeds, recording the
unrecognised scope on the network.

On the server both are schema-validation errors before anything runs. The skill can only warn, as
its Scout reference does, and a warning is advisory where a schema is not. So the argument
advantage is less "the server catches bad flags sooner" than "some bad values are never caught at
all, and only a schema can make them unrepresentable".

**Searching splits, rather than one side simply winning.** The skill is plain files on disk, so a
model can grep the entire corpus in one command and read a whole file when it wants breadth.
Nothing on the server matches that: full descriptions are reachable only through the protocol, one
tool at a time, using whatever search the client implements.

What the server answers instead are the questions no text search over descriptions can express,
because they are properties of the live surface rather than words in it. `tool_list` filters by
domain, by category and by parameter name, so "which of these destroy data" and "which accept a
`host`" are single calls rather than an inference from prose. Being generated from what actually
registered, it also reports the surface *this* server has, not the one the documentation describes:
a disabled domain's tools are absent, and `hidden_by_configuration` says how many were withheld. And
an empty result is a definitive negative - `matched: 0` means the capability is not there, which a
fuzzy search over descriptions can never assert. A domain briefing is the everyday use: one line per
tool for an unfamiliar area, at roughly a tenth of what fetching every definition in it costs.

The skill can reach none of those. Its files describe what the CLI can do, not what is enabled here,
and grep returns matching lines rather than an authoritative "no".

**Net:** clearly the server on arguments, roughly level on choosing the operation, and split on
searching - the skill for free-text breadth over the whole corpus, the server for structured
questions about the live surface. The server's advantage is narrower than it first appears and
concentrated in argument correctness.

## Token usage

Measured, not estimated - tool definitions serialized exactly as a client receives them in
`tools/list`, and skill files as loaded from disk. Counted with `tiktoken` (`cl100k_base`), which
is not Claude's tokenizer, so treat the absolute numbers as ±10-15% and the **ratios** as the
finding.

### Eager and lazy clients, and why it decides everything below

An MCP client chooses how much of a server it puts in front of the model, and the two strategies
differ by orders of magnitude.

**Eager loading** sends every tool definition at the start of the conversation: all 160 names,
descriptions and JSON schemas, present whether or not Docker ever comes up. **Lazy loading**
advertises only the tool *names* plus the server's `instructions` string, and fetches a tool's full
definition at the moment the model reaches for it. Claude Code is a lazy client. Several others,
including Claude Desktop today, still load eagerly.

**The trend is firmly towards lazy loading**, and it is the single biggest variable in this
document. As a client adopts it, the server's idle cost falls by roughly 40x and most of the
skill's advantage goes with it. So read the column that matches your client, and treat the eager
numbers as a snapshot of a situation that is improving rather than a permanent property of MCP.

The skill loads in a coarser unit: whole markdown files. The router loads when the skill triggers,
and a reference file loads when a task needs that domain. There is no equivalent of fetching a
single tool, and no client-side strategy that changes it.

### Configuring the server down

The server's surface is not fixed, and this matters more than it first appears. Three switches trim
it, and they compose:

- **`DOCKER_MCP_SERVER_DISABLE=<domains>`** drops whole feature areas: `swarm`, `scout`, `compose`
  and so on.
- **`DOCKER_MCP_SERVER_READONLY=1`** registers only read-only tools.
- **`DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1`** registers everything except the destructive ones.

All three drop tools from registration rather than refusing them at call time, so they are a
footprint lever and a safety control at once. And because the router is generated from what
actually registered, **the instructions and prompts shrink with the tools** rather than continuing
to describe things that are no longer there.

The range between the loosest and tightest usable configuration is wide:

| Config | Tools | Eager idle | Lazy idle |
|---|---|---|---|
| Full whack: everything enabled | 160 | 46,819 | 1,198 |
| Read-only, all domains | 73 | 19,464 | 905 |
| **Triage config** (below) | **63** | **18,212** | **523** |
| Core only: `containers` + `system` | 37 | 10,920 | 391 |
| Floor: core, read-only | 16 | 4,927 | 321 |

The eager column is **tools plus the router**: what every eager client pays. Prompts, resources and
resource templates are counted separately, because each is fetched by a separate call and only by a
client that supports it - see [Idle](#idle---loaded-but-not-used).

**On an eager client that spread is 4,927 to 46,819 tokens, roughly 9.5x**, which makes trimming
the single biggest lever available to you. **On a lazy client the same spread is 321 to 1,198: the
whole saving is under 900 tokens**, so there is little to gain from trimming for footprint alone.
On a lazy client, configure the surface for safety or clarity and treat any context saving as
incidental.

<a id="the-triage-config"></a>
The "trimmed" columns throughout this document mean one specific configuration: a realistic
"monitor, triage and resolve a failed container" setup that keeps `containers`, `images`,
`networks`, `volumes` and `system`, and drops the other twelve domains. Nothing else is changed,
so all categories (read-only, mutating and destructive) remain registered for the domains that are
kept:

```
DOCKER_MCP_SERVER_DISABLE=compose,stack,swarm,services,nodes,secrets,configs,buildx,scout,registry,plugins,context
```

That is a **2.5x** cut, with the router falling from 640 tokens to 315 as it stops advertising
absent domains, and the prompts from 30 to 16. The trade is real, though: a disabled tool is not
merely hidden, it is *gone*. If the triage turns out to need `compose_logs`, you have to change the
config and restart, whereas the skill always has every recipe available at no idle cost.

### Idle - loaded but not used

| | MCP (eager, full) | MCP (eager, triage config) | MCP (lazy, full) | Skill |
|---|---|---|---|---|
| Always in context | all 160 tool defs + router | 63 tool defs + router | router + tool names | name + description |
| | 46,179 (tools) | 17,897 (tools) | 640 (router) | 136 |
| | 640 (router) | 315 (router) | 558 (names) | |
| **Floor - every eager client** | **~46,800 tok** | **~18,200 tok** | **~1,200 tok** | **~140 tok** |
| *plus, for a client that loads them:* | | | | |
| prompts | +1,113 (30) | +596 (16) | - | - |
| resources | +739 (6) | +739 (6) | - | - |
| resource templates | +554 (5) | +554 (5) | - | - |
| **Ceiling - all of them** | **~49,200 tok** | **~20,100 tok** | **~1,200 tok** | **~140 tok** |

"Triage config" here and below means the `DOCKER_MCP_SERVER_DISABLE` line in
[Configuring the server down](#the-triage-config): `containers`, `images`, `networks`, `volumes`
and `system` kept, the other twelve domains dropped.

**Why a floor and a ceiling rather than one number.** A server advertises itself across four
calls - `tools/list`, `prompts/list`, `resources/list` and `resources/templates/list` - and a
client pays only for the ones it implements. Every eager client fetches tools, so that plus the
router is the floor; the other three are conditional, and between them they span ~2,400 tokens.
That is 5% at full surface but **36% of the `floor` configuration**, where your client's feature
support matters more than some of the config switches. Quoting a single blended total would
state a figure for a client we have not identified, so both ends are given and the figures
elsewhere in this document use the floor.

One honest limit on the ceiling: we have confirmed what the protocol advertises, not that a
client which supports resources puts those listings into the model's *context* rather than into
its own attachment picker. So treat the conditional rows as an upper bound on what the model is
charged for. Settling it needs an instrumented client, not more arithmetic.

This is still the skill's strongest result. On a client that eagerly loads every tool, the server
at full surface costs roughly **46,800 tokens of every conversation** whether or not Docker comes
up - around a third of a 128k window before you have said anything. Trimming to the triage config
cuts that to ~18,200 - a large and genuine saving, though still around 130 times what the skill
costs to sit installed.

On a lazy client the server's idle cost drops ~40x to ~1,200 (or ~520 trimmed), and the gap
narrows to something most people would not notice either way.

### In use - the cost of actually doing something

| Task | MCP (lazy, full) | MCP (lazy, triage cfg) | MCP (eager, full) | MCP (eager, triage cfg) | Skill |
|---|---|---|---|---|---|
| List containers (one-off) | 1,546 | **871** | 46,819 | 18,212 | 5,549 |
| Triage a crashed container | 2,584 | **1,909** | 46,819 | 18,212 | 7,812 |
| Bring up a Compose project | 2,641 | n/a¹ | 46,819 | n/a¹ | 6,489 |

¹ Compose is disabled in the triage config (`containers`, `images`, `networks`, `volumes`,
`system` only), which is the point: a trimmed surface is trimmed for a purpose, and a task outside
it needs a different one.

**Here the result reverses, and the MCP server wins on a lazy client.** A tool definition is small
- median 254 tokens, range 117-1,245 - so fetching the five tools a triage needs costs ~1,400 on
top of the ~1,200 baseline (or ~520 trimmed). The skill has to load its router (3,009) plus a
domain reference (~1,900) plus often a workflow (~2,300), because prose cannot be fetched a
paragraph at a time.

Note the eager+trimmed column never beats the skill on these tasks - 18,212 against 5,549-7,812 -
but it is the difference between "too expensive to leave installed" and "fine". If you are on an
eager client and want the server, disabling the domains you do not use is the single highest-value
change available.

So the honest summary is:

- **Eager-loading client** → the skill is dramatically cheaper: ~6-8x on a real task at full
  surface, ~2.3-3.3x even against a trimmed server, and ~130-340x at idle.
- **Lazy-loading client** → the server is cheaper in use, by ~2.5-3.5x at full surface and ~4-6.4x
  when trimmed, and both are cheap at idle.
- **Docker rarely comes up in your work** → the skill, decisively; it costs ~140 tokens to have
  installed and you may never pay more.
- **Docker is most of what you do, on a lazy client** → the server. Trim the surface if you want
  the control, but not for the tokens: on a lazy client that saving is negligible.
- **Docker is most of what you do, on an eager client** → the server, trimmed to the domains you
  actually use. Here the trimming is what makes it viable rather than merely tidier.
- **Eager client and you want everything available** → the skill, unless you need something only
  the server enforces.

Worst case for the skill is ~32,400 tokens if a single task somehow needed every reference and
workflow file at once; in practice a task touches one or two.

## Where each one genuinely wins

**Reasons to prefer the skill**

- Nothing to install or run beyond `docker` itself - no Python, no uv, no container, no server
  process, no dependency resolution.
- Ships no executable code. It is markdown; you can read every command it will ever run before
  installing it. The supply-chain surface is a text file.
- Far cheaper on eager-loading clients, and near-free when idle on any client.
- Transparent and hackable: disagree with a recipe, edit the markdown.
- Fewer moving parts of its own: no server process to crash, no dependency set to resolve, no
  startup to fail. It is not dependency-free, though. It needs a working local `docker` CLI for
  everything, which is precisely the dependency the server can often do without.

**Reasons to prefer the MCP server**

- The agent needs no shell. This is the one that matters most for constrained or shared agents.
- Works on a machine with no Docker installed, managing remote daemons over SSH/TLS/TCP.
- Works in any MCP client, not just Claude's.
- Safety modes that actually refuse rather than ask nicely - read-only, no-destructive, per-daemon
  `(ro)`/`(nd)`, self-termination protection.
- Output is bounded at the source, so a chatty container cannot blow the context window.
- Structured, typed returns; no parsing traps (see "Structural gaps" below for the ones the skill
  documents precisely because they bite).
- Multi-daemon targeting pinned at startup, so a stray `docker context use` cannot silently move
  what "production" means mid-session.
- Cheaper per task on a lazy client.
- **The surface is configurable.** Disable the domains you do not use and the tools, prompts *and*
  router shrink together - a triage-shaped config is 63 tools and ~18,200 eager tokens instead of
  160 and ~46,800. The skill has no equivalent lever beyond deleting reference files by hand.
- Auditable at runtime - one resource reports exactly which tools are registered under the current
  configuration, so you can confirm what a given config actually exposes rather than inferring it.

**Reasons that apply to neither as much as you'd think**

- *Capability.* Coverage is near-total in both directions; see the mapping below. If a task is
  possible at all it is possible in both, give or take the handful of registry queries that need
  `curl` in the skill.
- *Speed.* Both end up talking to the same daemon. The CLI adds process-startup overhead per call;
  the server adds a JSON-RPC round trip. Neither is the bottleneck.
- *Safety from a determined agent.* Against a **careless** one the server genuinely helps: a tool
  that was never registered cannot be reached by accident, and that is most of what goes wrong in
  practice. Against a determined one, neither holds. The server narrows what is reachable; it does
  not make `docker rm` safe.

## Picking one, in practice

Three situations where the answer is clear, and they are mostly about the client rather than the
Docker work:

- **Occasional Docker use from an eager-loading client, Claude Desktop being the common case.**
  Use the skill. Paying ~46,800 tokens of every conversation for a capability you reach for once a
  fortnight is a bad trade, and ~140 is not. This is the skill's strongest case by a distance, and
  it is worth being clear that **it is a case created by the client, not by the skill being
  better**. If and when Claude Desktop moves to lazy loading, the idle cost drops to ~1,200 and
  this narrows sharply, to genuinely rare use only.
- **Regular Docker work from a lazy-loading client, Claude Code being the common case.** Use the
  server. It is already cheaper per task, and the guarantees come free at that point: bounded
  output, enforced read-only where you want it, structured returns instead of parsing NDJSON.
- **Several environments with different rules: production, staging, a developer box.** Use the
  server, and it is not close. Name each daemon, mark production `(ro)` or `(nd)`, and those
  refusals are enforced at the call boundary with the endpoints pinned at startup so nothing moves
  mid-session. Add the runtime tool-catalog to confirm what a given configuration actually exposes.
  The skill has no answer here at all: it has one ambient Docker context, no notion of a
  per-environment policy, and its safety rules are text a model is asked to follow.

---

# Detailed mapping

**Everything from here on is written from the skill's point of view.** It takes the MCP server's
surface as the reference: 160 tools, 31 prompts and the resource endpoints, in the server's own
categories, and for each one records what the skill does instead. It is a coverage record for the
skill, not a description of the server, so a row saying "no CLI equivalent" is a statement about
the skill's limits and never about the server's.

Read it to answer "is X covered?" or "how would the skill do X?". It is not needed to choose
between the two, which is what everything above is for.

Legend: **✓** direct CLI equivalent; **≈** covered by a documented recipe (loop, `curl`, template);
**-** no equivalent, see [Structural gaps](#structural-gaps).

## Tools

### containers (25) - `reference/containers.md`

| Tool | CLI |
|---|---|
| container_list ✓ | `docker ps -a --format json` |
| container_run ✓ | `docker run -d` |
| container_create ✓ | `docker create` |
| container_start / stop / restart ✓ | `docker start` / `stop` / `restart` |
| container_pause / unpause ✓ | `docker pause` / `unpause` |
| container_kill ✓ | `docker kill [-s SIG]` |
| container_remove ✓ | `docker rm` |
| container_rename ✓ | `docker rename` |
| container_inspect ✓ | `docker inspect` |
| container_logs ✓ | `docker logs --tail N` |
| container_stats ✓ | `docker stats --no-stream` |
| container_top ✓ | `docker top` |
| container_diff ✓ | `docker diff` |
| container_exec ✓ | `docker exec` |
| container_commit ✓ | `docker commit` |
| container_export ✓ | `docker export -o` |
| container_update ✓ | `docker update` |
| container_prune ✓ | `docker container prune` |
| container_archive_get ✓ | `docker cp <c>:<path> -` |
| container_archive_get_to_file ✓ | `docker cp <c>:<path> <dest>` |
| container_archive_put ✓ | `docker cp <src> <c>:<path>` |
| container_wait (exit) ✓ | `docker wait` |
| container_wait (healthy) ≈ | `wait_healthy` loop, `reference/observability.md` |

### images (15) - `reference/images.md`

| Tool | CLI |
|---|---|
| image_list ✓ | `docker image ls` |
| image_pull / push ✓ | `docker pull` / `push` |
| image_build ✓ | `docker build` (or `buildx build`) |
| image_tag ✓ | `docker tag` |
| image_remove ✓ | `docker image rm` |
| image_history ✓ | `docker history` |
| image_inspect ✓ | `docker image inspect` |
| image_save / load ✓ | `docker save -o` / `load -i` |
| image_import ✓ | `docker import` |
| image_prune ✓ | `docker image prune` |
| image_prune_builds ✓ | `docker builder prune` (`--reserved-space`, ex-`--keep-storage`) |
| image_search ✓ | `docker search` (Hub only) |
| image_registry_data ✓ | `docker buildx imagetools inspect` |

### networks (7) / volumes (5) - `reference/networks-volumes.md`

All ✓: `docker network create/ls/inspect/connect/disconnect/rm/prune`,
`docker volume create/ls/inspect/rm/prune`.

### compose (21) - `reference/compose.md`

All ✓ - each maps to the identically-named `docker compose <sub>`: `up`, `down`, `ps`, `ls`,
`logs`, `build`, `pull`, `config`, `cp`, `exec`, `run`, `start`, `stop`, `restart`, `kill`,
`pause`, `unpause`, `port`, `top`, `images`, `wait`. (`compose_list` → `docker compose ls`.)

### swarm (10) / services (10) / nodes (5) / secrets (4) / configs (4) / stack (5) - `reference/swarm.md`

| Tool | CLI |
|---|---|
| swarm_init / join / leave / update ✓ | `docker swarm init` / `join` / `leave` / `update` |
| swarm_join_tokens ✓ | `docker swarm join-token <worker\|manager>` |
| swarm_unlock / unlock_key ✓ | `docker swarm unlock` / `unlock-key` |
| swarm_inspect ✓ | `docker info --format '{{json .Swarm}}'` |
| swarm_task_list ≈ | `docker service ps $(docker service ls -q)`, `reference/swarm.md` - no CLI command lists the cluster's tasks in one shot |
| swarm_task_inspect ✓ | `docker inspect --type task` |
| service_create / update / remove / logs / ps / inspect / list ✓ | `docker service <sub>` |
| service_scale ✓ | `docker service scale` |
| service_rollback ✓ | `docker service rollback` |
| service_wait ≈ | `wait_service` loop, `reference/observability.md` |
| node_list / inspect / update / remove ✓ | `docker node <sub>` |
| node_wait ≈ | `wait_node` loop |
| secret_* / config_* ✓ | `docker secret <sub>` / `docker config <sub>` |
| stack_deploy / list / ps / remove / services ✓ | `docker stack <sub>` |

### buildx (13) - `reference/buildx.md`

All ✓: `docker buildx build/bake/create/rm/use/ls/inspect/du/prune`,
`imagetools create/inspect`, `history ls/inspect`.

### scout (5) - `reference/scout.md`

All ✓: `docker scout cves/quickview/compare/recommendations/sbom`.

### context (5) - `reference/system.md`

All ✓: `docker context create/ls/inspect/rm/use`.

### plugins (11) - `reference/system.md`

All ✓: `docker plugin install/ls/inspect/enable/disable/set/upgrade/rm/push/create`.
`plugin_configure` → `docker plugin set`.
`plugin_privileges` ≈ the privileges `docker plugin install` prints in its interactive grant prompt -
**no read-only CLI equivalent**, so the skill can only show them by starting an install and declining
it; the tool reads them without installing anything.

### registry (7) - `reference/registry.md`

| Tool | Equivalent |
|---|---|
| registry_manifest ✓ | `docker buildx imagetools inspect --raw` |
| registry_image_config ≈ | registry API: manifest → config blob (two hops, documented) |
| registry_tags ≈ | `curl` `/v2/<repo>/tags/list` - **no CLI equivalent** |
| hub_tags ≈ | `curl` Hub API `?ordering=last_updated` |
| hub_repo_info ≈ | `curl` Hub API repository endpoint |
| hub_rate_limit ≈ | `curl -I` `ratelimitpreview/test` - **no CLI equivalent** |
| registry_tag_wait ≈ | `wait_tag` poll loop |

### system (10) - `reference/system.md`

| Tool | CLI |
|---|---|
| system_version ✓ | `docker version` |
| system_info ✓ | `docker system info` |
| system_ping ✓ | `docker version --format '{{.Server.Version}}'` (fails if unreachable) |
| system_df ✓ | `docker system df [-v]` |
| system_events ✓ | `docker events --since ... --until 0s` |
| system_login / logout ✓ | `docker login --password-stdin` / `docker logout` |
| host_list ≈ | `docker context ls` (see [Structural gaps](#structural-gaps)) |
| system_close / system_reconnect - | not applicable: the CLI is stateless per invocation |

### uncategorised (2)

| Tool | Equivalent |
|---|---|
| docs_lookup ≈ | `docker <cmd> --help` + the URL table in `reference/docs.md` |
| tool_list ✗ | no equivalent - see below |

`tool_list` has no CLI counterpart because it does not describe Docker, it describes *this server*:
which tools registered under the current configuration, and their domain, category and one-line
summary. The nearest skill equivalent is grepping `reference/` for a command, which answers a
different question - what the CLI can do, rather than what is enabled here - and cannot report a
category or an authoritative "nothing matches".

## Prompts (31 defined; 30 registered on a single-host server)

`survey_hosts` registers only when two or more daemons are configured, which is why the measured
figures above say 30.


| Prompt | Covered by |
|---|---|
| deploy_container, migrate_container | `workflows/deploy.md` |
| plan_compose_stack, deploy_compose_project | `workflows/deploy.md` |
| deploy_swarm_stack | `workflows/deploy.md` |
| troubleshoot_container, triage_incident, monitor_container_fleet | `workflows/troubleshoot.md` |
| troubleshoot_compose_project, debug_container_networking | `workflows/troubleshoot.md` |
| audit_swarm_health | `workflows/troubleshoot.md` |
| audit_container_security, review_dockerfile | `workflows/security.md` |
| audit_image_cves, compare_image_versions, recommend_base_image | `workflows/security.md` |
| clean_environment, prune_managed, investigate_disk_usage | `workflows/maintenance.md` |
| inspect_stack, backup_volume, restore_volume | `workflows/maintenance.md` |
| audit_docker_contexts, survey_hosts | `workflows/maintenance.md` |
| plan_multiarch_build, inspect_multiarch_manifest | `workflows/build-publish.md` |
| create_multiarch_manifest, migrate_from_docker_manifest | `workflows/build-publish.md` |
| find_latest_image_tag | `workflows/build-publish.md` |
| lookup_docker_docs, verify_docker_method | `reference/docs.md` |

Prompts are *invoked* in MCP and *read* in the skill - the trade is losing the explicit
slash-command entry point in exchange for the router selecting a workflow automatically.

## Resources

| Resource | Equivalent |
|---|---|
| `docker://containers` | `docker ps -a --format json` |
| `docker-logs://{id}` | `docker logs --tail N` |
| `docker-stats://{id}` | `docker stats --no-stream --format json` |
| `docker://services` | `docker service ls --format json` |
| `service-logs://{id}` | `docker service logs --tail N` |
| `service-tasks://{id}` | `docker service ps --no-trunc` + `inspect .UpdateStatus` |
| `docker://nodes` | `docker node ls --format json` |
| `docker-docs://{section}` | `reference/docs.md` URL table |
| `docker-mcp://hosts` | `docker context ls` |
| `docker-mcp://tool-catalog` | n/a - describes the MCP server itself |

All snapshot commands are collected in the table at the top of `reference/observability.md`.

## Structural gaps

Everything above is a command difference. These are differences in *kind*, and they are the
honest answer to "is this equivalent?" - it is not.

**1. Guards are conventions in the skill, not enforcement.** The server refuses at the call
boundary: `DOCKER_MCP_SERVER_READONLY` and `_NO_DESTRUCTIVE` drop tools from the surface entirely,
per-host `(ro)`/`(nd)` markers reject writes, and a self-termination guard blocks acting on the
server's own container. A skill has no call boundary - its rules are instructions a model can
misread, skip under pressure, or lose to a prompt injection in a container log it just read.
**Where a hard guarantee is needed, that is the reason to use the MCP server.** The skill
deliberately declares no tool-permission frontmatter (`tools` / `disallowedTools`, nor the
`allowed-tools` spelling slash commands use), so the host's own permission prompts stay in force on
every destructive command rather than being pre-approved wholesale.

**2. Output is unbounded in the skill.** The server caps output and streams large payloads to
files. In the skill, `docker logs` on a chatty container will fill the context window. Mitigated by
the `--tail` / `--no-stream` / `--until` discipline in SKILL.md - again a convention, not a limit.

**3. Multi-daemon targeting is weaker in the skill.** The server resolves and **pins** each host
label at startup, so a mid-session `docker context use` cannot silently move a target, and SDK and
CLI provably agree. Contexts are ambient and mutable, and `DOCKER_HOST` silently overrides them.
The mitigation is per-invocation `docker --context <name>` and naming the host in every report line.

**4. No SSH fallback for missing local plugins.** The server routes CLI-backed calls to an `ssh://`
host when a plugin is missing locally. In the skill a missing `compose`/`buildx`/`scout` plugin is a
hard stop - run the command on the remote host yourself, or install the plugin.

**5. Structured returns.** MCP tools return typed JSON. In the skill everything is text that must be
parsed, with the NDJSON-vs-array and stringified-sizes traps documented in SKILL.md. Those traps are
real: see below.

**6. Provenance labels differ.** The skill stamps `l337-docker-skill.managed=true`; the server
stamps `docker-mcp-server.managed=true`. Separate footprints - check both before calling a daemon
clean, and never report one as the other. The two can safely coexist on one daemon.

## Verified during authoring

Every claim in the skill was checked against a real daemon - Docker 29.7.2 / API 1.55, compose
v5.3.1, buildx v0.36.0, scout v1.24.0. These are the findings that corrected a draft, and they are
a fair sample of what "parse the CLI's output" costs in practice:

- `--format json` is NDJSON (one object per line), while `inspect` returns an array; `ls`
  stringifies and rounds sizes (`"122MB"`) where `inspect` gives bytes (`122125650`).
- **The Compose plugin is inconsistent with itself**: `docker compose ps --format json` is NDJSON
  but `docker compose ls --format json` is a JSON array, so `jq -s` is required for one and wrong
  for the other.
- `docker swarm init --help` is shadowed by the `docker-init` CLI plugin; the command itself parses
  and runs correctly - only the help output is wrong.
- **`docker events --until now` is rejected**; `--until 0s` and `--until <epoch>` work. A *future*
  `--until` is a portable bounded wait, but always consumes the full window - `head -1` does not
  make it return early.
- `timeout(1)` is **not** on stock macOS, so `timeout ... docker events` is not portable.
- `status` is a **read-only variable in zsh**, so `local status=...` fails outright - the wait loops
  use `st`.
- Volume backup/restore round trip confirmed end to end: `docker cp` works on a
  created-but-never-started container; the archive is rooted at `data/`; extracting at `/data`
  instead of `/` produces `/data/data/...`.
- The registry recipes (Hub token, tags list, manifest media types, `unknown/unknown` attestation
  entries, config-blob fetch, rate-limit headers, GHCR anonymous token) were each run successfully.

`tests/test_skill.py` and `tests/integration/test_skill.py` keep these honest: the integration
tests extract the skill's shell snippets from its markdown and execute them, so a documented
snippet cannot drift from what the CLI actually does.
