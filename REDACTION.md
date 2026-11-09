# Credential redaction

**Redaction reduces accidental exposure. It is not access control.**  It keeps credentials out of what
this server returns to an agent that reaches Docker only through this server, so the agent does not
see them by accident.  It cannot stop an agent that goes looking: an agent that can run commands in a
container (`container_exec`, `compose_exec`) can print a credential in a form no filter recognises.
Run it with `DOCKER_MCP_SERVER_READONLY`, or mark a redacted host `(ro,redact)`, which removes those
routes.

## Turning it on

Redaction is off by default.  There are two ways to turn it on, and they combine.

- **Every host:** `DOCKER_MCP_SERVER_REDACT=1` (also `true`, `yes` or `on`).
- **Chosen hosts:** the `redact` option on a host in `DOCKER_MCP_SERVER_HOSTS`, so that a production
  daemon is redacted while a local one stays unfiltered:

  ```text
  DOCKER_MCP_SERVER_HOSTS=dev=local, prod=ssh://ops@prod.example.com(ro,redact)
  ```

  A host's options go in one comma-separated bracket, `(ro,redact)`, or one per bracket,
  `(ro)(redact)`, and the two forms mix freely.  They are case-insensitive and may come in any
  order, except that `tls=<dir>` takes the rest of its bracket, so that a certificate directory
  containing a comma still works, and therefore has to come last in a list:
  `(ro,redact,tls=/etc/docker/prod)`.  A malformed bracket, such as `(ro,)` or one that is never
  closed, stops the server at startup with an error naming the host.

With the global switch on, every host is redacted whatever its own options say.

## Which names are treated as credentials

Values of variables with credential-like names are treated as credentials, in container, image and
service environments and in Compose `.env` files.  The built-in patterns are case-insensitive globs:

`*PASSWORD*`, `*PASSWD*`, `*PASSPHRASE*`, `*PASS`, `*_PASS_*`, `*SECRET*`, `*TOKEN*`, `*KEY*`,
`*CERT*` and `*CREDENTIAL*`.

Some names those patterns match are left alone by default, because what they hold is public: public
keys, and the signing-key fingerprints official images publish under `GPG_KEY`:

`*PUBLIC_KEY`, `*PUBKEY`, `*_PUB_KEY`, `GPG_KEY` and `GPG_KEYS`.

A value that is an absolute path, such as `SSL_CERT_FILE=/etc/ssl/cert.pem` or
`POSTGRES_PASSWORD_FILE=/run/secrets/db`, is not treated as a credential either: it says where one is.
That covers a value made only of path characters starting `/` or `~/`, so a password that happens to
look like a path, such as `/Summer2025`, is not harvested; the shape rules still catch it if it looks
random.

`DOCKER_MCP_SERVER_REDACT_NAMES` changes that list.  It is comma-separated, and each entry is a
variable name or a glob using `*`, `?` and `[...]`, matched without regard to case:

```text
DOCKER_MCP_SERVER_REDACT_NAMES=UPSTREAM_*, *_REF, !SORT_KEY
```

- An entry adds names to the built-in patterns: `UPSTREAM_*` treats `UPSTREAM_URL` as a credential.
  An entry also overrides the names left alone by default: `GPG_KEY` harvests it again.
- An entry starting `!` excludes names, whichever pattern would otherwise match them: `!SORT_KEY`
  stops a harmless `SORT_KEY` being treated as one.  An exclusion also wins over an addition.
- It takes names only, never values.  An entry containing `=` stops the server at startup, and the
  error gives the entry's position, not its text, in case it holds a credential.

The names setting does nothing on its own.  Setting it with redaction on for no host logs a warning
at startup and changes nothing.

## Where redaction applies

Every tool result, every resource body and every error message from a redacted host is redacted.  Each
call is judged by the host it reaches: a tool by its `host` argument, or the default host when it names
none; a resource by the host in its URI, or the default host for the forms without one.

