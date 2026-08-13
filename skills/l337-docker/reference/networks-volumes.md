# Networks and volumes

## Networks

```bash
docker network ls
docker network ls --format json | jq -s '.'
docker network ls --filter driver=bridge --filter label=app=web
docker network inspect <net>                              # JSON array
docker network inspect <net> --format '{{json .Containers}}' | jq
docker network inspect <net> --format '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}'
```

`network inspect`'s `.Containers` is the authoritative "who is attached", including each
container's IP on that network - more reliable than reading each container's
`.NetworkSettings.Networks` one at a time.

### Creating and attaching

```bash
docker network create appnet
docker network create --driver bridge --subnet 172.28.0.0/16 --gateway 172.28.0.1 appnet
docker network create --internal backend                  # no outbound route
docker network create --label l337-docker-skill.managed=true appnet

docker network connect appnet <container>
docker network connect --alias db appnet <container>      # extra DNS name on this network
docker network disconnect appnet <container>
```

**The default `bridge` network has no DNS.** Containers on it can only reach each other by IP.
Containers on a *user-defined* network resolve each other by container name automatically. This is
the single most common cause of "container A can't reach container B" - the fix is a user-defined
network, not a `/etc/hosts` edit. See `workflows/troubleshoot.md`.

- A container can be attached to several networks at once; `connect` works on a running container
  with no restart.
- `--internal` blocks outbound traffic - right for a database tier, and a trap if the service needs
  to reach a package registry or an external API.
- Publishing (`-p`) is orthogonal: it controls **host** access only. Two containers on a shared
  network reach each other on the container port with no `-p` at all.
- Drivers: `bridge` (single host, default), `host` (no isolation - the container shares the host
  network stack), `none`, `overlay` (multi-host, swarm), `macvlan`. `host` is a security finding,
  not a convenience - flag it.

### Removing

```bash
docker network rm <net>                                   # fails while containers are attached
docker network prune                                      # every unused user-defined network
docker network prune --filter label=l337-docker-skill.managed=true
```

`rm` refuses while anything is attached - disconnect first, or remove the containers. The
built-in `bridge`, `host` and `none` networks cannot be removed, and `prune` correctly ignores
them.

## Volumes

```bash
docker volume ls
docker volume ls --format json | jq -s '.'
docker volume ls --filter dangling=true                   # referenced by no container
docker volume ls --filter label=l337-docker-skill.managed=true
docker volume inspect <vol> --format '{{.Mountpoint}} {{.Driver}}'
```

Volumes are addressed by **name only** - there is no separate id, and no short-id form.

```bash
docker volume create appdata
docker volume create --label l337-docker-skill.managed=true appdata
docker volume create --driver local \
  --opt type=nfs --opt o=addr=10.0.0.1,rw --opt device=:/export/data nfsdata
```

### Mounting

```bash
docker run -v appdata:/var/lib/app ...                    # named volume
docker run -v /host/path:/container/path ...              # bind mount
docker run -v /host/path:/container/path:ro ...           # read-only
docker run --mount type=volume,source=appdata,target=/var/lib/app ...
docker run --mount type=bind,source=/host/path,target=/app,readonly ...
docker run --tmpfs /tmp:size=64m ...                      # memory-backed, never persisted
```

`-v` and `--mount` do the same job; `--mount` is explicit and fails loudly on a typo, while `-v`
**silently creates a directory on the host** if a bind source does not exist. Prefer `--mount` for
anything scripted.

`-v` with a name creates a named volume; `-v` with a path creates a bind mount. The difference is
the leading `/`, which is easy to get wrong and produces a stray volume named after the intended
path.

### Finding what uses a volume

There is no reverse index, so build one:

```bash
docker ps -a --format json \
  | jq -rs '.[] | .Names' \
  | xargs -I{} sh -c 'docker inspect {} --format "{{range .Mounts}}{{if eq .Name \"appdata\"}}{{$.Name}}{{end}}{{end}}"'
```

Simpler for a one-off, and what to reach for first:

```bash
docker ps -a --filter volume=appdata
```

Do this before stopping writers, before restoring, and before removing anything.

### Removing

```bash
docker volume rm <vol>                                    # fails while a container references it
docker volume prune                                       # unnamed + unreferenced volumes
docker volume prune -a                                    # ALL unused volumes, named ones included
```

**Volume removal is the most destructive routine operation in Docker.** There is no undo, no
recycle bin, and no way to tell whether a volume holds anything valuable without mounting it and
looking. A "dangling" volume is not junk - it commonly means the container was recreated and the
data is waiting to be re-attached.

Always: list the candidates, mount and inspect anything unrecognised, get explicit confirmation,
and offer to back up first (`workflows/maintenance.md` has the backup and restore procedures).

`docker rm -v <container>` removes that container's **anonymous** volumes as a side effect -
another silent data-loss path, and the reason `-v` should never be reflexive.

### Backing up

Docker has no volume export API. The pattern is a throwaway container with the volume mounted,
then `docker cp` - no `tar` binary needed inside the image, and it works with the helper
container stopped. Full procedure in `workflows/maintenance.md`.
