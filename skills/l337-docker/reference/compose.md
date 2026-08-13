# Docker Compose

Compose v2+ is a CLI **plugin**, invoked `docker compose` (space, not hyphen). The hyphenated
`docker-compose` is the retired v1 Python tool - if only that exists, say so rather than using it;
its flags and project-naming differ.

```bash
docker compose version                                    # confirm it is present
```

## Project selection - get this right first

Every Compose command needs to know *which* project. Three inputs decide it, and `-f`/`-p` are
**global flags that go before the subcommand**:

```bash
docker compose -f ./compose.yaml -p myproj up -d          # correct
docker compose --project-directory ./stack up -d          # resolve relative paths against ./stack
cd ./stack && docker compose up -d                        # cwd-based (project name = dir name)
```

- With no `-f`, Compose walks up from the working directory looking for `compose.yaml`,
  `compose.yml`, `docker-compose.yaml` or `docker-compose.yml`.
- With no `-p`, the project name defaults to the **directory name**, lowercased. Run the same file
  from a different directory and you get a *second, independent* project - the classic cause of
  "up says it created everything but ps shows nothing".
- Multiple `-f` flags merge in order, later overriding earlier - the override-file pattern.
- Relative paths inside the file resolve against the first file's directory unless
  `--project-directory` says otherwise.

Always state which project you are acting on. `docker compose ls -a --format json` lists every
project the daemon knows about, with its config file paths.

## Inspecting before acting

```bash
docker compose config                                     # fully resolved, interpolated YAML
docker compose config --format json | jq
docker compose config --quiet                             # validate only; non-zero on error
docker compose config --services                          # just the names
docker compose config --images
docker compose config --resolve-image-digests             # pin tags to digests
```

`config` is the safe first move on any unfamiliar project: it applies variable interpolation,
merges overrides and normalises the model, so it shows what will *actually* run rather than what
the file appears to say. Review it for `privileged: true`, host bind mounts that escape the
project directory, `network_mode: host`, and secrets sitting in plain `environment:` entries
before bringing anything up.

`--quiet` is the cheap syntax gate in a pipeline - it prints nothing and exits non-zero on a bad
file.

## Bringing up and down

```bash
docker compose up -d                                      # detached
docker compose up -d --wait                               # block until healthy/running
docker compose up -d --wait --wait-timeout 120
docker compose up -d --build                              # rebuild images first
docker compose up -d --force-recreate
docker compose up -d --remove-orphans                     # drop containers for removed services
docker compose up -d --scale web=3
docker compose up -d --dry-run                            # show what would happen
```

**`--wait` is the flag that makes `up` truthful.** Without it, `up -d` returns as soon as
containers are *created*, which is not the same as the application working; with it, Compose
blocks until every service with a healthcheck is healthy and fails non-zero if one does not get
there. Use it whenever you intend to report success.

`--dry-run` is genuinely useful before a risky change - it prints the actions without performing
them.

```bash
docker compose down                                       # stop + remove containers and networks
docker compose down --remove-orphans
docker compose down --rmi local                           # also remove images built here
docker compose down -v                                    # ALSO REMOVE NAMED VOLUMES
```

**`-v`/`--volumes` on `down` destroys the named volumes declared in the file** - databases
included, with no undo. Never add it on your own initiative; confirm it explicitly as data loss
every single time. Plain `down` already removes containers and networks, which is what people
usually mean.

## Per-service lifecycle

```bash
docker compose start [SERVICE...]                         # start existing, stopped containers
docker compose stop  [SERVICE...] [-t 30]                 # graceful
docker compose restart [SERVICE...] [-t 30]
docker compose kill [SERVICE...] [-s SIGTERM]             # immediate
docker compose pause / unpause [SERVICE...]
```

`stop`/`start` keep the containers; `down`/`up` recreate them. When a config change must take
effect, `restart` is **not** enough - it restarts the existing container with its existing
settings. Use `up -d` (which recreates changed services) or `up -d --force-recreate`.

## Status and logs

```bash
docker compose ps                                         # running services
docker compose ps -a --format json | jq -s '.'            # includes exited (NDJSON -> array)
docker compose ps --format json | jq -rs '.[] | [.Service,.State,.Health,.ExitCode] | @tsv'
docker compose ls -a --format json | jq '.'               # already an array - NO -s

docker compose logs --tail 200
docker compose logs --tail 200 --timestamps <service>
docker compose logs --tail 500 --since 30m <service>
docker compose top                                        # processes per service
docker compose port web 80                                # host binding for a container port
docker compose images                                     # image + tag + size per service
```

Same rule as plain containers: **always `--tail`**. Compose interleaves every service's output, so
an untailed `logs` on a multi-service project is several containers' full history at once.

`docker compose ps` output carries `Health` - the fastest read on whether a project is actually up
rather than merely started.

> **`--format json` is not consistent within the Compose plugin** (verified on v5.3.1):
> `docker compose ps` emits **NDJSON** (one object per line, like the core CLI), but
> `docker compose ls` emits a **JSON array**. So `ps` needs `jq -s` and `ls` must not have it -
> using `-s` on `ls` silently produces a nested `[[...]]` that then fails to iterate. Check the
> first character (`{` vs `[`) if unsure.

## Running commands

```bash
docker compose exec <service> <cmd>                       # in the RUNNING container
docker compose exec --index 2 <service> <cmd>             # a specific replica when scaled
docker compose run --rm <service> <cmd>                   # a NEW throwaway container
docker compose run --rm --no-deps <service> <cmd>         # without starting dependencies
```

`exec` needs the service running and shares its state. `run` starts a fresh container from the
same config - right for migrations and one-off jobs, and it starts `depends_on` services unless
you pass `--no-deps`. Always `--rm` on `run`, or you accumulate stopped containers.

Do not pass `-it`; there is no TTY. Add `-T` if a command misbehaves over the non-TTY path.

## Images

```bash
docker compose pull                                       # fetch everything up front
docker compose pull --ignore-pull-failures
docker compose build [--no-cache] [--pull] [SERVICE...]
docker compose build --push
```

Pull before `up` on a first deploy so registry/auth failures surface before any container starts,
rather than half-way through the rollout.

## Copying files

```bash
docker compose cp <service>:/etc/app.conf ./app.conf
docker compose cp ./app.conf <service>:/etc/app.conf
docker compose cp --all <service>:/logs ./logs           # every replica
docker compose cp --index 2 <service>:/logs ./logs
```

Same tar-rooting and trailing-slash semantics as `docker cp` - see `reference/containers.md`.

## Waiting

```bash
docker compose wait <service> [SERVICE...]                # block until they STOP, return exit code
```

Note the direction: `compose wait` waits for containers to **exit**, which suits one-shot jobs. To
wait for a project to come *up*, use `up --wait` instead.

## Compose against Swarm

`docker compose` targets a single daemon and ignores swarm-specific keys (`deploy.replicas`,
`deploy.placement`). To deploy the same file across a swarm cluster, use `docker stack deploy` -
see `reference/swarm.md`, which also lists the Compose keys the swarm orchestrator ignores in the
other direction (`depends_on`, `build`).