- **Output from a host that is not redacted is never redacted**, even where it contains a value read
  from a redacted host.  Redacting it would tell an agent that a credential it can read in full on one
  host is also used on the redacted one.
- **Tools that reach no daemon are not redacted:** `registry_*`, `hub_*`, `context_*`, `docs_lookup`,
  `host_list` and `tool_list`.
- **Raw bytes cannot be redacted** without corrupting the archive they belong to.  With
  `DOCKER_MCP_SERVER_REDACT` on, `container_archive_get` is not offered at all, and `container_export`
  and `image_save` require `dest_path`.  On a host marked `redact`, the same calls are refused.  The
  files that `container_archive_get_to_file`, `container_export` and `image_save` write are not
  redacted, and they are written on the machine running the server, even when the daemon is remote.

The server reads the values to redact from the redacted host's daemon: values under a credential name
in container, image and service environments, in build arguments recorded in image history, and in a
Compose project's `.env` file (read by Compose's quoting and comment rules, but without its escape
sequences in double quotes, variable interpolation or values spanning lines); and the content of Swarm
configs whose name is a credential name.  On each
call it compares the IDs of the daemon's containers, images, services (with their versions) and configs
with the last call's, and reads again if they differ, so a container created a moment ago is covered.  If
reading fails, the call is refused rather than answered with output that might not be redacted.

A value read this way is redacted wherever it appears, however word-like.  One shorter than eight
characters is redacted only where no letter, digit or underscore adjoins it, so that `pw` is replaced in
`[pw]` or `pw.` but not inside `pwd`, which would also give the short value away.

While redaction is on anywhere, the server's instructions say so in one line and point the agent to
`host_list`, whose `redaction` notes give each redacted host's rules.  On a host that accepts writes:
that a marker can be passed wherever a write needs the credential, and which markers are refused; that
a marker must never be written to a file, since a call reading one is refused; the tools whose bytes in
band are refused; and the tools whose files written to disk are not redacted.  On a read-only host: that
a marker must not be passed to a tool or written to a file, since it is never swapped back there; and the
tools whose bytes in band are refused.  The notes name only the tools that are registered and allowed on
that host.

## What is redacted, and how

Two things are replaced, wherever they appear in a redacted host's output, dict keys included:

- **Values the server knows**, read from where the daemon reports them under a credential name, in
  their raw form and as base64 (standard and URL-safe, padded or not, and inside a larger base64 blob
  at any alignment), base32, hex in either case, and URL-encoded.  A value of six characters or fewer,
  base64-encoded inside a larger blob, can be missed there: the part of its encoding that does not
  depend on its neighbours is too short to match safely.
- **Values found by their shape or context**: PEM private keys, GitHub, Stripe and Slack tokens, AWS
  access key IDs and JWTs; `Authorization`, `Proxy-Authorization`, cookie and API-key header values;
  passwords in a URL's user info or a connection string; the value assigned to a credential name, when
  it is 8 or more characters and looks random; the value after a credential-named command-line flag,
  such as `--password`, whatever it looks like, including where Docker reports a command as a list; and
  any other run of 32 or more base64 characters that looks random, except the body of an OpenSSH
  public key and the inside of a PEM public key or certificate block, which are public.  Where two of
  these overlap, the whole of both is replaced as one.  Hex alone is never treated as a credential by
  these rules, nor is a run longer than 16 KB, and lines wrapped by a base64 encoder count as one run.
  A value found this way is remembered, so its encoded forms are matched from then on.

Each replacement is a marker, `[REDACTED:<token>]`, where the token is 22 characters.  Every marker is
different, even for the same credential in the same result, so nothing in the output shows that two
places hold the same value; and a marker says nothing about the credential's format or how it was
found.  Two consequences to expect: the same call returns different text each time, even when nothing
has changed; and an agent comparing two configurations cannot tell whether their credentials match, so
in triage it may report credentials as mismatched or misconfigured when they are not.  That is the
design working, not a fault.

## What it misses, and what it replaces by mistake

