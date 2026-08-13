# Maintenance workflows

## Investigate disk usage (before reclaiming anything)

Diagnosis only - find out *what* is consuming space before deleting any of it. The right fix
differs per bucket.

1. **Top-line split** - identify the dominant bucket:
   ```bash
   docker system df
   ```
2. **If Images dominate:**
   ```bash
   docker image ls --format json | jq -rs 'sort_by(.Size) | reverse | .[:10] | .[] | [.Repository,.Tag,.Size] | @tsv'
   docker history --no-trunc <biggest-image>
   ```
   `history` shows which layer is heavy - a fat `COPY`, an uncleaned package cache. Remember the
   listed sizes double-count shared layers, so the sum overstates real disk use.
3. **If Build Cache dominates** (very common on build machines, and invisible to `image prune`):
   ```bash
   docker buildx du --verbose
   ```
   `docker system df` does not see a `docker-container` builder's cache; `buildx du` does.
4. **If Local Volumes dominate:**
   ```bash
   docker volume ls --filter dangling=true
   docker system df -v
   ```
   **Do not assume a dangling volume is junk** - it commonly holds data whose container was
   recreated. Identify each before proposing removal.
5. **If Containers dominate**, look for stopped containers with large writable layers:
   ```bash
   docker ps -a --size --format '{{.Names}}\t{{.Size}}\t{{.Status}}'
   docker diff <c>
   ```

Report the dominant cause, the specific offenders, and what each would reclaim. Recommend nothing
destructive here - hand off to the prune workflow.

## Prune safely

Always: run the read-only equivalent, show the count and the list, get confirmation, then act.
Finish by reporting the measured before/after delta, not an estimate.

```bash
docker system df                                       # BEFORE snapshot
```

1. **Stopped containers** - inspect the list first:
   ```bash
   docker ps -a --filter status=exited --format '{{.Names}}\t{{.Status}}'
   docker container prune
   ```
2. **Dangling images only** (safe; untagged leftovers of rebuilds):
   ```bash
   docker image ls --filter dangling=true
   docker image prune
   ```
   `docker image prune -a` is a different proposition - it removes every image not referenced by a
   container, including base images you will immediately re-pull. Treat it as a separate decision.
3. **Build cache** - usually the largest single reclaim on a build machine:
   ```bash
   docker buildx du                     # or: docker builder prune --help for a daemon without buildx
   docker buildx prune                  # dangling; -a for everything
   ```
   Warn that the next build will be slower with a cold cache.
4. **Unused networks** (low risk): `docker network prune`.
5. **Volumes - only with explicit confirmation, every time:**
   ```bash
   docker volume ls --filter dangling=true
   docker volume prune                  # -a also takes named volumes
   ```
   There is no undo and no way to know what a volume holds without mounting it. Offer to back up
   first (below).

```bash
docker system df                                       # AFTER - report the real delta
```

**Avoid `docker system prune -a`** as a first move. It combines all of the above, including the
aggressive image sweep, behind one confirmation - which is precisely the prompt people click
through. Run the individual prunes so each decision is separate and visible.

## Tear down only what you created

Scoped to the provenance label, so nothing else is touched.

1. **Inventory first, and show it** - including volumes, even when not removing them:
   ```bash
   docker ps -a      --filter label=l337-docker-skill.managed=true --format '{{.Names}}\t{{.Status}}'
   docker network ls --filter label=l337-docker-skill.managed=true --format '{{.Name}}'
   docker volume ls  --filter label=l337-docker-skill.managed=true --format '{{.Name}}'
   docker service ls --filter label=l337-docker-skill.managed=true --format '{{.Name}}'   # swarm only
   ```
   If it is empty, stop and say so.
2. **Containers.** `prune` only takes *stopped* ones; a running managed container survives it, so
   handle those explicitly after confirming:
   ```bash
   docker container prune --filter label=l337-docker-skill.managed=true
   ```
3. **Networks:** `docker network prune --filter label=l337-docker-skill.managed=true`.
4. **Volumes:** only on explicit request, and confirm as data loss:
   `docker volume prune --filter label=l337-docker-skill.managed=true`.
5. **Services** have no prune - remove individually from the step-1 inventory.
6. Re-run the inventory to confirm, and report what went.

If `docker-mcp-server` also runs against this daemon, repeat with
`label=docker-mcp-server.managed=true`. They are separate footprints; checking one proves nothing
about the other.

## Inventory everything sharing a label

