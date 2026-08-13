# Deployment workflows

## Deploy a single container

1. **Pull explicitly** so a registry or auth failure surfaces before anything else runs:
   `docker pull <image>`. Pin the tag; `latest` is not a version.
2. **Decide on supporting resources.** A dedicated user-defined network if it must talk to other
   containers by name; a named volume for anything that must survive recreation. Create them
   stamped: `docker network create --label l337-docker-skill.managed=true appnet`.
3. **Check the name is free.** `docker ps -a --filter name=^<name>$` - if something already holds
   it, stop and ask. Never silently replace an existing container.
4. **Run it**, detached, with a restart policy and explicit limits:
   ```bash
   docker run -d --name <name> --restart unless-stopped \
     --network appnet -p 8080:80 -v appdata:/var/lib/app \
     --memory 512m --cpus 1 \
     --label l337-docker-skill.managed=true \
     <image>
   ```
5. **Verify it actually came up** with the `wait_healthy` loop in
   `reference/observability.md`. If the image has no `HEALTHCHECK`, say that health was not
   verified rather than reporting it healthy.
6. **Report** the container id, everything you created, and the published ports. If it failed,
   `docker logs --tail 100 <name>` before proposing anything.

## Replace a running container with a new image

The goal is a rollback path that stays available until the replacement is proven. **Rename the old
container, do not remove it.**

1. **Capture the current configuration and show it to the user before changing anything:**
   ```bash
   docker inspect <c> --format '{{json .Config}}'      | jq '{Image,Env,Cmd,Entrypoint,User,Labels}'
   docker inspect <c> --format '{{json .HostConfig}}'  | jq '{PortBindings,Binds,RestartPolicy,Memory,NanoCpus}'
   docker inspect <c> --format '{{json .Mounts}}'      | jq
   docker inspect <c> --format '{{json .NetworkSettings.Networks}}' | jq 'keys'
   ```
   `docker inspect` is the **only** record of how a container was created - there is no stored
   `docker run` command. Losing it before the new container is running means reconstructing the
   config from memory.
2. `docker pull <new-image>`.
3. `docker stop <c>` then `docker rename <c> <c>-old`. This frees the name while keeping the old
   container intact and startable.
4. `docker run -d --name <c> ...` with the captured config and the new image.
5. **Wait for health** (`wait_healthy`). On failure, roll back immediately:
   ```bash
   docker stop <c> && docker rm <c>
   docker rename <c>-old <c> && docker start <c>
   ```
6. **Only after it is confirmed healthy**, ask before `docker rm <c>-old` - that is the moment the
   rollback path disappears. Leaving it for a day is often the right call.

Volumes carry over by reference, so data persists. Anonymous volumes do **not** - check
`.Mounts` for entries with no `Name` before assuming state is safe.

## Plan a multi-container application

When the user describes an app informally, design before touching the daemon.

1. **Produce a written plan first** - no tool calls: each service (image, name, role, ports),
   networks and what attaches to them, volumes and mount paths, environment and secrets, and the
   startup order if there are dependencies.
2. **Recommend Compose over hand-run containers** for anything with more than one service. It
   makes the topology reviewable and the teardown reliable; a pile of `docker run` commands is
   neither.
3. Wait for approval, then write the `compose.yaml` and follow the project workflow below.

## Bring up a Compose project

1. **Render the resolved config and review it** before touching the daemon:
   ```bash
   docker compose -f <file> config --format json | jq
   ```
   Flag `privileged: true`, `network_mode: host`, bind mounts that escape the project directory,
   and anything secret-shaped sitting in `environment:`. Show the services, networks and volumes
   that will be created and get approval.
2. **Pull up front** so registry failures land before any container starts:
   `docker compose pull`.
3. **Bring it up and block on health:**
   ```bash
   docker compose up -d --wait --wait-timeout 120
   ```
   `--wait` is what makes a success report truthful; without it `up -d` returns once containers
   are created, not once they work.
4. **Verify:** `docker compose ps --format json | jq -rs '.[] | [.Service,.State,.Health] | @tsv'`.
5. **Tail logs** for anything that started but is unhappy:
   `docker compose logs --tail 100`.
6. Report per-service state. Mention that teardown is `docker compose down`, and that `-v` on
   `down` destroys the named volumes - never add it unprompted.

## Deploy a stack to a Swarm

1. **Confirm the target is a manager** - everything else fails confusingly otherwise:
   ```bash
   docker system info --format '{{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}'   # want: active true
   ```
2. **Render and review** the Compose file, and explicitly call out the keys the swarm orchestrator
   ignores - `build`, `depends_on`, `restart`, `links`, `network_mode`. A file that works under
   `docker compose` can deploy to a swarm and quietly do less:
   ```bash
   docker stack config -c compose.yaml
   ```
   Prefer swarm `secrets`/`configs` over environment variables for anything sensitive.
3. **Deploy:**
   ```bash
   docker stack deploy -c compose.yaml --with-registry-auth mystack
   ```
   `--with-registry-auth` is required for private images - without it the workers cannot pull,
   and the error blames the image rather than the credentials. Add `--prune` only if the user
   wants services removed when they leave the file, and confirm it as a deletion.
4. **Wait for convergence per service.** `stack deploy` returns before the rollout finishes:
   ```bash
   docker stack services mystack --format json | jq -rs '.[].Name'
   # then wait_service <name> for each - see reference/observability.md
   ```
5. **On a stuck rollout**, the failing task's error is the answer:
   ```bash
   docker stack ps mystack --no-trunc --format json | jq -rs \
     '.[] | select(.CurrentState | startswith("Running") | not) | [.Name,.CurrentState,.Error] | @tsv'
   ```
6. **Iterate by redeploying**, never by removing first - `docker stack deploy` with the same name
   updates in place and preserves the rolling update. Report desired vs running per service.
