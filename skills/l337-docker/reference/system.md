# Daemon, contexts, remote hosts, plugins, auth

## Daemon state

```bash
docker version                                            # client AND server versions
docker version --format '{{.Server.Version}} {{.Server.APIVersion}}'
docker system info
docker system info --format '{{.Name}} {{.OperatingSystem}} {{.ServerVersion}}'
docker system info --format '{{.Containers}} running={{.ContainersRunning}} images={{.Images}}'
docker system info --format '{{json .Swarm}}' | jq
docker system info --format '{{.SecurityOptions}}'
```

`docker version` failing to report a **Server** section means the client cannot reach a daemon -
that is the ping. There is no separate ping command; use this.

Client/server version skew matters: a new CLI against an old daemon fails on flags that map to
unsupported API versions, with errors that look like typos. Report both versions when something
inexplicable fails.

## Disk usage

```bash
docker system df
docker system df -v                                       # per-image/container/volume breakdown
docker system df --format json | jq -s '.'
```

Four buckets - Images, Containers, Local Volumes, Build Cache - each with `RECLAIMABLE`. The fix
differs per bucket, so always identify the dominant one before recommending anything
(`workflows/maintenance.md`).

`docker system df` does **not** see a `docker-container` buildx builder's cache. Cross-check with
`docker buildx du` on any machine that builds.

`-v` is what attributes usage to specific objects; the summary alone cannot tell you *which* image
is the problem.

## Events

```bash
docker events --since 30m --until 0s
docker events --since 30m --until 0s --format json | jq -s '.'
docker events --since 30m --until 0s --filter type=container --filter event=die
docker events --since 2026-08-13T09:00:00 --until 2026-08-13T10:00:00
```

**`docker events` with no `--until` streams forever** and will hang the session. Always bound it.

`--since`/`--until` accept an RFC3339 timestamp, a Unix epoch integer, or a Go duration counted
back from now (`30m`, `2h`, `0s`). **`--until now` is not valid** - it fails with "failed to parse
value as time or duration". Use `--until 0s` (or `--until $(date +%s)`) to mean "up to now".

Filters: `type=` (container/image/volume/network/daemon/plugin/service/node/secret/config),
`event=` (`die`, `oom`, `kill`, `health_status`, `start`, `destroy`), `container=`, `image=`,
`label=`.

This is the timeline for incident triage - what changed and when, in order. `die` + `oom` +
`health_status: unhealthy` is the high-signal filter set. Tight `start`/`die` pairs on one
container are a crash loop.

To *block until something happens* rather than read history, see `reference/observability.md`.

## Registry authentication

```bash
docker login                                              # Docker Hub
docker login ghcr.io -u USERNAME --password-stdin < token.txt
echo "$TOKEN" | docker login ghcr.io -u USERNAME --password-stdin
docker logout ghcr.io
```

**Never pass a credential with `-p`/`--password`** - it lands in shell history and in the process
list, and Docker prints a warning saying so. `--password-stdin` is the only acceptable form. Use a
token/PAT rather than an account password wherever the registry supports one.

Credentials live in `~/.docker/config.json` (or an OS keychain via a credential helper). Check
what is authenticated:

```bash
jq '.auths | keys' ~/.docker/config.json
jq '.credsStore, .credHelpers' ~/.docker/config.json
```

`docker logout <registry>` removes that entry. There is no server-side session, so logout is
purely local.

Scout, private pulls and `stack deploy --with-registry-auth` all depend on this being set up.

## Contexts and remote daemons

```bash
docker context ls
docker context ls --format json | jq -s '.'
docker context inspect <name>
docker context create prod --docker host=ssh://ops@prod.example.com --description "prod"
docker context create tls-prod --docker "host=tcp://10.0.0.5:2376,ca=~/.docker/ca.pem,cert=~/.docker/cert.pem,key=~/.docker/key.pem"
docker context create staging --from prod
docker context rm <name>
```

### Targeting, without changing global state

```bash
docker --context prod ps                                  # preferred: explicit, per-invocation
docker -H ssh://ops@prod.example.com ps                   # ad-hoc, no context needed
DOCKER_HOST=tcp://10.0.0.5:2376 DOCKER_TLS_VERIFY=1 DOCKER_CERT_PATH=~/.docker/certs docker ps
docker context use prod                                   # MODAL - changes the default for everything
```

`docker context use` is **global and persistent** - it changes the target for every process and
every later session on the machine, silently. A later command that "looks local" is not. Use
`docker --context <name>` for one-off work; reserve `context use` for when the user explicitly
asks to move their default, and confirm it as a persistent change.

Precedence when several are set: `-H`/`--host` beats `--context`, which beats `DOCKER_HOST`, which
beats the current context. `DOCKER_HOST` silently overriding a context the user set is a common
source of "I'm on the wrong daemon".

Always report which daemon a result came from when more than one is in play.

### SSH endpoints

`ssh://user@host` needs key-based auth and a `docker` binary the remote user can run. It uses the
**system** `ssh` client, so `~/.ssh/config` applies - `Host`, `Port`, `IdentityFile`,
`ProxyJump`/`ProxyCommand` all work, which is how bastion setups are handled.

```bash
ssh ops@prod docker version                               # test the SSH path independently first
docker -H ssh://ops@prod ps
```

When `docker -H ssh://...` fails, test plain `ssh` first. It separates an SSH/auth problem from a
Docker problem, and they present almost identically.

### TLS endpoints

```bash
docker -H tcp://10.0.0.5:2376 --tlsverify \
  --tlscacert ca.pem --tlscert cert.pem --tlskey key.pem ps
```

