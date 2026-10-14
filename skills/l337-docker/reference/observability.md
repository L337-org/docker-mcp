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
| one service's tasks and failures | `docker service ps <svc> --no-trunc --format json \| jq -rs '.[] \| [.ID,.Name,.CurrentState,.Error] \| @tsv'` |
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
- **Distinguish the three outcomes**: condition met / condition can no longer be met / timed out.
  Collapsing "timed out" into either of the others is the failure that produces false success
  reports. The loops below print `OK`, `FAIL` or `NOT MET`, or `TIMEOUT`, and return 0 only on `OK`
  (`wait_service` adds a fourth, `ROLLED BACK`).
- **Keep the timeout under the host's limit on one shell command.** A loop the host kills never
  prints `TIMEOUT`, so a timeout becomes indistinguishable from a hang. The loops default to 110 s,
  chosen to sit under a common two-minute limit; for a longer wait, run the loop several times.
- **Point a loop at a daemon with `DOCKER_CONTEXT=<name>`** before the call -
  `DOCKER_CONTEXT=prod wait_service web` - since the loops call `docker` without `--context`. It
  applies to that call only, in bash and zsh. `DOCKER_HOST`, if set, overrides it.
- **Poll every 2-5s**, not every 0.1s. Each poll is a daemon round trip.
- Prefer `docker events` (below) over polling when you are waiting for a discrete event.
- **Define a loop in the same command that calls it.** Each is a shell function; where the shell
  does not keep state between commands, paste the definition and the call into one command.

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
  local c="$1" tmo="${2:-110}" elapsed=0 st health
  while [ "$elapsed" -lt "$tmo" ]; do
    st=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null) || {
      echo "FAIL: no such container $c"; return 1; }
    health=$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
    case "$health" in
      healthy)   echo "OK: healthy after ${elapsed}s"; return 0 ;;
      unhealthy) echo "FAIL: unhealthy after ${elapsed}s"; return 1 ;;
    esac
    case "$st" in
      running) [ "$health" = none ] && {
        echo "NOT MET: running, but the image declares no healthcheck, so health cannot be observed"
        return 1; } ;;
      exited|dead) echo "FAIL: $st($(docker inspect "$c" --format '{{.State.ExitCode}}')) after ${elapsed}s"
                   return 1 ;;
    esac
    sleep 2; elapsed=$((elapsed + 2))
  done
  echo "TIMEOUT: not healthy after ${tmo}s (last st=$st health=$health)"; return 1
}
```

**`health=none` means the image declares no `HEALTHCHECK`.** The loop returns at once with
`NOT MET` rather than `OK`, because "healthy" was never observed and cannot be. If running is all
the user needs, say so in the report: "running; the image declares no healthcheck, so application
health was not verified".

### Until a line appears in a container's logs

For "wait until the server says it is listening", where there is no healthcheck to watch:

```bash
wait_log() {
  local c="$1" pattern="$2" tmo="${3:-110}" elapsed=0 st line
  while [ "$elapsed" -lt "$tmo" ]; do
    line=$(docker logs --tail 1000 "$c" 2>&1 | grep -F -- "$pattern" | tail -1)
    [ -n "$line" ] && { echo "OK: after ${elapsed}s: $line"; return 0; }
    st=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null) || {
      echo "FAIL: no such container $c"; return 1; }
    case "$st" in
      exited|dead) echo "NOT MET: container $st without logging the pattern"; return 1 ;;
    esac
    sleep 2; elapsed=$((elapsed + 2))
  done
  echo "TIMEOUT: pattern not seen after ${tmo}s"; return 1
}
```

`grep -F` matches the pattern as plain text. Use `grep -E` only for a pattern you wrote yourself:
the logs are the container's output, and a pattern is run over all of them on every poll.

### Following logs for a bounded time

Following logs with `--follow` never returns on its own, and a future `--until` does not fix
that: it ends the follow only when a later line arrives, so on a quiet container it still blocks.
Run the follow in the background and stop it:

```bash
follow_logs() {
  local c="$1" secs="${2:-60}" pid elapsed=0
  docker logs --follow --tail 0 "$c" 2>&1 & pid=$!
  while [ "$elapsed" -lt "$secs" ] && kill -0 "$pid" 2>/dev/null; do
    sleep 1; elapsed=$((elapsed + 1))
  done
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  echo "--- followed ${elapsed}s"
}
```

It shows only what is logged during the window (`--tail 0`), so nothing printed is older than
the call; read recent history first with `docker logs --tail 200` if that is wanted too. It
returns after `secs` seconds, or sooner if the container stops.

### Until swarm replicas converge

`docker service create`, `update`, `scale` and `rollback` block until the service converges, with
no timeout, unless given `-d`. `docker stack deploy` returns at once by default. Either way, run
the command detached and wait with this loop, which has a timeout:

```bash
wait_service() {
  local svc="$1" tmo="${2:-110}" elapsed=0 mode desired upd tmpl ids running
  while [ "$elapsed" -lt "$tmo" ]; do
    mode=$(docker service inspect "$svc" --format '{{if .Spec.Mode.Global}}global{{else}}replicated{{end}}' \
           2>/dev/null) || { echo "FAIL: no such service $svc"; return 1; }
    desired=$(docker service inspect "$svc" --format '{{with .Spec.Mode.Replicated}}{{.Replicas}}{{end}}')
    upd=$(docker service inspect "$svc" --format '{{with .UpdateStatus}}{{.State}}{{end}}')
    case "$upd" in
      paused|rollback_paused) echo "FAIL: update $upd after ${elapsed}s"; break ;;
    esac
    # Count only running tasks made from the current spec, so the old tasks of an update count for nothing.
    # `service inspect` fills in a StopGracePeriod the task's own spec does not carry; drop it to compare.
    tmpl=$(docker service inspect "$svc" --format '{{json .Spec.TaskTemplate}}')
    ids=$(docker service ps "$svc" --filter desired-state=running --no-trunc -q)
    running=0
    [ -n "$ids" ] && running=$(printf '%s\n' "$ids" | xargs docker inspect --type task | jq --argjson t "$tmpl" '
      [.[] | select(.Status.State == "running") | .Spec as $s
       | ($t | if ($s.ContainerSpec | has("StopGracePeriod")) then . else del(.ContainerSpec.StopGracePeriod) end) as $u
       | select(all($s | keys[]; $u[.] == $s[.]))] | length')
    case "$upd" in
      updating|rollback_started) ;;
      *)
        if { [ "$mode" = global ] && [ "$running" -gt 0 ] && [ "$running" -eq "$(printf '%s\n' "$ids" | grep -c .)" ]; } ||
           { [ "$mode" = replicated ] && [ "$running" -eq "$desired" ]; }; then
          [ "$upd" = rollback_completed ] && {
            echo "ROLLED BACK: $running running on the previous spec after ${elapsed}s"; return 2; }
          echo "OK: $running running ($mode) after ${elapsed}s"; return 0
        fi ;;
    esac
    sleep 5; elapsed=$((elapsed + 5))
  done
  echo "TIMEOUT/FAIL after ${elapsed}s (update=${upd:-none}) - failing tasks:"
  docker service ps "$svc" --no-trunc --format json | jq -rs \
    '.[] | select(.CurrentState | startswith("Running") | not) | [.ID,.Name,.CurrentState,.Error] | @tsv'
  return 1
}
```

- Only running tasks made from the service's current spec are counted, by comparing each task's
  spec with the service's task template (as swarm itself decides what to replace). Right after
  `update -d` the old tasks are still running and `UpdateStatus` is briefly empty - swarm resets
  it and sets `updating` a moment later - so counting every running task would report a rollout
  that has not started as finished.
- `rollback_completed` means the service is back on its previous spec, and the loop returns 2
  with `ROLLED BACK`. After `docker service rollback` that is the result asked for. After a deploy
  it means the deploy failed and was reverted, however many replicas are running: report it so.
- A global service has no replica count. It is converged when it has at least one task and every
  task it means to run is running.

Always dump the failing tasks with `--no-trunc` on failure - the `Error` column is the answer and
is truncated by default. The task `ID` is what `docker service logs --tail 200 <task-id>` and
`docker inspect --type task <task-id>` take; the `Name` (`<service>.<slot>`) is not.

### Until a stack converges, or is gone

A stack is converged when each of its services is: run `wait_service` over
`docker stack services <stack> --format '{{.Name}}'`. After `docker stack rm`, which also returns
at once, wait for the services and their tasks to go before reusing the name or its networks:

```bash
wait_stack_removed() {
  local stack="$1" tmo="${2:-110}" elapsed=0 left
  while [ "$elapsed" -lt "$tmo" ]; do
    left=$(docker service ls --filter "label=com.docker.stack.namespace=$stack" -q | wc -l)
    left=$((left + $(docker ps -aq --filter "label=com.docker.stack.namespace=$stack" | wc -l)))
    [ "$left" -eq 0 ] && { echo "OK: stack $stack removed after ${elapsed}s"; return 0; }
    sleep 2; elapsed=$((elapsed + 2))
  done
  echo "TIMEOUT: $left services or containers of $stack left after ${tmo}s"; return 1
}
```

The container count is for this node only; on a multi-node swarm, the services going is what
can be checked from here.

### Until a node reaches a state

```bash
wait_node() {
  local node="$1" want="${2:-ready}" tmo="${3:-110}" avail="${4:-}" elapsed=0 st av
  while [ "$elapsed" -lt "$tmo" ]; do
    st=$(docker node inspect "$node" --format '{{.Status.State}}' 2>/dev/null) || {
      echo "FAIL: no such node $node"; return 1; }
    av=$(docker node inspect "$node" --format '{{.Spec.Availability}}')
    [ "$st" = "$want" ] && { [ -z "$avail" ] || [ "$av" = "$avail" ]; } && {
      echo "OK: $st/$av after ${elapsed}s"; return 0; }
    sleep 5; elapsed=$((elapsed + 5))
  done
  echo "TIMEOUT: state=$st availability=$av after ${tmo}s"; return 1
}
```

`want` is the node's `Status.State`: `ready`, `down`, `disconnected` or `unknown`. `avail` is
optional: pass `active` as well to wait for a node that is both up and taking work
(`wait_node <node> ready 110 active`), since a drained node can be `ready`.

### Until a registry tag appears

```bash
wait_tag() {
  local ref="$1" tmo="${2:-110}" elapsed=0
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
  the honest report of a `NOT MET` from `wait_healthy`.
