# Buildx / BuildKit

A CLI plugin. Confirm it exists before planning around it: `docker buildx version`.

Modern `docker build` already routes through BuildKit, so reach for `docker buildx build`
explicitly when you need multi-platform output, cache import/export, attestations, or a
non-default builder.

## Builders

```bash
docker buildx ls
docker buildx ls --format json | jq -s '.'
docker buildx inspect                                     # current builder
docker buildx inspect --bootstrap <name>                  # start it and report
docker buildx use <name>
docker buildx create --name multi --driver docker-container --use --bootstrap
docker buildx create --name multi --driver docker-container --platform linux/amd64,linux/arm64
docker buildx rm <name>
```

**The default `docker` driver cannot build multi-platform images.** This is the single most common
buildx failure. `docker buildx ls` shows the driver per builder; if the active one is `docker`,
create a `docker-container` builder first. The `Platforms` column lists what the builder can
target - entries beyond the host architecture come from QEMU emulation and are dramatically
slower, not free.

Drivers: `docker` (built into the daemon, no multi-platform, images land in the local store),
`docker-container` (BuildKit in a container - the usual choice), `kubernetes`, `remote`, `cloud`.

`--bootstrap` starts the builder immediately rather than on first build, so failures surface now.

## Building

```bash
docker buildx build -t myapp:dev --load .                 # into the local image store
docker buildx build -t ghcr.io/org/app:v1 --push .        # straight to the registry

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/org/app:v1 \
  --provenance mode=max --sbom true \
  --push .
```

**`--load` and multi-platform are mutually exclusive.** The local image store holds one platform
per tag, so a multi-platform build must go somewhere that can hold an index: `--push` to a
registry, or `--output type=oci,dest=out.tar`. There is no way to `--load` a two-platform image;
build the single platform you need locally instead.

Without `--load` or `--push` the result is **discarded** - the build runs, the cache warms, and
nothing is kept. That surprises people who expect `docker build` semantics.

Cache:

```bash
docker buildx build --cache-from type=registry,ref=ghcr.io/org/app:cache \
                    --cache-to   type=registry,ref=ghcr.io/org/app:cache,mode=max \
                    -t ghcr.io/org/app:v1 --push .
docker buildx build --cache-to type=local,dest=/tmp/bcache --cache-from type=local,src=/tmp/bcache .
```

`mode=max` exports intermediate stage cache too - much better hit rates for multi-stage builds, at
the cost of a larger cache artefact. A missing `--cache-from` source is **non-fatal**: the build
silently proceeds uncached, so a typo shows up as a slow build rather than an error.

Secrets and SSH, never build args:

```bash
docker buildx build --secret id=npmrc,src=$HOME/.npmrc .
docker buildx build --secret id=token,env=GITHUB_TOKEN .
docker buildx build --ssh default .
```

## Bake

Declarative multi-target builds from `docker-bake.hcl` / `docker-bake.json` (or the `x-bake`
extension in a Compose file).

```bash
docker buildx bake --print                                # resolved plan, builds nothing
docker buildx bake
docker buildx bake <target> --push
docker buildx bake -f docker-bake.hcl --set '*.platform=linux/amd64,linux/arm64'
```

`--print` first, always - it renders the fully-resolved targets, including inherited groups and
variable interpolation, so you can see what would actually build.

## Imagetools - inspect and assemble manifests

```bash
docker buildx imagetools inspect alpine:3.20
docker buildx imagetools inspect alpine:3.20 --raw | jq
docker buildx imagetools inspect alpine:3.20 --format '{{json .Manifest}}' | jq
```

This reads **from the registry** without pulling the image - the right tool for "what platforms
does this tag publish?" and for verifying a push landed.

Combining per-platform tags into one index:

```bash
docker buildx imagetools create --dry-run -t org/app:v1 org/app:v1-amd64 org/app:v1-arm64
docker buildx imagetools create -t org/app:v1 org/app:v1-amd64 org/app:v1-arm64
docker buildx imagetools create --append -t org/app:v1 org/app:v1-s390x
docker buildx imagetools create --annotation "index:org.opencontainers.image.source=..." -t org/app:v1 <srcs>
```

`create` **only stitches manifests** - it cannot upload missing layers, so every source must
already be pushed. It pushes the result immediately; `--dry-run` prints the resulting index
instead, which is the step to show the user before publishing.

### Replacing `docker manifest`

`docker manifest` is explicitly experimental and lacks OCI index and attestation support. Use
imagetools instead:

| `docker manifest ...` | Use instead |
|---|---|
| `inspect REF` | `docker buildx imagetools inspect REF` |
| `inspect --verbose REF` | `docker buildx imagetools inspect REF --raw` |
| `create NEW SRC...` then `push NEW` | `docker buildx imagetools create -t NEW SRC...` (pushes) |
| `create --amend NEW SRC...` | `docker buildx imagetools create --append -t NEW SRC...` |
| `annotate NEW SRC --os/--arch` | `docker buildx imagetools create --annotation ... -t NEW SRC...` |
| `push NEW` | not needed - `create` pushes |
| `rm NEW` | not needed - `create` overwrites |

## Build cache and disk

```bash
docker buildx du
docker buildx du --verbose
docker buildx prune                                       # dangling cache
docker buildx prune -a --force                            # everything
docker buildx prune --filter 'until=168h'
docker buildx prune --reserved-space 10GB
```

`buildx du` sees a non-default builder's own cache, which `docker system df` does not - on a
machine with a `docker-container` builder the two disagree, and `buildx du` is the one telling the
truth about that builder. Reclaim with `buildx prune`, or `docker builder prune` where buildx is
absent (see `reference/images.md`).

## Build history

```bash
docker buildx history ls
docker buildx history inspect <ref>
docker buildx history logs <ref>
docker buildx history rm <ref>
```

Recorded builds, with timing per step - the way to answer "why did that build take nine minutes"
after the fact. Availability depends on the driver and buildx version; `ls` returning nothing
means no records, not an error.
