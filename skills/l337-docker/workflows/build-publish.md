# Build and publish workflows

## Pick the right tag to deploy

Choosing a version without pulling anything. Full command detail in `reference/registry.md`.

1. **Enumerate tags.** They come back in **lexical** order, so the newest is not the last entry:
   ```bash
   REPO=library/nginx
   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$REPO:pull" | jq -r .token)
   curl -s -H "Authorization: Bearer $TOKEN" "https://registry-1.docker.io/v2/$REPO/tags/list?n=1000" | jq -r '.tags[]'
   ```
   On Docker Hub, `?ordering=last_updated` on the Hub API answers "what shipped most recently",
   which lexical order cannot.
2. **Filter to stable releases** - drop floating tags (`latest`, `edge`, `nightly`, `stable`) and
   pre-releases (`-rc`, `-beta`, `-alpha`), then sort by version:
   ```bash
   ... | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -5
   ```
3. **Confirm it exists and capture the immutable digest:**
   ```bash
   docker buildx imagetools inspect nginx:<tag>
   ```
   Note the platforms. Filter `unknown/unknown` entries - those are attestations, not platforms.
4. **Vet what the tag actually contains** before letting it near a host - user, entrypoint,
   exposed ports, labels - via the registry config blob (`reference/registry.md`). Running as
   root or an unexpected exposed port is worth catching here rather than after deployment.
5. **On Docker Hub**, sanity-check provenance and pull budget:
   ```bash
   curl -s "https://hub.docker.com/v2/repositories/$REPO/" | jq '{star_count,pull_count,last_updated}'
   ```
6. Recommend the tag **and its digest**. For anything security-sensitive, deploy the digest - a
   tag can be repointed, a digest cannot. Do not pull as part of this.

## Multi-platform build and push

1. **Confirm the builder can do it.** The default `docker` driver **cannot** build
   multi-platform - the most common failure here:
   ```bash
   docker buildx ls --format json | jq -rs '.[] | [.Name,.Driver,(.Current|tostring)] | @tsv'
   ```
   If the active driver is `docker`, create a proper builder:
   ```bash
   docker buildx create --name multi --driver docker-container --use --bootstrap
   ```
2. **Check every base image publishes the target platforms.** A missing platform silently falls
   back to QEMU emulation - builds that take twenty minutes instead of two, or fail obscurely:
   ```bash
   docker buildx imagetools inspect <base> --raw \
     | jq -r '.manifests[] | select(.platform.os != "unknown") | "\(.platform.os)/\(.platform.architecture)"'
   ```
3. **Build, attest and push in one step:**
   ```bash
   docker buildx build \
     --platform linux/amd64,linux/arm64 \
     -t ghcr.io/org/app:v1 \
     --provenance mode=max --sbom true \
     --push .
   ```
   **`--load` cannot be combined with multi-platform** - the local store holds one platform per
   tag. Results live in the registry only. Without `--push` or `--load` the build output is
   discarded entirely.
4. **Verify what was actually published** - do not trust the build's exit code alone:
   ```bash
   docker buildx imagetools inspect ghcr.io/org/app:v1 --raw \
     | jq -r '.manifests[] | select(.platform.os != "unknown") | "\(.platform.os)/\(.platform.architecture)"'
   ```
5. Report every requested platform against what the index actually contains, and surface anything
   skipped or emulated before declaring success.

Add registry cache across CI runs when builds are slow:
`--cache-from type=registry,ref=...:cache --cache-to type=registry,ref=...:cache,mode=max`. A missing
cache source is non-fatal, so a typo shows up as a slow build rather than an error.

## Inspect a manifest list

```bash
docker buildx imagetools inspect <ref>                    # human-readable
docker buildx imagetools inspect <ref> --raw | jq         # raw JSON
```

Read `.mediaType` to know what you have:

- `application/vnd.oci.image.index.v1+json` or `...manifest.list.v2+json` -> **multi-platform index**.
  Report each entry's platform and digest.
- `application/vnd.oci.image.manifest.v1+json` or `...manifest.v2+json` -> **single image**. Report
  architecture, OS and layer count.

`unknown/unknown` entries are **attestation manifests** (provenance, SBOM), not broken platforms.
To read one, inspect its digest:

```bash
docker buildx imagetools inspect <ref>@<attestation-digest> --raw | jq
```

Use `imagetools`, not `docker manifest` - the latter is experimental and has no OCI index or
attestation support.

## Create a manifest list from per-platform tags

1. **Confirm every source is already pushed.** `imagetools create` only stitches manifests; it
   cannot upload missing layers:
   ```bash
   docker buildx imagetools inspect org/app:v1-amd64
   docker buildx imagetools inspect org/app:v1-arm64
   ```
2. **Dry run and show the user** which platforms will publish under the combined tag:
   ```bash
   docker buildx imagetools create --dry-run -t org/app:v1 org/app:v1-amd64 org/app:v1-arm64
   ```
3. **After approval**, repeat without `--dry-run`. It pushes immediately - there is no separate
   push step, and it **overwrites** an existing tag without prompting.
4. **Verify** the published index contains every expected platform, and report its digest.

## Migrating off `docker manifest`

`docker manifest` is experimental and lacks OCI index, attestation and annotation support.

| `docker manifest ...` | Use instead |
|---|---|
| `inspect REF` | `docker buildx imagetools inspect REF` |
| `inspect --verbose REF` | `docker buildx imagetools inspect REF --raw` |
| `create NEW SRC...` + `push NEW` | `docker buildx imagetools create -t NEW SRC...` (pushes) |
| `create --amend NEW SRC...` | `docker buildx imagetools create --append -t NEW SRC...` |
| `annotate NEW SRC --os/--arch` | `docker buildx imagetools create --annotation ... -t NEW SRC...` |
| `push NEW` | not needed - `create` pushes |
| `rm NEW` | not needed - `create` overwrites |

When unsure of the current shape, run `docker buildx imagetools inspect REF --raw` first.
