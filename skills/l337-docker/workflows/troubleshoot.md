# Troubleshooting workflows

All of these are **diagnosis**. Change nothing; propose the fix and let the user decide.

Pick the entry point by what you know:

- a named container is misbehaving → **Diagnose one container**
- something is wrong on the host but you don't know what → **Triage a host incident**
- routine "is everything OK?" → **Fleet sweep**
- a Compose project → **Diagnose a Compose project**
- A can't reach B → **Container-to-container networking**
- a swarm → **Swarm health audit**

## Diagnose one container

1. **State, exit code, restart count** - the fastest read on *what kind* of failure this is:
   ```bash
   docker inspect <c> --format '{{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}'
   docker inspect <c> --format '{{json .State.Health}}' | jq
   ```
   `OOMKilled: true` ends the investigation - it needs more memory or a leak fix. Exit 137 is
   SIGKILL (usually OOM or a `stop` timeout), 139 is SIGSEGV, 143 is SIGTERM. A high
   `RestartCount` means a crash loop, so the current logs may only show the latest attempt.
2. **Logs** - usually the actual answer:
   ```bash
   docker logs --tail 200 --timestamps <c> 2>&1
   ```
   These survive the container stopping. If they are empty, check the logging driver
   (`reference/containers.md`) before concluding the app is silent.
3. **If running**, check pressure and processes:
   ```bash
   docker stats --no-stream <c>
   docker top <c>
   ```
   Memory near its limit predicts the next OOM kill.
4. **If it won't start**, inspect the config rather than the runtime - a bad mount, a missing env
   var, or an entrypoint that isn't executable:
   ```bash
   docker inspect <c> --format '{{json .Config}}' | jq '{Entrypoint,Cmd,Env,User,WorkingDir}'
   docker inspect <c> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
   docker diff <c>
   ```
5. **Confirm inside** where a config file or a port is in question: `docker exec <c> <argv>` (see
   `reference/containers.md`; the container must be running).

State the root cause in one paragraph, then the concrete fix. Do not apply it unasked.

## Triage a host incident

Symptom-first, when you don't yet know which container is at fault. Work from what changed
towards a single suspect.

1. **Build a timeline.** `--since` takes a duration or timestamp; `--until now` is invalid, use
   `--until 0s`:
   ```bash
   docker events --since 30m --until 0s --filter type=container --format json \
     | jq -rs '.[] | [(.time|tostring), .Actor.Attributes.name, .Action] | @tsv'
   ```
   Scan for `die`, `oom`, `kill`, `health_status: unhealthy`, and tight `start`/`die` pairs on one
   container - that is a crash loop.
2. **Reconcile against current state.** A container with a recent `die` that is now `restarting`
   or `exited` non-zero is the prime suspect:
   ```bash
   docker ps -a --format json | jq -rs '.[] | [.Names,.State,.Status] | @tsv'
   ```
3. **Separate cause from symptom.** A host out of memory or disk takes down healthy containers
   too. If many *unrelated* containers failed at once, suspect the host, not any one container:
   ```bash
   docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemPerc}}'
   docker system df
   df -h /var/lib/docker 2>/dev/null || df -h
   ```
   A full disk presents as containers failing to start with unrelated-looking errors.
4. **Read the suspect's logs** around the first `die` in the timeline.

Report the most likely root cause and the **blast radius** - one container or host-wide - in two
sentences, then hand off to the single-container workflow or name the host-level fix.

## Fleet sweep

Read-only health and load snapshot across everything.

1. **Enumerate with state**, and flag the unhealthy set *first* - it matters more than load:
   ```bash
   docker ps -a --format json | jq -rs '.[] | [.Names,.State,.Status] | @tsv'
   ```
   Problems: `exited` with non-zero code, `restarting` (crash loop), `paused`, `dead`.
2. **Health per container** - use the fleet loop in `reference/observability.md`.
3. **Resource pressure**, one sample:
   ```bash
   docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.MemPerc}}'
   ```
   Instantaneous, not an average - a single spike is not sustained pressure. `MemPerc` near 100 is
   the one to act on.