`2376` is TLS, `2375` is **plaintext and unauthenticated** - anyone who can reach that port has
root on the host. If you find a daemon on 2375, flag it as a serious finding rather than using it
quietly. Never disable verification to make a connection work.

## Plugins (managed volume/network/log drivers)

These are daemon plugins, not CLI plugins - a different thing from `compose`/`buildx`/`scout`.

```bash
docker plugin ls
docker plugin ls --filter enabled=true
docker plugin inspect <plugin>
docker plugin install vieux/sshfs                         # prompts for permissions
docker plugin install --grant-all-permissions --alias sshfs vieux/sshfs
docker plugin install --disable vieux/sshfs               # install without enabling
docker plugin enable <plugin>
docker plugin disable <plugin>                            # must be unused
docker plugin set <plugin> DEBUG=1                        # only while disabled
docker plugin upgrade <plugin> [REMOTE]                   # must be disabled first
docker plugin rm <plugin>
docker plugin push <user>/<plugin>:<tag>
docker plugin create <user>/<plugin>:<tag> ./plugin-dir   # needs config.json + rootfs/
```

- Installing a plugin **grants it privileges on the host** (mounts, network, devices, capabilities).
  Show the permission list and get confirmation; `--grant-all-permissions` skips exactly the prompt
  a human should be reading.
- There is **no read-only `docker plugin` subcommand that prints those privileges**, and
  `docker plugin inspect` only works once the plugin is installed, which is too late. Read them
  straight out of the registry instead, before installing anything - see below.
- Ordering is strict: `disable` -> `set`/`upgrade` -> `enable`. Doing it out of order gives errors
  that read like the plugin is broken.
- `disable` fails while a volume or network still uses the plugin - find and remove those first.
- `docker plugin push` works normally here. (The docker-py SDK's plugin push is broken upstream -
  it POSTs to the pull URL - so on the CLI path this is one of the operations that is *easier*, not
  harder.)

### Reading a plugin's privileges before installing it

A plugin is an OCI artifact whose config blob *is* its plugin config, so the privileges the install
prompt would ask you to grant are readable over plain HTTPS. Same token dance as
`reference/registry.md`, one hop shorter - a plugin manifest names a single config blob, with no
per-platform index to walk first:

```bash
PLUGIN=vieux/sshfs          # repository only; the tag is separate, see below
TAG=latest
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$PLUGIN:pull" | jq -er .token)

CDIGEST=$(curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  "https://registry-1.docker.io/v2/$PLUGIN/manifests/$TAG" | jq -er '.config.digest')

curl -sL -H "Authorization: Bearer $TOKEN" \
  "https://registry-1.docker.io/v2/$PLUGIN/blobs/$CDIGEST" \
  | jq -e '[
      (select((.Network.Type // "") as $t | $t != "" and $t != "null" and $t != "bridge")
         | {name: "network", value: [.Network.Type]}),
      (select(.IpcHost) | {name: "host ipc namespace", value: ["true"]}),
      (select(.PidHost) | {name: "host pid namespace", value: ["true"]}),
      (.Mounts[]?        | select(.Source != null) | {name: "mount",  value: [.Source]}),
      (.Linux.Devices[]? | select(.Path   != null) | {name: "device", value: [.Path]}),
      (select(.Linux.AllowAllDevices) | {name: "allow-all-devices", value: ["true"]}),
      (select((.Linux.Capabilities // []) | length > 0)
         | {name: "capabilities", value: .Linux.Capabilities})
    ]'
```

For `vieux/sshfs` that reports host networking, a bind mount of `/var/lib/docker/plugins/`, a second
mount with an empty source, `/dev/fuse` and `CAP_SYS_ADMIN` - the same list, in the same order, that
the install prompt shows.

**The filter deliberately mirrors the daemon's own `computePrivileges`, and all seven cases matter.**
It is tempting to pull out only network, mounts, devices and capabilities, because that is what a
typical plugin declares. Doing so silently drops `host ipc namespace`, `host pid namespace` and
`allow-all-devices` - and `allow-all-devices` grants `rwm` on every device on the host, which is the
single most important thing this check exists to surface. A privilege review that omits the worst
privilege is worse than none, because it reads as a clean bill of health. Keep all seven.

Other traps, each of which fails quietly:

- The config keys are **capitalised** (`Mounts`, `Linux`, `Network`), unlike an image config's
  lower-case `config` block. Reading `.mounts` gives `null`, which looks like "asks for nothing"
  rather than an error.
- `jq -e` on the first two steps, so a missing `.token` or `.config.digest` fails there rather than
  substituting `null` into the next URL and failing somewhere confusing. The final `-e` is harmless:
  an empty privilege list is `[]`, which is truthy, so a plugin that genuinely asks for nothing
  still exits 0.
- `-L` on the blob fetch - it redirects to a CDN.
- **Repository and tag are separate variables.** The tag defaults to `latest` on the CLI but not in
  a manifest URL, and folding it into `$PLUGIN` would also corrupt the token scope
  (`repository:vieux/sshfs:latest:pull`), which fails as an auth error rather than as a bad tag.
- A mount whose `Source` is `null` is not a privilege and is skipped, matching the daemon. An
  **empty** source is: the plugin declares the mount point and leaves the host path for the operator
  to supply at install time.
- Network type `bridge`, `null` or absent is not a privilege either, so a plugin using the default
  network correctly reports nothing for it.
- This reads the **remote** plugin. For one already installed, `docker plugin inspect <plugin>`
  gives the same fields from local state.

## No equivalent needed: connection management

An MCP server pools daemon connections and needs explicit close/reconnect operations. The CLI
opens a fresh connection per invocation, so there is nothing to close and nothing to reconnect -
a stale-connection problem cannot occur, and no command is missing.
