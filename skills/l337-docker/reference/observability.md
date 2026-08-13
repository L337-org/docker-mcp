# Watching state and waiting for it to settle

Two jobs: taking a **bounded snapshot** of what is happening, and **blocking** until something
converges. Both have the same discipline - bound the output, bound the wait, and never report a
timeout as a success.

## Snapshot views

One command each. These are the reads to reach for instead of improvising a `jq` pipeline.

| View | Command |
|---|---|
| all containers, with state and exit code | `docker ps -a --format json \| jq -rs '.[] \| [.Names,.State,.Status,.Image] \| @tsv'` |
| one container's recent output | `docker logs --tail 200 --timestamps <c>` |
| one container's resource usage | `docker stats --no-stream --format json <c>` |
| whole-host resource usage | `docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemPerc}}'` |
| all services, desired vs running | `docker service ls --format json \| jq -rs '.[] \| [.Name,.Mode,.Replicas] \| @tsv'` |
| one service's output | `docker service logs --tail 200 --timestamps <svc>` |
| one service's tasks and failures | `docker service ps <svc> --no-trunc --format json \| jq -rs '.[] \| [.Name,.CurrentState,.Error] \| @tsv'` |
| a rollout in progress | `docker service inspect <svc> --format '{{json .UpdateStatus}}' \| jq` |
| all swarm nodes | `docker node ls --format json \| jq -rs '.[] \| [.Hostname,.Status,.Availability,.ManagerStatus] \| @tsv'` |
| what changed recently | `docker events --since 30m --until 0s --format json \| jq -s '.'` |

Health across the fleet in one pass:

```bash
docker ps -a --format '{{.Names}}' | while read -r c; do
  printf '%s\t%s\t%s\n' "$c" \
    "$(docker inspect "$c" --format '{{.State.Status}}')" \
    "$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}')"
done
```

`docker stats` samples **instantaneously**. One reading is not a trend - take two a few seconds
apart before calling anything a resource problem. `MemPerc` near 100 is the exception: that is an
imminent OOM kill and worth acting on from a single sample.

## Waiting

### Rules

- **Always bound the loop** with a timeout, and make the timeout an argument.
- **Distinguish the three outcomes**: condition met / condition definitively failed / timed out.
  Collapsing "timed out" into either of the others is the failure that produces false success
  reports.
- **Poll every 2-5s**, not every 0.1s. Each poll is a daemon round trip.
- Prefer `docker events` (below) over polling when you are waiting for a discrete event.

### Until a container exits

Built in:

```bash
docker wait <c>            # blocks, prints the exit code
```

It waits for **exit only** - it will block forever on a healthy long-running container, and it has
no timeout flag of its own. To bound it, poll `.State.Status` the way `wait_healthy` does below.
Do not reach for `timeout 300 docker wait ...`: `timeout(1)` is GNU coreutils and is **not present on
stock macOS**, so that form works on Linux and silently fails to run elsewhere.

### Until a container is healthy

There is no built-in equivalent, and this is the wait that actually matters after a deploy:

```bash
wait_healthy() {
  local c="$1" tmo="${2:-120}" elapsed=0 st health
  while [ "$elapsed" -lt "$tmo" ]; do
    st=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null) || {
      echo "FAIL: no such container $c"; return 1; }
    health=$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
    case "$health" in
      healthy)   echo "OK: healthy after ${elapsed}s"; return 0 ;;
      unhealthy) echo "FAIL: unhealthy after ${elapsed}s"; return 1 ;;
    esac
    case "$st" in
      running) [ "$health" = none ] && { echo "OK: running (no healthcheck) after ${elapsed}s"; return 0; } ;;
      exited)  echo "FAIL: exited($(docker inspect "$c" --format '{{.State.ExitCode}}')) after ${elapsed}s"; return 1 ;;
    esac
    sleep 2; elapsed=$((elapsed + 2))
  done
  echo "TIMEOUT: not healthy after ${tmo}s (last st=$st health=$health)"; return 1
}
```

**`health=none` means the image declares no `HEALTHCHECK` - it is not a failure.** Treat
`running` as success in that case, and say in the report that health was not actually verified,
because it was not. Reporting "healthy" for an image with no healthcheck is a false claim.

### Until swarm replicas converge

`docker service update` and `stack deploy` return **before** the rollout finishes. Never report
success from their exit code alone.

