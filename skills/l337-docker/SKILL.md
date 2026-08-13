---
name: l337-docker
description: Drive Docker through the docker CLI - containers, images, networks, volumes, Compose, Swarm services/stacks/nodes/secrets, buildx, Scout, OCI registries, plugins, contexts and remote daemons. Covers inspection, deployment, debugging, security auditing, build/publish and cleanup, with bounded output and a confirmation gate on anything destructive. Use whenever the user wants to run, build, inspect, deploy, debug, monitor or clean up Docker resources, or mentions docker, a container, an image, compose, swarm, a stack, buildx, scout, a registry, a volume or a Dockerfile.
---

# Docker via the CLI

Every operation here goes through the `docker` binary and its plugins. No SDK, no daemon library -
`docker`, its subcommands, and (for registry queries that need no daemon) `curl` against the
registry HTTP API.

This file is the router. It carries the rules that apply to *every* Docker operation plus a map
into `reference/` (how to do a thing) and `workflows/` (multi-step procedures). **Read the one
reference file the task needs - not all of them.**

## Preflight

Run once at the start of a Docker session, before anything else:

```bash
docker version --format '{{.Server.Version}} (API {{.Server.APIVersion}})'   # daemon reachable?
docker context ls --format json                                              # which daemon, and what else is configured
docker system info --format '{{.Name}} {{.OperatingSystem}} {{.Swarm.LocalNodeState}}'
```

If the daemon is unreachable, **say so and stop** - do not fall back to guessing state from
config files. The common causes are Docker Desktop not running (macOS/Windows), the user not
being in the `docker` group (Linux), or `DOCKER_HOST`/`DOCKER_CONTEXT` pointing somewhere dead.

Check plugins only when the task needs one - `compose`, `buildx` and `scout` are separate
binaries and are frequently absent on servers:

```bash
docker system info --format '{{range .ClientInfo.Plugins}}{{.Name}} {{end}}'
```

A missing plugin is a hard stop for that domain, not something to work around by hand-rolling the
equivalent. Say which plugin is missing and what installs it.

## Rules that always apply

**1. Read before you write.** Inspect current state before changing it, and show the user what you
found. Most Docker mistakes are irreversible in the way that matters (a removed volume, a
clobbered tag), and the read costs nothing.

**2. Bound every output.** The CLI streams without limit and will flood the context window. There
is no server truncating for you.

- Logs: always `--tail N` (start at 100-200). Never bare `docker logs`.
- `docker stats`: always `--no-stream`. Without it, it never returns.
- `docker events`: never bare - it streams forever. Bound it with `--until 0s` for history, or a
  future epoch to wait (`--until $(($(date +%s)+300))`). **`--until now` is invalid.**
- Anything large (image save, container export, archives): write to a **file**, never into the
  context. Then report the path and size.
- Projecting fields with `--format` beats piping a full dump through `head`.

**3. Confirm destructive operations, naming the exact target.** Before any `rm`, `prune`, `down
--volumes`, `kill`, `system prune`, `swarm leave`, `stack rm`, or force-tag-overwrite: state
precisely what will be destroyed (count and names, obtained by running the read-only equivalent
first) and wait for the user. "Prune unused images" is not a target; "these 14 images, 7.0 GB" is.

Never pass `-f`/`--force` to satisfy an interactive prompt you were not asked to bypass. Treat
`docker system prune -a` and `docker volume prune` as needing explicit sign-off every time -
volumes hold the only copy of data often enough that this rule has no exceptions.

**4. Never act on the container you are running in.** If this session is itself containerised,
stopping or removing its own container kills the session mid-operation:

```bash
test -f /.dockerenv && echo "in a container: $(cat /etc/hostname)"
```

When that prints, cross-check the id against any container you are about to stop/kill/remove/
restart, and refuse rather than "being careful". Also refuse a `prune` that would sweep it up.

**5. Machine-readable output only.** Never parse the human table format - column widths, `NAMES`
truncation and locale-dependent times all break it. See "The JSON contract" below.

**6. Target the daemon explicitly.** See "Choosing a daemon" below. Never assume the ambient
context is the one the user means when more than one is configured.

**7. Say what you did not check.** A read-only audit that skipped a host, a service or a plugin
reports the gap. Silence reads as "clean".

## The JSON contract

Two different shapes, and mixing them up is the most common parsing bug:

| Command family | Output | Parse with |
|---|---|---|
| `docker <thing> ls --format json` | **NDJSON** - one object per line, *not* a JSON array | `jq -s '.'` to collect, or line-by-line |
| `docker compose ps --format json` | **NDJSON** | `jq -s '.'` |
| `docker compose ls --format json` | a **JSON array** - the odd one out | `jq '.'` (**no** `-s`) |
| `docker inspect` / `docker <thing> inspect` | a **JSON array**, even for one object | `jq '.[0]'` |
| `docker inspect --format '{{json .State}}'` | one JSON value | `jq '.'` |

The Compose plugin is inconsistent with itself, so when in doubt check the first character -
`{` means NDJSON and needs `jq -s`, `[` is already an array and must not have it. Adding `-s` to
an array silently yields a nested `[[...]]` that then fails to iterate.

Second trap: **`ls --format json` stringifies everything and pre-humanises sizes** -
`"Size":"122MB"`, `"Containers":"0"`. Those are display strings, useless for arithmetic or
sorting. When you need real numbers, go through `inspect`, which returns native types:

```bash
docker image ls --format json | jq -s '.[0]'            # "Size":"122MB"   <- string, rounded
docker image inspect <id> --format '{{.Size}}'          # 122125650        <- bytes
```

Useful projections:

