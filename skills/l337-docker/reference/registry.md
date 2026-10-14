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

One request returns at most one page. A registry that has more says so in a `Link` header, and
only following it gives the full list - without it "the newest tag" can be one you never saw:

```bash
REGISTRY=registry-1.docker.io
url="https://$REGISTRY/v2/$REPO/tags/list?n=1000" pages=0
while [ -n "$url" ] && [ "$pages" -lt 50 ]; do
  hdr=$(mktemp)
  curl -sf -D "$hdr" -H "Authorization: Bearer $TOKEN" "$url" | jq -er '.tags[]' \
    || { echo "FAIL: $url" >&2; rm -f "$hdr"; break; }
  next=$(grep -i '^link:' "$hdr" | sed -E 's#.*<(https?://[^/]*)?(/v2/[^>]*)>.*#\2#' | tr -d '\r')
  rm -f "$hdr"; pages=$((pages + 1))
  url=${next:+https://$REGISTRY$next}
done > tags.txt
[ -n "$url" ] && echo "TRUNCATED after $pages pages: more tags exist" >&2
```

The next-page URL comes from the registry's response, so only its path is kept and it is fetched
from the same registry: a `Link` naming another host is not followed. Report a truncation rather
than presenting a partial list as complete.

Tags come back in **lexical order, not chronological** - `1.10` sorts before `1.9`, and the newest
tag is not the last entry. Sort properly before picking:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "https://registry-1.docker.io/v2/$REPO/tags/list?n=1000" \
  | jq -r '.tags[]' \
  | grep -E '^[0-9]+\.[0-9]+(\.[0-9]+)?$' \
  | sort -V | tail -5
```

Filter out floating tags (`latest`, `edge`, `nightly`, `stable`) and pre-releases (`-rc`, `-beta`,
`-alpha`) before recommending one. On a large repository, sort the paginated `tags.txt` above
rather than one page.

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

A tag names either an index (one manifest per platform) or a single image's manifest, and the
config blob hangs off the per-platform manifest. This handles both shapes, and stops with a
`FAIL` line rather than passing an empty digest on to the next request:

```bash
image_config() {
  local repo="$1" ref="$2" arch="${3:-amd64}" reg=https://registry-1.docker.io token manifest digest
  local accept='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json'
  accept="$accept,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"
  token=$(curl -sf "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$repo:pull" \
          | jq -er .token) || { echo "FAIL: no token for $repo"; return 1; }
  # 1. the tag's manifest: an index (multi-platform) or already a single image
  manifest=$(curl -sf -H "Authorization: Bearer $token" -H "Accept: $accept" "$reg/v2/$repo/manifests/$ref") \
    || { echo "FAIL: no manifest for $repo:$ref"; return 1; }
  # 2. an index names one manifest per platform: follow linux/$arch
  if printf '%s' "$manifest" | jq -e 'has("manifests")' >/dev/null; then
    digest=$(printf '%s' "$manifest" | jq -er --arg a "$arch" \
             'first(.manifests[] | select(.platform.os=="linux" and .platform.architecture==$a)) | .digest') \
      || { echo "FAIL: $repo:$ref publishes no linux/$arch"; return 1; }
    manifest=$(curl -sf -H "Authorization: Bearer $token" -H "Accept: $accept" "$reg/v2/$repo/manifests/$digest") \
      || { echo "FAIL: no manifest for $repo@$digest"; return 1; }
  fi
  digest=$(printf '%s' "$manifest" | jq -er '.config.digest') || { echo "FAIL: manifest names no config"; return 1; }
  # 3. the config blob; -L because blob fetches redirect to a CDN
  curl -sfL -H "Authorization: Bearer $token" "$reg/v2/$repo/blobs/$digest" | jq -e \
    '{user:.config.User, entrypoint:.config.Entrypoint, cmd:.config.Cmd, exposed:.config.ExposedPorts, labels:.config.Labels}'
}
```

`image_config library/alpine 3.20` (or `... 3.20 arm64`). This is how you vet what a tag contains
(does it run as root? what does it expose?) before allowing it near a host.

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

### Private repositories

A private repository needs a token issued against a username and an access token. Keep both out
of the command line, where they would land in shell history, the process list and this
transcript: `curl -K -` reads them from standard input instead, here from files the user supplies.

```bash
REGISTRY=registry.example.com REPO=team/app
curl -sI "https://$REGISTRY/v2/" | grep -i www-authenticate     # read realm= and service=
REALM=https://auth.example.com/token SERVICE=registry.example.com
TOKEN=$(printf 'user = "%s:%s"\n' "$(cat ./registry-user)" "$(cat ./registry-token)" \
  | curl -sf -K - "$REALM?service=$SERVICE&scope=repository:$REPO:pull" | jq -er .token)
```

The realm comes from the registry's reply, so check it before sending it a credential: it must be
`https`, and it must not be a loopback or private address unless the registry itself is one. A
realm that fails either check is a reason to stop and tell the user, not to send the credential.
`printf` is a shell builtin, so the credential never appears in any command's arguments.

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
costs nothing: Docker's documentation says "Using GET emulates a real pull and counts towards the
limit. Using HEAD won't." Keep the `-I` (HEAD) for that reason.

## Waiting for a tag to appear

Use `wait_tag` in `reference/observability.md`: it polls `docker buildx imagetools inspect` with a
timeout and reports a timeout as a timeout, never as success.