```bash
wait_service() {
  local svc="$1" tmo="${2:-300}" elapsed=0 running desired upd
  while [ "$elapsed" -lt "$tmo" ]; do
    running=$(docker service ps "$svc" --filter desired-state=running --format json 2>/dev/null \
              | jq -rs '[.[] | select(.CurrentState | startswith("Running"))] | length')
    desired=$(docker service inspect "$svc" --format '{{if .Spec.Mode.Replicated}}{{.Spec.Mode.Replicated.Replicas}}{{else}}global{{end}}')
    upd=$(docker service inspect "$svc" --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}')
    if [ "$upd" = "paused" ] || [ "$upd" = "rollback_started" ]; then
      echo "FAIL: rollout $upd after ${elapsed}s"; break
    fi
    if [ "$desired" = "global" ] || [ "$running" = "$desired" ]; then
      echo "OK: $running/$desired running after ${elapsed}s"; return 0
    fi
    sleep 5; elapsed=$((elapsed + 5))
  done
  echo "TIMEOUT/FAIL after ${elapsed}s - failing tasks:"
  docker service ps "$svc" --no-trunc --format json | jq -rs \
    '.[] | select(.CurrentState | startswith("Running") | not) | [.Name,.CurrentState,.Error] | @tsv'
  return 1
}
```

Always dump the failing tasks with `--no-trunc` on failure - the `Error` column is the answer and
is truncated by default.

### Until a node is ready

```bash
wait_node() {
  local node="$1" tmo="${2:-120}" elapsed=0 st av
  while [ "$elapsed" -lt "$tmo" ]; do
    st=$(docker node inspect "$node" --format '{{.Status.State}}' 2>/dev/null) || {
      echo "FAIL: no such node"; return 1; }
    av=$(docker node inspect "$node" --format '{{.Spec.Availability}}')
    [ "$st" = "ready" ] && [ "$av" = "active" ] && { echo "OK: ready/active after ${elapsed}s"; return 0; }
    sleep 5; elapsed=$((elapsed + 5))
  done
  echo "TIMEOUT: state=$st availability=$av after ${tmo}s"; return 1
}
```

### Until a registry tag appears

```bash
wait_tag() {
  local ref="$1" tmo="${2:-600}" elapsed=0
  while [ "$elapsed" -lt "$tmo" ]; do
    docker buildx imagetools inspect "$ref" >/dev/null 2>&1 && \
      { echo "OK: $ref published after ${elapsed}s"; return 0; }
    sleep 15; elapsed=$((elapsed + 15))
  done
  echo "TIMEOUT: $ref not published after ${tmo}s"; return 1
}
```

Poll a registry gently - 15s or slower. Registries rate-limit, and a tight loop can exhaust the
Hub pull budget (`reference/registry.md`).

## Event-driven waiting

`docker events` with no `--until` **streams forever and will hang the session**. It must always be
bounded. Three facts about bounding it, all verified:

- **`--until now` is rejected** - "failed to parse value as time or duration". Use a relative
  duration (`--until 0s`) or a Unix epoch integer (`--until $(date +%s)`).
- **A `--until` in the future turns it into a bounded wait**: the command blocks until that
  timestamp, then exits 0. This needs no external tools.
- **`timeout(1)` is not on stock macOS** (it is GNU coreutils; `brew install coreutils` provides
  `gtimeout`). Do not write `timeout 300 docker events ...` and assume it runs everywhere.

Historical window - what already happened:

```bash
docker events --since 30m --until 0s --format json | jq -s '.'
docker events --since 30m --until $(date +%s) --filter event=die --format json
```

Forward-looking window - wait for something to happen, portably:

```bash
docker events --since 0s --until $(($(date +%s) + 300)) \
  --filter type=container --filter event=health_status --format json
```

**This always consumes the full window.** It does not return early on the first match, and piping
to `head -1` does not fix that - the first line prints immediately but the pipeline still blocks
until the window closes (`docker events` only notices the closed pipe on its next write). If you
need to act the moment an event arrives, either use `gtimeout`/`timeout` where it exists, or poll
with one of the loops above.

Use events for "tell me when X happens" and polling for "tell me when X is true". A condition that
may **already** hold is not an event - waiting on the stream for it will block for the whole
window and report nothing.

## Shell portability

These snippets are run under whatever shell is active, commonly **zsh** on macOS:

- **`status` is a read-only variable in zsh.** `local status=...` fails outright with
  `read-only variable: status`. The loops above use `st` for this reason - do not "tidy" it back.
- `timeout` is absent on macOS (see above).
- `seq`, `date` flags and `sed -i` differ between GNU and BSD. Prefer `$(( ))` arithmetic and
  `date +%s`, which behave the same on both.

## Reporting

State what was observed, over what window, and what was not checked:

- "3/3 replicas running, confirmed 40s after the update" - a measurement.
- "Deployed successfully" - an assumption, unless a wait actually returned OK.
- "Container running; the image declares no healthcheck, so application health was not verified" -
  the honest version of the ambiguous case.