```bash
docker ps -a --format json | jq -rs '.[] | [.Names,.State,.Status] | @tsv'
docker inspect <c> --format '{{.State.ExitCode}} {{.State.Health.Status}} {{.RestartCount}}'
docker inspect <c> --format '{{json .HostConfig}}' | jq '{Privileged,NetworkMode,Binds,CapAdd}'
```

`--filter` server-side beats `jq` client-side - it is faster and does not stream what you then
discard: `docker ps --filter status=exited --filter label=app=web`.

## Choosing a daemon

`docker context use <name>` is **modal and global** - it changes the target for every process on
the machine, silently, and outlives the session. Do not use it to run one command somewhere else.

Prefer per-invocation targeting, which is explicit and leaves no residue:

```bash
docker --context prod ps          # a configured context
docker -H ssh://ops@prod ps       # an ad-hoc endpoint
DOCKER_HOST=tcp://10.0.0.5:2376 docker ps
```

`docker --context X` is the right default. Reserve `context use` for when the user explicitly asks
to change their default, and confirm it as the persistent change it is.

When several daemons are in play, **name the host in every report line** - "3 containers running"
is worthless without it. Details, TLS and SSH setup: `reference/system.md`.

## Provenance labels

Stamp every resource you create so it can be found and torn down later without guessing:

```bash
--label l337-docker-skill.managed=true --label l337-docker-skill.created="$(date -u +%FT%TZ)"
```

Applies to `run`, `create`, `network create`, `volume create`, `service create`, `config create`,
`secret create`. It does **not** apply to `image build` - a label changes the resulting image
digest, so builds are deliberately unstamped.

Then `docker ps -a --filter label=l337-docker-skill.managed=true` scopes any later inventory or
cleanup to your own footprint. `workflows/maintenance.md` uses this.

> If `docker-mcp-server` also runs against this daemon, it stamps `docker-mcp-server.managed=true`
> for the same purpose. They are separate footprints - check both before concluding a daemon is
> clean, and never claim one tool's resources as the other's.

## Reference map

Read the one that matches. Each is a command reference with the flags that matter and the traps.

| When the ask is about | Read |
|---|---|
| running/stopping/exec'ing containers, logs, stats, processes, copying files in or out, commit/export, "why did it die" | `reference/containers.md` |
| pulling, building, tagging, pushing, saving/loading images, layer history, sizes | `reference/images.md` |
| networks, connectivity between containers, volumes, mounts | `reference/networks-volumes.md` |
| a `compose.yaml`/`docker-compose.yml` project, multi-service local dev | `reference/compose.md` |
| swarm mode: services, replicas, rolling updates, nodes, stacks, secrets, configs | `reference/swarm.md` |
| multi-platform builds, BuildKit, bake, manifest lists, build cache | `reference/buildx.md` |
| CVEs, SBOMs, base-image recommendations | `reference/scout.md` |
| tags/digests/manifests **without pulling**, Docker Hub metadata and rate limits | `reference/registry.md` |
| daemon info, disk usage, event stream, login/logout, plugins, contexts, remote/TLS/SSH daemons | `reference/system.md` |
| watching state over time, waiting for healthy/converged/available | `reference/observability.md` |
| what the official docs say about an API, a Dockerfile instruction, a Compose key | `reference/docs.md` |

## Workflow map

Multi-step procedures with the decision points and the failure modes worked out. Follow one when
the task matches; do not improvise the sequence.

| Task | Read |
|---|---|
| deploy a container, swap a running container to a new image, bring up a Compose project, deploy a Swarm stack | `workflows/deploy.md` |
| something is broken: one container, a whole host, a Compose project, container-to-container networking, swarm health | `workflows/troubleshoot.md` |
| CVE audit, compare two image versions, pick a safer base, harden running containers, review a Dockerfile | `workflows/security.md` |
| what is eating disk, prune safely, back up/restore a volume, inventory by label, audit configured daemons | `workflows/maintenance.md` |
| multi-arch build, create/inspect a manifest list, migrate off `docker manifest`, pick the right tag to deploy | `workflows/build-publish.md` |

## Coverage

**[MCP_VS_SKILLS.md](https://github.com/L337-org/docker-mcp/blob/main/MCP_VS_SKILLS.md)** maps this
skill against the `docker-mcp-server` MCP surface (159 tools, 30 prompts, the resource endpoints),
compares the two on token cost and capability, and records the handful of things the CLI genuinely
cannot do. Consult it when the question is "is X covered?" or "should I be using the server
instead?" - not during normal work.

## Provenance and licence

Part of **[L337-org/docker-mcp](https://github.com/L337-org/docker-mcp)**, the repo behind the
`docker-mcp-server` MCP server. This skill is that server's CLI-only counterpart, published to
show how far the skill approach goes on its own: no server process, no SDK, nothing beyond
`docker`, `jq` and `curl`.

**Where it stops is enforcement, not coverage.** The rules in this file are instructions - a model
can skip them under pressure, or be talked around by a prompt injection in a container log it just
read. The server's equivalents are refusals in code: read-only and non-destructive modes that
never register the tool, per-host write guards, self-termination protection, bounded tool output,
and multi-daemon targeting pinned at startup. Prefer the server for anything where a guarantee
matters more than simplicity -
[MCP_VS_SKILLS.md](https://github.com/L337-org/docker-mcp/blob/main/MCP_VS_SKILLS.md) sets out the
full comparison, including the cases where this skill is the better choice.

- Issues and contributions: <https://github.com/L337-org/docker-mcp/issues>
- Licence: MIT - see the `LICENSE` file alongside this skill.
- Copyright (c) 2026 Gavin Lucas.

The `l337-docker-skill.*` provenance labels above identify resources created through this skill,
and are what makes its footprint separable from anything else on the daemon - including the
server's own, which uses a different prefix.