4. **Drill into the worst few** (~5) plus everything flagged in step 1:
   `docker logs --tail 100 <c> 2>&1 | grep -iE 'error|fatal|panic|refused'`.

Render one table - name, status, CPU%, mem%, one-line note - sorted with problems on top. End
naming the single container most worth attention. Recommend nothing destructive.

If asked to keep watching, prefer a bounded event wait
(`docker events --since 0s --until $(($(date +%s)+300)) --filter event=health_status`) over
re-running this on a timer.

## Diagnose a Compose project

1. `docker compose ps -a --format json | jq -rs '.[] | [.Service,.State,.Health,.ExitCode] | @tsv'`
2. For every service not `running`:
   `docker compose logs --tail 200 --timestamps <service>`
3. Confirm the **rendered** config matches expectations - interpolation and override merging mean
   the file is not what runs: `docker compose config --format json | jq`.
4. **Check `depends_on` conditions.** `condition: service_started` only confirms the process
   began, not that it accepts connections. If a dependency has no healthcheck, `service_healthy`
   is unavailable and startup races are expected - that is very often the whole bug.
5. Check for a project-name split: `docker compose ls -a --format json` will show a second project
   if the file has been run from two directories.

## Container-to-container networking

Work from the most common cause outward.

1. **Do they share a user-defined network?**
   ```bash
   docker inspect <source> --format '{{json .NetworkSettings.Networks}}' | jq 'keys'
   docker inspect <target> --format '{{json .NetworkSettings.Networks}}' | jq 'keys'
   ```
   No shared user-defined network is almost always the answer - the default `bridge` has no DNS,
   so name resolution cannot work there. Fix: `docker network connect <net> <container>`.
2. **If they do share one**, test DNS and the port from inside the source, preferring argv form:
   ```bash
   docker exec <source> getent hosts <target>
   docker exec <source> nc -z -w 2 <target> <port>
   ```
   Distinguish **DNS failure** (name doesn't resolve - wrong network or wrong alias) from
   **connection failure** (resolves, refused/timed out - the service isn't listening).
3. **If DNS resolves but the connection is refused**, check the target is listening on `0.0.0.0`
   rather than `127.0.0.1` - a service bound to loopback inside a container is unreachable from
   any other container, and this is the second most common cause. Confirm it started at all with
   `docker logs`.
4. **Do not confuse published ports with container reachability.** A missing `-p` only affects
   access *from the host*; containers on a shared network reach each other on the container port
   regardless.

State the cause in one sentence and the exact fix.

## Swarm health audit

Read-only. Requires a manager (`reference/swarm.md`).

1. **Nodes** - flag `Status.State != ready`, `Availability` of `drain`/`pause`, and any manager
   whose `Reachability` is not `reachable` (a quorum threat):
   ```bash
   docker node ls --format json | jq -rs '.[] | [.Hostname,.Status,.Availability,.ManagerStatus] | @tsv'
   ```
   Call out an **even** number of managers, or only one - neither gives fault tolerance.
2. **Services** - desired vs running:
   ```bash
   docker service ls --format json | jq -rs '.[] | [.Name,.Mode,.Replicas] | @tsv'
   ```
3. **For each under-replicated service**, drop the `desired-state` filter to see the failure
   history, and use `--no-trunc` - the `Error` column is truncated by default and is the answer:
   ```bash
   docker service ps <svc> --no-trunc --format json | jq -rs '.[] | [.Name,.CurrentState,.Error] | @tsv'
   ```
   Look for tasks stuck in `rejected`/`failed`, or cycling `shutdown` → `starting`.
4. **Check for a stalled rollout:**
   `docker service inspect <svc> --format '{{json .UpdateStatus}}' | jq` - `state: paused` will
   not resume on its own.
5. **Logs for a crash-looping service:** `docker service logs --tail 200 --timestamps <svc>`.

Summarise as two tables (nodes, services) plus a one-paragraph verdict and the single most urgent
fix. If something is mid-convergence, use `wait_service` rather than re-running the audit on a
timer.
