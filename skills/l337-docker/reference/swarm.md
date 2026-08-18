# Swarm: cluster, services, nodes, stacks, secrets, configs

Everything here needs the target daemon to be a **swarm manager**. Check before anything else -
the errors otherwise are unhelpful and easy to misread as a broken command:

```bash
docker system info --format '{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}'
# active true  -> manager, good
# active false -> worker; management commands will be refused
# inactive     -> not in a swarm at all
```

> **Gotcha (verified on Docker Desktop 29.7.x):** `docker swarm init --help` prints the unrelated
> `docker-init` plugin's help, because the `init` CLI plugin shadows the help lookup. The command
> itself is unaffected - `docker swarm init --advertise-addr ... --autolock` parses and runs
> correctly. Only the `--help` output is wrong, so read the online reference
> (`reference/docs.md`) rather than concluding the flags do not exist. Sibling subcommands
> (`docker swarm join --help`, `docker swarm ca --help`) are fine.

## Cluster lifecycle

```bash
docker swarm init --advertise-addr 10.0.0.1               # bootstrap; this node becomes a manager
docker swarm init --advertise-addr 10.0.0.1 --autolock    # encrypt the raft log at rest
docker swarm join --token <token> 10.0.0.1:2377
docker swarm join-token worker                            # prints the full join command
docker swarm join-token manager -q                        # token only
docker swarm join-token worker --rotate                   # invalidates the old token
docker swarm leave                                        # worker
docker swarm leave --force                                # last manager - DESTROYS THE SWARM
docker swarm update --autolock=true
docker swarm ca --rotate
docker info --format '{{json .Swarm}}' | jq               # cluster state
```

- `--advertise-addr` is effectively required on any multi-homed host; without it swarm may pick
  the wrong interface and workers will fail to connect.
- **Join tokens are credentials.** `join-token manager` grants full control of the cluster. Never
  print one into a shared transcript or a ticket; hand over the rotation command instead.
- `docker swarm leave --force` on the last manager **destroys the swarm and every service
  definition in it**. There is no undo. Confirm explicitly, every time.
- Run an **odd** number of managers (3 or 5). One manager means no fault tolerance; an even number
  gives no quorum benefit over the odd number below it.

Autolock: with it enabled, a restarted manager needs the unlock key before it rejoins.

```bash
docker swarm unlock-key                                   # display current key
docker swarm unlock-key --rotate
docker swarm unlock                                       # prompts for the key
```

Store the unlock key somewhere outside the cluster. Losing it after a full restart means
rebuilding the swarm.

## Services

```bash
docker service ls
docker service ls --format json | jq -s '.'
docker service ls --filter label=app=web
docker service inspect <svc> --format '{{json .Spec.Mode}}' | jq
docker service inspect <svc> --format '{{json .UpdateStatus}}' | jq
docker service ps <svc> --no-trunc
docker service ps <svc> --filter desired-state=running
docker service logs --tail 200 --timestamps <svc>
```

`docker service ls` shows `REPLICAS` as `running/desired` - the fastest convergence read. When they
disagree, `docker service ps` explains why.

**`--filter desired-state=running` filters on *desired*, not actual state.** A returned task can
still be `failed` or `rejected`; check each task's `CURRENT STATE` rather than assuming a match
means it is running. Drop the filter entirely to see the retired/failed history that explains a
crash loop:

```bash
docker service ps <svc> --no-trunc --format json | jq -rs \
  '.[] | [.Name,.CurrentState,.DesiredState,.Error] | @tsv'
```

`--no-trunc` matters here - the `Error` column is where the real reason lives and it is truncated
by default.

### Tasks across the whole cluster

There is no CLI command that lists every task in the swarm; `docker service ps` always needs a
service. Fan out over the service list instead:

```bash
for svc in $(docker service ls -q); do
  docker service ps "$svc" --no-trunc --format json
done | jq -rs '.[] | select(.CurrentState | startswith("Running") | not)
                   | [.Name, .Node, .CurrentState, .Error] | @tsv'
docker node ps <node> --no-trunc            # the other axis: every task on one node
docker inspect --type task <task-id>        # one task, full document
```

`docker service ps` prints `NAME` as `<service>.<slot>`, but `docker inspect --type task` will not
resolve that form - pass the task `ID` from the same row instead. (The MCP server exposes the
cluster-wide read directly as `swarm_task_list`, with `node`, `service` and `desired-state`
filters.)

### Creating and updating

```bash
docker service create --name web --replicas 3 \
  --network appnet --publish published=8080,target=80 \
  --constraint 'node.role==worker' \
  --limit-memory 512m --reserve-memory 256m \
  --secret db_password --config app_config \
  --label l337-docker-skill.managed=true \
  nginx:1.27

docker service create --mode global --name agent monitoring:latest   # one task per node
```

```bash
docker service update --image nginx:1.28 web              # rolling update
docker service update --replicas 5 web
docker service scale web=5                                # shorthand
docker service scale web=5 api=3                          # several at once
docker service update --force web                         # redistribute tasks, no spec change
docker service rollback web                               # back to the previous spec
docker service rm web                                     # no confirmation, no undo
```

