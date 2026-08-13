# Containers

## Listing and inspecting

```bash
docker ps                                              # running only
docker ps -a --format json | jq -s '.'                 # everything, parseable (NDJSON -> array)
docker ps -a --filter status=exited --filter label=app=web
docker ps -a --format json | jq -rs '.[] | [.Names,.State,.Status,.Image] | @tsv'
```

Filters worth knowing: `status=` (created/restarting/running/removing/paused/exited/dead),
`label=`, `name=` (substring, not exact), `ancestor=<image>`, `health=` (starting/healthy/
unhealthy/none), `before=`/`since=<container>`, `exited=<code>`.

```bash
docker inspect <c>                                     # JSON array, even for one container
docker inspect <c> --format '{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}'
docker inspect <c> --format '{{json .State.Health}}' | jq
docker inspect <c> --format '{{json .NetworkSettings.Networks}}' | jq 'keys'
docker inspect <c> --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}} {{.RW}}{{"\n"}}{{end}}'
```

**Health is three-valued, and the third value is not a failure.** `.State.Health` is absent
entirely when the image declares no `HEALTHCHECK`. Absent ≠ unhealthy - treat
`.State.Status == "running"` as success in that case. Only `.State.Health.Status == "unhealthy"`
is a real failure.

## Creating and running

```bash
docker run -d --name web \
  --restart unless-stopped \
  -p 8080:80 \
  -v webdata:/var/lib/app \
  -e KEY=value \
  --network appnet \
  --label l337-docker-skill.managed=true \
  nginx:1.27

docker create --name web ... nginx:1.27      # configure now, start later
docker start web
```

- `-d` (detach) for anything long-running. Without it the CLI attaches and blocks until exit.
- `--rm` for throwaway one-shots so they clean themselves up.
- `--restart unless-stopped` for a service you want back after a reboot; `no` (default) for a
  one-shot. `always` also restarts a container the user deliberately stopped - rarely what is
  wanted.
- Pin the tag. `nginx` means `nginx:latest`, which is a moving target.
- Resource limits are off by default: `--memory 512m --cpus 1.5`. An unlimited container can take
  the host down.
- `-p 8080:80` publishes to the host. Containers on a shared user-defined network reach each other
  on the container port **without** any `-p` - publishing is for host access only.

Run a one-shot and capture output:

```bash
docker run --rm alpine:3.20 sh -c 'echo hi'
```

## Lifecycle

| Intent | Command | Signal / effect |
|---|---|---|
| Graceful stop | `docker stop [-t 30] <c>` | SIGTERM, then SIGKILL after the timeout (default 10s) |
| Immediate stop | `docker kill <c>` | SIGKILL now, no grace period - data loss risk |
| Custom signal | `docker kill -s HUP <c>` | e.g. reload config without stopping |
| Restart | `docker restart [-t 30] <c>` | stop (graceful) then start |
| Freeze / thaw | `docker pause <c>` / `docker unpause <c>` | SIGSTOP; keeps memory, releases CPU |
| Remove | `docker rm <c>` | must be stopped first, unless `-f` |
| Remove + anon volumes | `docker rm -v <c>` | also drops volumes with no name - **data loss** |
| Rename | `docker rename <old> <new>` | frees the old name; nothing else changes |

`docker rm -f` stops and removes in one step. It is a `kill`, not a `stop` - the process gets no
chance to flush. Prefer `docker stop && docker rm` unless the container is already wedged.

Rename is the cheap rollback primitive: rename the old container aside instead of removing it, and
you can rename it back. `workflows/deploy.md` relies on this.

## Logs

```bash
docker logs --tail 200 <c>
docker logs --tail 200 --timestamps <c>
docker logs --since 30m --tail 500 <c>
docker logs --since 2026-08-13T09:00:00 --until 2026-08-13T10:00:00 <c>
docker logs --tail 200 <c> 2>&1 | grep -iE 'error|fatal|panic|refused'
```

**Always pass `--tail`.** A container with a month of output will fill the context window and the
useful part is at the end anyway.

- Logs survive the container stopping - a crash-looping or exited container still has them. This
  is usually the single most informative read during triage.
- `-f`/`--follow` never returns. Only use it with a bounded wait; see `reference/observability.md`.
- stdout and stderr are interleaved; `2>&1` before a pipe to catch both.
- This only works for the `json-file` and `local` log drivers. Under `journald`, `syslog` or a
  remote driver, `docker logs` returns an error or nothing - check
  `docker inspect <c> --format '{{.HostConfig.LogConfig.Type}}'` before concluding the app is
  silent.