```bash
L='com.example.app=web'
docker ps -a      --filter "label=$L" --format '{{.Names}}\t{{.ID}}\t{{.Status}}'
docker network ls --filter "label=$L" --format '{{.Name}}\t{{.ID}}'
docker volume ls  --filter "label=$L" --format '{{.Name}}'
docker image ls   --filter "label=$L" --format '{{.Repository}}:{{.Tag}}\t{{.ID}}'
```

Render grouped by resource type. Read-only; change nothing.

## Back up a volume

Docker has no volume export API. Mount the volume into a helper container and pull the filesystem
out through `docker cp`. **The helper never needs to run** and needs no `tar` binary - `docker cp`
works on a created-but-never-started container.

1. **Confirm it exists:** `docker volume inspect <vol>`.
2. **Quiesce writers if integrity matters.** A hot copy of a live database is crash-consistent at
   best:
   ```bash
   docker ps -a --filter volume=<vol> --format '{{.Names}}\t{{.State}}'
   ```
   Offer to stop them, or state plainly that the backup is crash-consistent only.
3. **Create the helper, copy out, remove it:**
   ```bash
   docker create --name vbackup -v <vol>:/data alpine:3.20 >/dev/null
   docker cp vbackup:/data - > <vol>-backup.tar
   docker rm vbackup
   ```
4. **Restart anything stopped in step 2.** Report the archive path and size.

The archive is **rooted at `data/`** - `docker cp` names the tar after the path's last component.
The restore below depends on that, so do not repackage it.

## Restore a volume

Destructive to whatever the volume currently holds.

1. **If the volume already exists, stop and confirm the overwrite** - there is no way to tell
   whether it holds anything valuable without mounting and looking:
   ```bash
   docker volume inspect <vol> >/dev/null 2>&1 && echo "EXISTS - confirm overwrite"
   docker volume create <vol>          # only if it does not exist
   ```
2. **Ensure nothing is writing to it.** Restoring underneath a live writer corrupts state:
   `docker ps --filter volume=<vol>`.
3. **Clear stale files** - files absent from the archive would otherwise survive the restore. This
   step needs a *running* helper:
   ```bash
   docker run -d --name vrestore -v <vol>:/data alpine:3.20 sleep 3600
   docker exec vrestore sh -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true'
   ```
4. **Extract at `/`, not `/data`:**
   ```bash
   docker cp - vrestore:/ < <vol>-backup.tar
   ```
   The archive is rooted at `data/`, so extracting at `/` lands it back in `/data`. **Extracting
   at `/data` produces `/data/data/...`** - verified, and the single most common restore bug.
5. **Verify, then clean up:**
   ```bash
   docker exec vrestore ls -la /data
   docker rm -f vrestore
   ```
6. Restart anything stopped in step 2 and report what was restored.

## Audit configured daemons

1. **Contexts and the current target:**
   ```bash
   docker context ls --format json | jq -rs '.[] | [.Name,(.Current|tostring),.DockerEndpoint] | @tsv'
   ```
   Highlight `Current: true` - that is what an unqualified `docker` command hits.
2. **Check for an override.** `DOCKER_HOST` silently beats the current context, and `-H` beats
   everything:
   ```bash
   echo "DOCKER_HOST=${DOCKER_HOST:-<unset>} DOCKER_CONTEXT=${DOCKER_CONTEXT:-<unset>}"
   ```
3. **Confirm what each actually is** - a context name is not evidence of what it points at:
   ```bash
   docker --context <name> system info --format '{{.Name}} {{.ServerVersion}} {{.OperatingSystem}}'
   ```
4. If contexts point at different daemons, **ask which is intended before any mutating
   operation**.

## Survey several hosts

Read-only sweep when more than one daemon is configured. Change nothing anywhere.

```bash
for ctx in $(docker context ls --format json | jq -rs '.[].Name'); do
  info=$(docker --context "$ctx" system info \
          --format '{{.ServerVersion}}|{{.OperatingSystem}}|{{.ContainersRunning}}|{{.ContainersStopped}}' 2>/dev/null) \
    || { printf '%s\tUNREACHABLE\n' "$ctx"; continue; }
  printf '%s\t%s\n' "$ctx" "$info"
done
```

A host may be unreachable - record it and move on; the others are independent. Then per reachable
host, flag problem containers:

```bash
docker --context "$ctx" ps -a --format json \
  | jq -rs '.[] | select(.State != "running") | [.Names,.State,.Status] | @tsv'
```

Render one table across all hosts: host, reachable, version, running, stopped, problems. **Name
the host on every line** - a count without a host is meaningless. End with a one-line verdict per
host and name the one most worth attention.
