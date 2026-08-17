# Images

## Listing and inspecting

```bash
docker image ls
docker image ls -a                                        # include intermediate layers
docker image ls --filter dangling=true                    # untagged leftovers
docker image ls --filter reference='myapp:*'
docker image ls --format json | jq -s '.'
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'
```

**Sizes in `image ls` are per-image totals that double-count shared layers.** Ten images on the
same base do not occupy the sum of their listed sizes. For actual reclaimable disk use
`docker system df` (see `reference/system.md`), and for byte-accurate single-image size use
`inspect`, since `ls --format json` returns a rounded display string:

```bash
docker image inspect <ref> --format '{{.Size}}'           # bytes (int)
docker image ls --format json | jq -rs '.[0].Size'        # "122MB" (string) - display only
```

```bash
docker image inspect <ref>                                # JSON array
docker image inspect <ref> --format '{{.Os}}/{{.Architecture}}'
docker image inspect <ref> --format '{{json .Config}}' | jq '{User,Entrypoint,Cmd,ExposedPorts,Env}'
docker image inspect <ref> --format '{{json .RepoDigests}}' | jq
docker history <ref>                                      # layers, newest first
docker history --no-trunc --format json <ref> | jq -s '.'
```

`docker history` is where a fat image gets explained - a `COPY` that pulled in `node_modules`, an
`apt-get` whose cache was never cleaned. Layers show as `<missing>` for pulled images; that is
normal (only locally-built layers keep build metadata), not corruption.

**Checking `Config.User` is the fast root check**: empty means the image runs as root.

## Pulling and pushing

```bash
docker pull nginx:1.27
docker pull --platform linux/amd64 nginx:1.27             # on an arm64 host
docker pull nginx@sha256:abc...                           # digest pin - immutable
docker push ghcr.io/org/app:v1.2.3
docker push --all-tags ghcr.io/org/app
```

- Pin tags for anything reproducible; pin **digests** for anything security-sensitive. A tag can
  be repointed by whoever owns the repo, a digest cannot.
- `--platform` matters on Apple Silicon and ARM servers. Without it you get the host platform, and
  an image with no matching platform fails with a manifest error rather than falling back.
- Push needs auth: `docker login <registry>` first (`reference/system.md`).
- To see what a tag *is* without pulling it - platforms, digest, config, labels - use
  `reference/registry.md`. Do not pull just to inspect.

## Tagging

```bash
docker tag <source> ghcr.io/org/app:v1.2.3
```

A tag is a pointer, and **`docker tag` silently overwrites an existing one** - no prompt, no
warning. Check first when the target might exist:

```bash
docker image inspect ghcr.io/org/app:v1.2.3 >/dev/null 2>&1 && echo "tag already exists"
```

## Building

```bash
docker build -t myapp:dev .
docker build -t myapp:dev -f docker/Dockerfile.prod .
docker build -t myapp:dev --build-arg VERSION=1.2.3 --no-cache .
docker build -t myapp:dev --target builder .              # stop at a named stage
docker build -t myapp:dev --secret id=npmrc,src=$HOME/.npmrc .
```

Modern Docker routes `docker build` through BuildKit/buildx automatically. For anything
multi-platform, cache-exported, or attestation-producing, go to `reference/buildx.md` - the
`docker build` path cannot do those.

- The **build context** (`.`) is uploaded to the daemon. Without a `.dockerignore` this can mean
  gigabytes and a leaked `.git`/`.env`. Check for one before building an unfamiliar project.
- `-f` is resolved relative to the **working directory**, not the context. They are independent
  arguments; a Dockerfile outside the context is legal.
- Never bake credentials with `--build-arg` - build args are visible in `docker history`. Use
  `--secret`, which mounts at build time and leaves no layer.
- Builds are deliberately **not** provenance-labelled: a label changes the image digest.

## Saving and loading

```bash
docker save -o myapp.tar myapp:dev                        # with layers and metadata
docker save -o multi.tar myapp:dev myapp:v1 nginx:1.27
docker load -i myapp.tar
```

Always use `-o`/`-i`. A bare `docker save` writes a multi-hundred-megabyte tar to stdout.

`save`/`load` preserve layers, tags and history - this is the airgap transfer path.
`export`/`import` (see `reference/containers.md`) flatten and lose all of it; they are not
interchangeable.

## Importing a flat rootfs

```bash
docker import rootfs.tar myorg/rootfs:v1                  # tar of a filesystem -> 1-layer image
docker import --change 'CMD /bin/sh' rootfs.tar myorg/rootfs:v1
docker import https://example.com/rootfs.tar myorg/rootfs:v1
```

- `import` takes a **filesystem** tar, `load` takes a `docker save` bundle. Feeding a save bundle to
  `import` "works" and produces a useless image whose root is the bundle's own metadata files.
- An imported image has an empty config - no `CMD`, `ENTRYPOINT` or `ENV` - so it will not run until
  you set one. `--change` accepts only `CMD`, `ENTRYPOINT`, `ENV`, `EXPOSE`, `ONBUILD`, `USER`,
  `VOLUME` and `WORKDIR`; `LABEL` is not among them, so an imported image cannot be
  provenance-labelled at import time.
- The URL form is fetched by the **daemon**, not by the shell - so it resolves in the daemon's
  network namespace, which matters against a remote host.

## Removing and pruning

```bash
docker image rm <ref>                                     # alias: docker rmi
docker image rm -f <ref>                                  # even if containers reference it
docker image prune                                        # dangling only - usually safe
docker image prune -a                                     # EVERY image not used by a container
docker image prune --filter 'until=168h'
```

**`docker image rm <tag>` on an image carrying several tags only removes that tag** - the image
survives under its other names, and reclaims nothing. `docker image inspect <ref> --format '{{json
.RepoTags}}'` tells you which case you are in before you promise the user disk back.

`docker image prune -a` is the one that surprises people: it removes every image no *running or
stopped container* references, including base images you will immediately re-pull and anything
built locally but not yet run. Always show the candidate list and total first:

```bash
docker image ls --filter dangling=true                    # what plain prune would take
docker system df                                          # what is actually reclaimable
```

## Build cache

The build cache is invisible to `docker image prune` and is frequently the largest reclaimable
bucket on a build machine.

```bash
docker builder prune                                      # dangling build cache
docker builder prune -a                                   # all build cache
docker builder prune --reserved-space 10GB                # keep this much, evict the rest
docker buildx du --verbose                                # what is actually in there
```

`--reserved-space` is the current spelling. The older `--keep-storage` was renamed at Engine API
v1.48; on a modern daemon use `--reserved-space`, and see also `--max-used-space` /
`--min-free-space`. Check `docker builder prune --help` if a flag is rejected - this set moved
recently.

A cold cache makes the next build markedly slower. Say so before pruning it.

## Searching Docker Hub

```bash
docker search nginx --limit 10 --format json | jq -s '.'
docker search --filter is-official=true --filter stars=100 nginx
```

`docker search` covers **Docker Hub only** - never GHCR, ECR or a private registry - and returns
repositories, not tags. For tags, digests and metadata on any registry, use
`reference/registry.md`.