## Runtime observation

```bash
docker stats --no-stream                                  # one sample, all running containers
docker stats --no-stream --format json | jq -s '.'
docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.MemPerc}}'
docker top <c>                                            # process tree inside
docker top <c> aux                                        # ps flags passed through
docker diff <c>                                           # filesystem changes vs the image
docker port <c>                                           # published port map
```

**`--no-stream` is mandatory** - without it `docker stats` renders forever and never returns.

The sample is instantaneous, not an average. A single 100% CPU reading is not evidence of
sustained load; take two samples a few seconds apart before concluding anything. `MemPerc` near
100 is the one to act on - that is an imminent OOM kill.

`docker diff` prefixes each path `A` (added), `C` (changed) or `D` (deleted). A large diff on a
stopped container explains disk usage that `docker ps -s` attributes to the writable layer.

## Executing inside a container

```bash
docker exec <c> ls /etc                                   # argv form - preferred
docker exec <c> sh -c 'ls /etc | wc -l'                   # shell needed for pipes/globs/redirection
docker exec -u root <c> <cmd>                             # override the user
docker exec -w /app <c> <cmd>                             # working directory
docker exec -e KEY=val <c> <cmd>                          # extra env for this call
```

Use the **argv form** by default - `docker exec <c> getent hosts db` - so the container's shell
never re-parses your arguments. Reach for `sh -c` only when you genuinely need shell features, and
be aware many minimal images have no `bash` (`sh` usually exists; distroless images have neither).

Do **not** pass `-it` - there is no TTY here and it will fail or hang. `-i` alone is only needed
when piping stdin in.

The container must be running. `docker exec` on a stopped container is an error; to inspect a
stopped container's filesystem use `docker cp` or `docker diff` instead.

## Copying files in and out

```bash
docker cp <c>:/etc/nginx/nginx.conf ./nginx.conf          # out
docker cp ./config.yaml <c>:/app/config.yaml              # in
docker cp <c>:/var/log/app ./applogs/                     # directory, recursive
docker cp <c>:/data - > data.tar                          # out as a tar stream
docker cp - <c>:/ < data.tar                              # in from a tar stream
```

`docker cp` works on **stopped** containers too - that is how you recover files from something
that will not start, and how volume backup works without needing `tar` inside the image
(`workflows/maintenance.md`).

Trailing-slash semantics follow `cp`: `SRC` without a trailing `/.` copies the directory itself;
`SRC/.` copies its contents. Getting this wrong nests a directory one level deeper than intended.

When streaming a tar out with `-`, the archive is rooted at the **last path component** - `docker
cp <c>:/data -` produces a tar whose entries start `data/`. Extracting that at `/` puts things
back in `/data`; extracting it at `/data` produces `/data/data`. This is the single most common
volume-restore bug.

## Turning a container into an image or archive

```bash
docker commit <c> myimage:snapshot                        # container -> image (writable layer)
docker commit -m "msg" -a "name" <c> myimage:snapshot
docker export <c> -o container.tar                        # flat filesystem tar, no layers/history
```

`commit` keeps layers and image config; `export` flattens and discards history, env and entrypoint
(pair it with `docker import`). Neither is a substitute for a Dockerfile - a committed image is not
reproducible. Use them for forensics and rescue, not for building.

Always write `export` to a file with `-o`. Never let a filesystem tar stream into the context.

## Reconfiguring a running container

```bash
docker update --memory 1g --cpus 2 <c>
docker update --restart unless-stopped <c>
```

`docker update` changes resource limits and restart policy **in place, without a restart**. It
cannot change ports, env, mounts, image or network - those need a replacement container, which is
what `workflows/deploy.md` covers.

## Waiting

```bash
docker wait <c>            # blocks until it exits, prints the exit code
```

`docker wait` only waits for **exit**. There is no built-in "wait until healthy" - poll
`.State.Health.Status`. See `reference/observability.md` for the polling loops and for
event-driven waiting.

## Pruning

```bash
docker container prune --filter label=l337-docker-skill.managed=true    # scoped
docker container prune                                             # every stopped container
```

Prune only touches **stopped** containers. A running container that should go needs an explicit
`stop` + `rm`. Always run `docker ps -a --filter status=exited` first and show the user the list
and count before pruning.
