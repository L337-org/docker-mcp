# Registries - querying without pulling

Answering "what tags exist", "what is the digest", "what platforms does this publish" **without
pulling**. Two routes: the CLI where it suffices, and the registry HTTP API where it does not.

## Prefer the CLI where it works

```bash
docker buildx imagetools inspect alpine:3.20              # platforms, digests, human-readable
docker buildx imagetools inspect alpine:3.20 --raw | jq   # raw manifest/index JSON
```

This reads from the registry, handles auth from `~/.docker/config.json`, works on any registry,
and needs no token juggling. **Use it for anything manifest-shaped.** Drop to HTTP only for what
it cannot do: listing tags, catalog enumeration, and Hub metadata.

`docker search` is Docker Hub only and returns repositories, not tags - not a substitute.

## Registry HTTP API (OCI distribution spec)

Anonymous pulls still need a token; the registry replies `401` with a `WWW-Authenticate` header
naming the auth service. For Docker Hub:

```bash
REPO=library/alpine
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$REPO:pull" | jq -r .token)
```

Official Hub images live under `library/` - `alpine` is `library/alpine`. A user image is
`user/name` as written.

### Tags

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://registry-1.docker.io/v2/$REPO/tags/list?n=100" | jq -r '.tags[]'
```

Tags come back in **lexical order, not chronological** - `1.10` sorts before `1.9`, and the newest
tag is not the last entry. Sort properly before picking:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "https://registry-1.docker.io/v2/$REPO/tags/list?n=1000" \
  | jq -r '.tags[]' \
  | grep -E '^[0-9]+\.[0-9]+(\.[0-9]+)?$' \
  | sort -V | tail -5
```

Filter out floating tags (`latest`, `edge`, `nightly`, `stable`) and pre-releases (`-rc`, `-beta`,
`-alpha`) before recommending one. Large repos paginate via a `Link` header.

### Manifests, digests and platforms

Send an `Accept` header listing the index media types, or the registry may return a single
platform manifest instead of the index:

```bash
ACCEPT='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json'

curl -s -H "Authorization: Bearer $TOKEN" -H "Accept: $ACCEPT" \
  "https://registry-1.docker.io/v2/$REPO/manifests/3.20" | jq
```

Resolve a tag to its immutable digest without downloading the body:

```bash
curl -sI -H "Authorization: Bearer $TOKEN" -H "Accept: $ACCEPT" \
  "https://registry-1.docker.io/v2/$REPO/manifests/3.20" | grep -i docker-content-digest
```

Reading the response:

- `...image.index.v1+json` / `...manifest.list.v2+json` -> **multi-platform index**; each
  `.manifests[]` has `.platform` and `.digest`.
- `...image.manifest.v1+json` / `...manifest.v2+json` -> **single image**; `.config` and `.layers`.

**`unknown/unknown` platform entries are attestations, not broken images** - provenance and SBOM
records attached by buildx. Filter them out when reporting supported platforms:

```bash
... | jq -r '.manifests[] | select(.platform.os != "unknown") | "\(.platform.os)/\(.platform.architecture) \(.digest)"'
```

### Image config - entrypoint, user, env, labels, without pulling

Two hops: fetch the platform-specific manifest, then its config blob.

```bash
# 1. pick the per-platform manifest digest from the index
MDIGEST=$(curl -s -H "Authorization: Bearer $TOKEN" -H "Accept: $ACCEPT" \
  "https://registry-1.docker.io/v2/$REPO/manifests/3.20" \
  | jq -r '.manifests[] | select(.platform.os=="linux" and .platform.architecture=="amd64") | .digest')

# 2. that manifest names the config blob
CDIGEST=$(curl -s -H "Authorization: Bearer $TOKEN" -H "Accept: $ACCEPT" \
  "https://registry-1.docker.io/v2/$REPO/manifests/$MDIGEST" | jq -r '.config.digest')

# 3. fetch it
curl -sL -H "Authorization: Bearer $TOKEN" \
  "https://registry-1.docker.io/v2/$REPO/blobs/$CDIGEST" \
  | jq '{user:.config.User, entrypoint:.config.Entrypoint, cmd:.config.Cmd, exposed:.config.ExposedPorts, labels:.config.Labels}'
```

`-L` matters on step 3 - blob fetches usually redirect to a CDN. This is how you vet what a tag
contains (does it run as root? what does it expose?) before allowing it near a host.

### Other registries

Same protocol, different auth endpoint. GHCR accepts anonymous tokens for public repos:

```bash
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:ORG/IMAGE:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" "https://ghcr.io/v2/ORG/IMAGE/tags/list" | jq
```

Discover the right endpoint from the challenge rather than guessing:

```bash
curl -sI https://<registry>/v2/ | grep -i www-authenticate
```

ECR, ACR and GAR use cloud-provider credentials; get a token from their own CLI
(`aws ecr get-login-password`, `az acr login`, `gcloud auth`) rather than the pattern above.

## Docker Hub metadata API

A different API from the registry - repository metadata, not manifests. No auth for public repos.

```bash
curl -s "https://hub.docker.com/v2/repositories/library/nginx/" \
  | jq '{name,star_count,pull_count,last_updated,description}'

curl -s "https://hub.docker.com/v2/repositories/library/nginx/tags?page_size=25&ordering=last_updated" \
  | jq -r '.results[] | "\(.name)\t\(.last_updated)\t\(.full_size)"'
```

**`ordering=last_updated` is the reason to use this over the registry tags list** - it answers
"what shipped most recently", which lexical tag order cannot. It also gives sizes and per-platform
detail. Hub only, though; for any other registry use the tags endpoint above.

Star and pull counts are the quick provenance sanity check on an unfamiliar image.

## Pull rate limits

Docker Hub throttles anonymous and free-tier pulls. Check before a bulk pull, and check it first
when pulls start failing with `toomanyrequests`:

```bash
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:ratelimitpreview/test:pull" | jq -r .token)
curl -sI -H "Authorization: Bearer $TOKEN" \
  https://registry-1.docker.io/v2/ratelimitpreview/test/manifests/latest | grep -i ratelimit
```

Returns e.g. `ratelimit-limit: 100;w=3600` and `ratelimit-remaining: 99;w=3600` - the window is in
seconds. Authenticated pulls get a higher limit, so `docker login` is itself a fix. The check
consumes one pull from the budget.

## Waiting for a tag to appear

Polling a registry after triggering a build or a release:

```bash
for i in $(seq 1 60); do
  if docker buildx imagetools inspect "$REF" >/dev/null 2>&1; then echo "published"; break; fi
  sleep 10
done
```

Always bound the loop and report a timeout as a timeout, never as success. See
`reference/observability.md` for the general waiting pattern.