Rolling-update behaviour is worth setting explicitly rather than inheriting the defaults, which
update one task at a time and **pause** the whole rollout on the first failure:

```bash
docker service update --image myapp:v2 \
  --update-parallelism 2 --update-delay 10s \
  --update-failure-action rollback \
  --update-order start-first \
  myapp
```

- `--update-failure-action rollback` turns a bad deploy into an automatic revert instead of a
  service stuck half-updated.
- `--update-order start-first` starts the replacement before stopping the old task - no capacity
  dip, but both versions run briefly, so it needs a backward-compatible change.
- A paused rollout shows in `docker service inspect <svc> --format '{{json .UpdateStatus}}'` as
  `state: paused`. It will not resume on its own.
- `docker service rollback` only goes back **one** spec. It is not a version history.

`--publish published=8080,target=80` uses the swarm routing mesh - the port answers on *every*
node, not just the ones running a task.

## Nodes

```bash
docker node ls
docker node ls --format json | jq -s '.'
docker node inspect <node> --format '{{.Status.State}} {{.Spec.Availability}} {{.Spec.Role}}'
docker node inspect <node> --format '{{json .ManagerStatus}}' | jq
docker node ps <node>                                     # tasks on that node
```

Flag any node whose `Status.State` is not `ready`, whose `Spec.Availability` is `drain`/`pause`,
or whose `ManagerStatus.Reachability` is not `reachable` - an unreachable manager threatens quorum.

```bash
docker node update --availability drain <node>            # evacuate; tasks reschedule elsewhere
docker node update --availability active <node>
docker node update --label-add zone=eu-west-1 <node>
docker node update --role manager <node>                  # promote (also: docker node promote)
docker node rm <node>                                     # must be down or drained first
docker node rm --force <node>
```

Draining is the safe maintenance primitive: it reschedules tasks off the node before you touch it.
Check capacity first - draining a node whose replicas cannot be placed elsewhere leaves the
service under-replicated rather than failing loudly.

`docker node rm` on a still-reachable node needs `--force`, and that leaves the node believing it
is still in the swarm; run `docker swarm leave` on the node itself as well.

## Stacks (Compose on Swarm)

```bash
docker stack deploy -c compose.yaml mystack
docker stack deploy -c compose.yaml -c override.yaml mystack
docker stack deploy -c compose.yaml --with-registry-auth mystack     # private images
docker stack deploy -c compose.yaml --prune mystack                  # remove departed services
docker stack ls
docker stack services mystack
docker stack ps mystack --no-trunc --filter desired-state=running
docker stack rm mystack                                              # removes every service in it
```

- Re-running `deploy` with the same name **updates in place**. Never `rm` then `deploy` to apply a
  change - that drops traffic and loses the rolling update.
- `--with-registry-auth` is required for private images: without it the manager has credentials
  but the workers do not, and tasks fail to pull with a confusing auth error.
- `--prune` removes services that have left the Compose file. Useful, but it deletes services -
  confirm it.
- Services are named `<stack>_<service>`. Use that full name with `docker service ...`.

**The swarm orchestrator ignores several Compose keys** - `build`, `depends_on`, `restart` (use
`deploy.restart_policy`), `links`, `network_mode`. A file that works under `docker compose` can
deploy to a swarm and silently do less. Render it first and call out what will be dropped:

```bash
docker stack config -c compose.yaml                       # resolved swarm-side view
```

Conversely `deploy:` keys (`replicas`, `placement`, `update_config`) are ignored by plain
`docker compose`. The same file rarely means the same thing to both.

## Secrets and configs

Both are cluster-stored blobs mounted into service tasks. **Secrets** are for credentials (mounted
at `/run/secrets/<name>`, in-memory, encrypted at rest in raft); **configs** are for non-sensitive
files (arbitrary mount path). Neither is available to plain `docker run` - swarm services only.

```bash
docker secret create db_password ./password.txt
printf 'hunter2' | docker secret create db_password -     # from stdin, no file on disk
docker secret ls
docker secret inspect db_password                         # METADATA ONLY
docker secret rm db_password

docker config create app_config ./app.conf
docker config ls
docker config inspect app_config --format '{{json .Spec.Data}}'   # base64 of the content
docker config rm app_config
```

**A secret's value cannot be read back - by design.** `docker secret inspect` returns metadata
only. If the value is lost, rotate it; there is no recovery. (A config's content *is* readable via
`.Spec.Data`, base64-encoded - so configs must never hold credentials.)

Secrets and configs are **immutable**. Updating one means create-new, point the service at it,
remove the old:

```bash
printf 'newsecret' | docker secret create db_password_v2 -
docker service update --secret-rm db_password --secret-add db_password_v2 myapp
docker secret rm db_password
```

`--secret-rm`/`--secret-add` in one `service update` avoids a window where the service has neither.

Prefer stdin (`-`) over a file: it leaves no plaintext copy on disk and nothing in shell history.

## Waiting for convergence

Swarm operations are asynchronous - `service update` returns before the rollout finishes. Do not
report success off the command's exit code. See `reference/observability.md` for the polling loops
that block until replicas converge or a node reaches `ready`.