Measured by planting credentials and decoys on a daemon, then calling the list, inspect, logs, top,
exec, image, history, Compose, service, events and disk-usage tools on a redacted host and on an
unfiltered one pointing at the same daemon, and `container_inspect` and `image_inspect` on everything
the daemon held.  A value counts as caught only if no raw, base64, hex or URL-encoded form of it
survives anywhere.  `uv run scripts/measure-redaction.py sweep` repeats it (`--help` for the options),
and the results below are as it last reported them, on Docker Desktop, on Docker 26.1 and 28 daemons with
Swarm, and on a Docker 29 daemon reached over SSH; they can move with the code, so run it again before
relying on them.

**Caught:** a credential-named environment value (raw, base64 and hex in logs), an AWS key pair, a
GitHub token, a JWT under an innocuous name, a password in a database URL, a word-like password under a
credential name, a Compose `.env` value, an image-level environment value, a build argument in image
history, a PEM private key and a credential-named random secret in files read through `container_exec`,
a value after `--password` on a command line (in `container_list`, `container_inspect`, `container_top`
and `system_events`), and a Swarm service's environment.

**Missed:**

- a word-like secret in a file, with no credential name next to it, read through `container_exec`;
- sometimes, a random value with no credential name or known format beside it: a credential under an
  innocuous name, a value generated at container start and printed to the logs, or a Swarm secret a
  service prints (a secret's content cannot be read through the API).  Only the shape rules can catch
  these, and they take a run as random only above a fixed threshold, which a random value falls under
  by chance: about one time in fifty at 40 characters, and one in three at 32.  Each was missed at
  least once across the runs;
- anything transformed before it is printed: split, reversed, encrypted, hashed, or encoded some way
  not listed above.  Hashes are not matched at all.

**Replaced by mistake:**

- a value under a name the patterns match that is not a credential, such as a sort key in `SORT_KEY`.
  `!SORT_KEY` in `DOCKER_MCP_SERVER_REDACT_NAMES` excludes a name like that;
- a long random-looking value that is not a credential, such as a subresource-integrity hash, by its
  shape.

Of the decoys planted, a build ID, a commit hash, a UUID, a cache buster and an SSH public key were left
alone, and nothing else on the daemons measured was replaced.

## Using a credential without seeing it

On a redacted host that accepts writes, a marker in the arguments of a tool that writes is swapped back,
before the call runs, for exactly the text it replaced, in the form it was replaced in.  So an agent can
recreate a container from `container_inspect` output, or pass a password to `container_exec`, without
ever seeing the value; the value is redacted again in any later output.  Tools that only read are never
touched: a marker passed to one reaches the daemon as literal text.  Swapped arguments are never logged.

The whole call is refused before anything runs, with an error naming the argument and the reason but
never a value, when a marker in it:

- was issued for another host.  That includes every marker in a write to a host that is not redacted,
  in a `system_login` to a read-only host, which never swaps one back, and in the `context_*` tools,
  which reach no daemon;
- was issued before the server last started, or was altered;
- stands for a value found by the rules that the store had no room to keep (see Limits).  The error says
  that reading it again will not help, and to ask the user what to do.

As a guardrail against a credential leaking beyond the host, a marker is refused rather than swapped back
where a credential is never needed for a container to run, and would spread from there.  Everywhere a
credential is needed it is swapped back: environments, commands, container and service labels, volume
driver options, `auth_config`, `password` and the rest.

- **names and identifiers:** container, service, network, volume, secret, config, builder and project
  names, image references and tags, and hostnames.  They are copied into every listing and event, and
  into registries when pushed;
- **image metadata:** the labels `image_build` and `buildx_build` give an image, annotations,
  `container_commit`'s `message` and `author`, and the labels in its `conf`, which are kept in the image
  and published by `image_push`.  A container's or service's own labels are swapped back, since some
  containers read a credential from them, such as a Traefik basic-auth label, and they stay on the host;
- **build arguments,** `image_build`'s `buildargs` and `buildx_build`'s `build_args`, which are recorded
  in the image's history.  Pass the value in `buildx_build`'s `vars` and use it as a build secret
  (`secret`: `id=NAME,env=VAR`) instead;
- **bake overrides,** `buildx_bake`'s `set_overrides`, which are passed on the command line.  Pass the
  value in `vars` and reference it as a bake variable instead;
- **the docker CLI's command line,** for the CLI-backed tools (Compose, `stack_deploy` and buildx): every
  argument except `vars`, and `env` on `compose_run` and `compose_exec`, which reach the CLI through its
  environment.  Other users of the machine, or of the remote host under the SSH fallback, can read a
  command line.  So a password goes in `env` and is referenced from the command
  (`sh -c 'psql -p "$PGPASS"'`), or in `vars` and referenced from the compose or bake file.  An `env`
  name the CLI's environment cannot carry goes on the command line, so a marker in its value is refused
  too: a reserved name, which the CLI would act on itself (such as `DOCKER_*`, `GIT_*` or `SSH_*`), or one
  that is not a plain shell name (such as `app.password`).  The refusal gives two routes that set it
  inside the container from a plain name, `VALUE` here:
  - `env` `{"VALUE": <marker>}` with `command` `["sh", "-c", "exec env \"GIT_TOKEN=$VALUE\" <command>"]`,
    where the image has a shell;
  - when starting the service with `compose_up` or `compose_run`, `vars` `{"VALUE": <marker>}`, with
    `GIT_TOKEN: ${VALUE}` under the service's `environment` in the compose file.

The same names inside `extra_kwargs`, `updates` and `container_commit`'s `conf` are refused the same way,
and so are the labels in `conf`, which are the committed image's own.

### Files and archives

A marker means something only to the server process that issued it, so it is never swapped back in a
file: the server will not rewrite a user's file, and replacing text in an archive would change the sizes
its headers record.  On a redacted host that accepts writes, a call is refused before it runs, naming the
file, when a file it reads holds anything shaped like a marker, whichever process issued it.  What is
searched, as a whole, since any file in it can end up in a container or an image:

- the Compose tools: the project directory, which is the first compose file's directory or, with none
  named, `project_dir`, by default the server's working directory; and `compose_cp`'s local `source`;
- `stack_deploy`: its first compose file's directory; `buildx_bake`: its `cwd`, by default the server's
  working directory;
- `image_build` and `buildx_build`: the build context, with its `.dockerignore` honoured, and the
  Dockerfile; and `buildx_build`'s named contexts and secret files;
- every file a call names outside those directories.

Each directory is the one the CLI resolves the call's relative paths against.  The search stops at 50,000
files or 200 MB read in a call, the same limits as the SSH fallback's copying to a remote host, and past
either the call is refused rather than run with files unsearched.

Not searched: a build context the server cannot read, such as a Git URL; anything a compose or bake file
refers to outside those directories, such as a build context at `../other`; and the tarballs
`image_load` and `image_import` read.

An archive passed to `container_archive_put`, in band or from a file, is searched too, and refused naming
the member that holds the marker.  A gzip, bzip2 or xz archive is refused, because a search cannot see
into it: pass an uncompressed tar.  A marker inside a compressed file within the tar, such as a `.gz`
member, is not found and reaches the container as literal text.

### Routes that stay open

- A value passed in `vars` lands wherever the compose or bake file uses it, labels included, which the
  server cannot see.
- `container_commit`'s `changes` and the environment in its `conf` are swapped back, so an `ENV` set
  there is kept in the image.
- Anything written into a container can reach a registry through `container_commit` and `image_push`.

## Fields the rules leave alone

These fields in Docker's own output look random but are public, so the shape rules never replace
their values; a value the server knows is still replaced in them:

`TrustRoot`, `CertIssuerPublicKey`, `CertIssuerSubject` and `SandboxKey`.

## What it costs

Redaction costs time on every call to a redacted host, and memory for the values it holds.  It is kept
as low as the design allows; if you do not need it, leave it off.  How the cost grows:

- **The first call to a host** reads every credential the daemon reports, so it takes longest, growing
  with the daemon's containers, images and services.
- **Every call** first checks that nothing has changed since, by listing the daemon's containers,
  images, services and configs, so it grows with them too.
- **Redacting a result** costs in proportion to its size, so a call returning a lot of data costs most.
- **A write to a redacted host that accepts writes** also searches the files it reads, in proportion to
  their size.
- **Memory** grows with the number of distinct credentials, not with the number of calls.

`uv run scripts/measure-redaction.py cost` measures it on a throwaway daemon of 2,000 containers, each
with a credential, and 500 images.  As it last reported, on a Mac, and with the same calls on Docker
Desktop for comparison; the figures move with the code and the machine, so run it again before relying
on them:

| | Docker Desktop | 2,000 containers, 501 images |
| --- | --- | --- |
| First call to the host | about 2 s | about 12.5 s |
| Added to every call | about 40 ms | about 140 ms |
| Added to `container_list` of every container (1.4 MB of output) | | about 1.7 s |
| Values held | 1 value, 245 bytes | 2,000 values, 586 KB |

## Limits

The server keeps the values it redacts in memory, and stops growing that store at fixed limits:

| Limit | Values read from the daemon | Values found by the rules |
| --- | --- | --- |
| Longest value matched | No limit | 16 KB; a longer run is left alone |
| Values stored | No limit of their own | 100,000 |
| Bytes stored, each value and its encoded forms | No limit of their own | 64 MB |
| Overall budget, both kinds together | 256 MB | |

Past either limit on found values, a new one is still replaced where a rule finds it, but not
remembered, so its other forms are not matched and its marker cannot be swapped back; this is logged
once at `WARNING`, naming the host.  At the overall budget, which only an abnormal harvest reaches,
every call to a redacted host is refused with an error suggesting a restart; it is logged once at
`ERROR`.  A restart empties the store and makes every earlier marker invalid.

## Checking what is in effect

- **`host_list`** and the `docker-mcp://hosts` resource also give each redacted host a `redaction`
  field: `notes` on how markers behave on that host, which differ on a read-only one, where nothing is
  swapped back, and the `store`
  state, values and bytes held for each kind and whether the overall budget is reached.  Never a value.
- **`tool_list`** and the `docker-mcp://tool-catalog` resource report, under `switches`:
  `DOCKER_MCP_SERVER_REDACT`, `DOCKER_MCP_SERVER_REDACT_NAMES` as the resolved name list (`builtin`,
  `builtin_excluded`, `added` and `excluded`), and `redacted_hosts`, the hosts whose output is redacted.
- **`host_list`** and the `docker-mcp://hosts` resource give each host a `redact` field: whether
  its output is redacted, by its own option or by the global switch.

## Configuration warnings

The server warns:

- for each redacted host that is neither marked `ro` nor covered by `DOCKER_MCP_SERVER_READONLY`,
  whether it is redacted by its own option or by `DOCKER_MCP_SERVER_REDACT`.  On such a host exec and
  the tools that write daemon data to files stay available, and either can show an agent a credential
  that redaction cannot catch;
- when `DOCKER_MCP_SERVER_REDACT_NAMES` is set but redaction is on for no host.

Because an MCP client rarely shows a server's stderr to anyone, each warning goes three places, as
`DOCKER_MCP_SERVER_DISABLE` mistakes do: a `WARNING` line on stderr at startup, an entry in
`tool_list`'s `configuration_warnings`, and a line at the top of the server instructions telling the
agent to check `tool_list` and tell the user.

## The boundary this protects

Redaction keeps credentials from an agent whose only route to Docker is this server.  An agent that can
also run commands as the same user on the same machine, as a coding agent can, could read the server
process's memory, though it could as easily use the Docker socket directly; for real separation run the
server as another user or in its own container, which the published image provides.  While redaction is
on the server switches core dumps off, so a crash cannot write the stored values to disk.  Two limits
are accepted: values cannot be reliably wiped from memory on shutdown, because Python strings cannot be
overwritten in place, and the store cannot be kept out of swap.
