<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see REDACTION.md for that. -->

# Credential redaction

Deep detail behind the summaries in [../AGENTS.md](../AGENTS.md).
Read this before changing anything under `docker_mcp/redaction/`, or the `redact` host option in
`docker_mcp/_hosts.py`.  User documentation, including what the mode cannot catch, is
[../REDACTION.md](../REDACTION.md).

## Package layout

`docker_mcp/redaction/` lives at the package root, not under `tools/`, so `server.py` can import it
without importing `docker_mcp.tools` (the same reason `_hosts.py` and `_env.py` live there).  One
module per concern:

- **`settings.py`** - the global switch (`DOCKER_MCP_SERVER_REDACT`), the names setting
  (`DOCKER_MCP_SERVER_REDACT_NAMES`), and which hosts are redacted.  `load()` runs in
  `docker_mcp/__init__.py` after `_hosts.load()`, because `host_redacted` reads each host's `redact`
  option, and before the tools import.  `redacts(host)` is the one place the global switch and a
  host's own option are combined; `host_redacted(label)` resolves a label and calls it.  A malformed
  names value fails fast with `SystemExit(1)`, like a malformed `DOCKER_MCP_SERVER_HOSTS`.

- **`store.py`** - `ValueStore`, every value redacted, once per host, with its tier (harvested or
  discovered), and **every limit as a named constant at the top**, each with its reason.  Values are held
  in `Secret`, whose text form is a placeholder and which refuses to be pickled.  Nothing is evicted.
- **`forms.py`** - the encoded forms of a value the matcher looks for, each with a one-byte identifier
  a marker records, and `render(value, form_id)` to restore exactly the replaced text.
- **`markers.py`** - `MarkerCodec`: one AES block per marker under a per-process key, holding value
  identifier, form, host, a 5-byte counter and a check field.  `read` cannot tell an altered marker from
  one issued by an earlier process; both fail the check field.
- **`rules.py`** - the shape rules as a table, `PUBLIC_FIELDS`, and the credential-named flag rule.
- **`matcher.py`** - the Aho-Corasick index (`pyahocorasick`) over every stored value's forms.
- **`harvest.py`** - `HostHarvest`, one per redacted host: reads credential-named values from container,
  image and service environments, image history build arguments and credential-named Swarm configs.  It
  keeps itself fresh by comparing a snapshot of container, image, service-version and config IDs on every
  call, rather than a background thread, and when the snapshot changes reads only the objects new to it:
  an environment cannot change under the same ID, and a service's version is part of its entry.  Do not use the
  daemon's event history for this: the daemon keeps only a fixed number of recent events, so on a busy
  host a `create` can fall out between two calls, and `since`/`until` are judged by the daemon's clock,
  not the server's.  A result naming an ID
  the snapshot has not seen prompts one more comparison, once per ID, since network and plugin IDs look
  the same.  Each image's history is read once.  A container or image removed mid-harvest is skipped.
- **`wire.py`** - `wrap`, applied by `server.tool()` and `server.resource()` outermost, outside the
  failure translation, and `REDACTOR`, the process's one `Redactor`.  It also holds `NO_DAEMON_TOOLS`,
  `BYTE_TOOLS`, `FILE_TOOLS`, the Compose `.env` harvest and `disable_core_dumps` (called from `main()`).
  A harvest that fails with a library failure refuses the call, with the error translated as
  `_LIBRARY_FAILURES` does it: output that might hold a value the harvest could not read is not
  returned.  The connection-control tools are not held up by a harvest, so `system_reconnect` still
  recovers a host whose client is wedged.
- **`redactor.py`** - `Redactor`: `remember` (store a harvested value), `redact` (walk a result and
  replace), `resolve` (a marker back to its text, or a `Refusal`).
- **`swap.py`** - `swap_markers`: a write's arguments with every marker swapped back through `resolve`,
  or `MarkerRefusedError` naming the first that cannot be.  `ARGUMENTS` classifies every argument of
  every write tool that can hold text, by name, as swapped back or refused by the guardrail (names,
  image metadata, build arguments, bake overrides); `_TOOL_ARGUMENTS` overrides a name for one tool
  (`labels` is image metadata only in the build tools), and `_NESTED_KEYS_BY_ARGUMENT` applies the
  guardrail inside `extra_kwargs`, `updates` and `conf`, where `conf`'s labels are the image's own.
  For a tool in a CLI-backed domain (`server.tool()` passes `command_line=domain in _CLI_DOMAINS`),
  every argument the table swaps back is refused instead (`COMMAND_LINE`), except those in
  `ENVIRONMENT_ARGUMENTS`, which reach the CLI through its environment; within `env`, a name
  `_cli.values_by_name` would put on the command line is refused (`ENV_NAME`), by the same test, with
  the two routes that set it inside the container.
- **`disk.py`** - the search of what a write reads from disk (`FILE_INPUTS`, with Compose tools matched
  by prefix) and of `container_archive_put`'s archive, refusing on any marker-shaped text.  The
  directories searched are the ones the CLI resolves the call's relative paths against, which for Compose
  and a stack is the first compose file's directory, where the fallback stages `project_dir` or `cwd`.  A
  build context is walked with docker-py's `exclude_paths` and the fallback's `_read_dockerignore`, so what
  is searched is what is sent.  The limits are the fallback's staging limits, held equal by a test, and
  are charged on bytes actually read, not reported sizes; past them the call is refused.

### Concurrency

The MCP SDK runs each request as its own task and sync tools on worker threads, so calls genuinely
overlap.  The store and the marker counter each take a lock, and the matcher builds a new index and
swaps it in whole, so a reader never sees a half-built one.  A value is indexed outside the store's lock,
since indexing can take a while; a concurrent `add` of the same value waits until it is indexed, so no
call redacts against an index that does not hold a value it has just stored, and a value that fails to
index is removed again.

### Matching

Python's `re` cannot do this job: with thousands of literal alternatives it takes seconds per 100 KB,
where an Aho-Corasick automaton takes milliseconds.  Building an automaton costs time in proportion to
its forms, so the index is a few automata of geometrically falling size: a new value's forms become an
automaton of their own, and one is merged into the one before it whenever it reaches a quarter of that
one's size.  Each form is rebuilt about log4(n) times over the store's life, a search scans at most about
ten automata, and adding forms already indexed rebuilds nothing.  The occasional merge of the largest
levels stalls the call that triggers it, for seconds near the limits; accepted, since only an abnormal
store reaches that size.

The matcher reports every match, overlapping ones included, and the redactor resolves them: candidates
that overlap form one cluster, kept as one candidate where one covers it all and otherwise replaced
whole as one discovered value, so no part of a credential is left in the text.  A known value found in
another host's output is stored for that host too, so its marker can be swapped back there.

Every rule pattern that opens with a repeated class starts only where a run of that class starts and
repeats possessively, so a long run of base64 or hex is scanned once rather than from every position.
`test_the_rules_take_linear_time_on_long_runs` holds that.

### Running out of identifiers

Value identifiers (4 bytes) and the marker counter (5 bytes) are never reused.  If either runs out, the
redactor starts a new generation - new key, empty store - so every outstanding marker is refused.  The
call in which that happens, whether storing or redacting, is refused with `RedactionResetError`, which
the caller may retry as it is, rather than finished against the empty store, where a value the rules
cannot see would come back unredacted.  Neither is reachable before the overall budget in practice.

### The wire

`wrap` judges each call by its `Scope`: `HOST_ARGUMENT` (the `host` argument, or the default host),
`DEFAULT_HOST` (a resource whose URI names no host) or `NO_DAEMON`.  A tool's scope is derived: it has a
`host` argument, or it is in `NO_DAEMON_TOOLS`.  A resource's is required by `server.resource(scope=...)`.
`test_every_registered_tool_and_resource_is_classified_for_redaction` holds both, so a new one cannot
slip through.

`server.tool()` passes `writes=True` for every tool that is not `READ_ONLY`, connection control included,
since logging in with a credential read from the daemon is legitimate.  While redaction is on anywhere,
a write's markers go through `swap.swap_markers` first, on every host: swapped back on a redacted host
that accepts writes, refused everywhere else, since no marker was issued for an unfiltered host.  A call
to an unknown host, and a write the guard refuses on a read-only host (`guarded`, from `_is_host_write`),
are left alone, for the host guard to refuse with its own message.  Connection control is not guarded, so
a `system_login` to a read-only host has its markers refused here.  The
function is then called by name with the swapped arguments, and exactly as it arrived when nothing was
swapped.

For a call to a redacted host the wrapper refuses first (the budget; bytes in band), then, for a write to
a host that accepts writes, searches the files or archive it reads (`disk.py`), refreshes the harvest,
runs the call, redacts its result or the text of its translated failure (raised without the cause, which
holds the same text unredacted), and asks the harvest about IDs in the result.
A JSON resource body is parsed, redacted as data and dumped again, so an escaped value is still found.
Anything else that fails inside redaction propagates: the call fails rather than returning output.

## Measuring it

`scripts/measure-redaction.py` regenerates the figures `REDACTION.md` quotes and checks the mode end to
end:

- `sweep` plants credentials and decoys and compares a redacted server's output with an unfiltered
  server's on the same daemon.  `--configuration` picks how redaction is switched on (see `--help`),
  and each run also checks what the agent is told and offered there: the instructions pointer, the
  `host_list` notes, and the tools withdrawn.  A value only a refused call could show is reported as
  not reached, not caught.
- `cost` times calls on a daemon filled with containers and images.
- `agents` gives a headless Claude Code, with only this server's tools, the everyday requests in
  `_scenarios` on a small planted app, once read-only and once writable, and scores each: whether the
  task was done (or declined, for a write under READONLY), any planted value anywhere in the
  transcript, the tool errors it met, and whether it blamed a correct password in triage.  It costs
  model usage (`--model`, `--budget` per request), so run it after a change to what the agent is told
  or refused, not routinely.

All three run against a throwaway dind daemon; `sweep` alone can take `--docker-host`.  Run `sweep` and
`cost` after a change to the rules, the harvest or the wire, and correct `REDACTION.md` from their
output; the script reports only and is not a CI gate.  It never prints a replaced value, since on a
real daemon one may be a real credential.

## Where the settings surface

- `server._switch_report()` merges `settings.report()` into the `switches` that `tool_list` and the
  tool catalog return, so both show the same thing.
- `host_list` and the startup roster of other hosts report `redact` per host from `settings.redacts`,
  the global switch or the host's own option.  `Host.redact` is the per-host option only; never read
  it directly to decide whether a call is redacted.
- `server.configuration_warnings()` joins `settings.startup_warnings(readonly=READONLY)` to the
  `DOCKER_MCP_SERVER_DISABLE` warnings, so they reach stderr at startup, `tool_list` and the
  instructions pointer together.  READONLY is passed in rather than read, so the settings module never
  imports `server.py`.  The warnings are judged per redacted host, because per-host `ro` removes exec
  and the file-writing tools just as READONLY does.

## The host option grammar

`redact` is one of the options in a host's trailing brackets, parsed by `_hosts._parse_markers`.
Brackets may hold one option or a comma-separated list, so splitting the host list on commas has to
skip commas inside brackets (`_split_top_level`), and the bare single-host form is detected by an `=`
outside brackets (`_has_top_level_equals`), so that `(tls=<dir>)` does not make a bare endpoint read
as a `label=endpoint` list.  The grammar itself is described in [hosts.md](hosts.md).

## Extension points

Each is a single table, so adding a case is a one-line change plus its test.

### Adding an encoded form

1. Add a renderer to `_FORMS` in `docker_mcp/redaction/forms.py` under the next unused identifier.
   Identifiers are recorded in markers, so never reuse or renumber one.
2. Extend `test_form_identifiers_are_fixed` and add the encoding to
   `test_the_common_encodings_are_all_matched` in `tests/test_redaction_forms.py`.
3. Add it to the list of forms in `REDACTION.md`.

### Adding a shape rule

1. Add a finder to `_RULES` in `docker_mcp/redaction/rules.py`.
2. Add a case it must find to `test_each_rule_finds_its_credential`, and anything it must not to
   `test_what_is_not_a_credential_is_left_alone`, in `tests/test_redaction_rules.py`.
3. Add it to the list of rules in `REDACTION.md`.

### Adding a public field

1. Add it to `PUBLIC_FIELDS` in `docker_mcp/redaction/rules.py`, and to the list in `REDACTION.md`;
   `test_redaction_md_lists_exactly_the_public_fields` fails until the two agree.  Keep the list short: a
   field on it is never redacted by a rule, even if a credential ends up in it.

### Changing a limit

1. Change the constant at the top of `docker_mcp/redaction/store.py`, and its reason if that changed.
2. Update the table in `REDACTION.md`.

### Adding a built-in credential name pattern

1. Add the glob to `BUILTIN_NAME_PATTERNS` in `docker_mcp/redaction/settings.py`, with a comment if
   it is not obviously a credential word.
2. Add a name it catches to `test_the_built_in_patterns_harvest_credential_names` in
   `tests/test_redaction_settings.py`, and any ordinary name it must not catch to
   `test_the_built_in_patterns_leave_ordinary_names_alone`.
3. Add it to the pattern list in `REDACTION.md`, in the same order;
   `test_redaction_md_lists_exactly_the_built_in_patterns` fails until the two agree.

### Leaving a name alone by default

1. A name a built-in pattern matches but whose value is public goes in `BUILTIN_EXCLUDED_PATTERNS` in
   `docker_mcp/redaction/settings.py`, as narrowly as possible: an exact name where a glob would also
   catch private material (`GPG_KEY`, not `GPG_KEY*`, which would catch `GPG_KEY_PASSPHRASE`).
2. Add it to `test_public_keys_are_left_alone_by_default`, and a private name it must not catch to
   `test_the_public_key_exclusions_leave_private_material_harvested`, in
   `tests/test_redaction_settings.py`.
3. Add it to the exclusion list in `REDACTION.md`, in the same order;
   `test_redaction_md_lists_exactly_the_built_in_exclusions` fails until the two agree.

### Adding a tool or resource

1. A tool that takes `host` needs nothing more.  A tool that reaches no daemon and takes no `host` goes in
   `NO_DAEMON_TOOLS` in `docker_mcp/redaction/wire.py`; the classification test fails until it is there.
2. A tool returning raw bytes in band goes in `BYTE_TOOLS`, with the argument that sends them to a file.
   A tool writing daemon output to a file on the server host goes in `FILE_TOOLS`.  Both feed the
   `host_list` notes, and the lists in `REDACTION.md` are held to them by tests in
   `tests/test_redaction_wire.py`.
3. A resource passes `scope=` to `@resource()`: `HOST_ARGUMENT` for `{host}` in the URI, `DEFAULT_HOST`
   for daemon data with no host in the URI, `NO_DAEMON` otherwise.

### Adding an argument to a write tool

1. An argument that can hold text goes in `ARGUMENTS` in `docker_mcp/redaction/swap.py`, unless its name
   is already there with the right meaning.  Refuse it (`NAME`, `IMAGE_LABEL`, `BUILD_ARG`,
   `BAKE_OVERRIDE`) only where a credential is never needed for a container to run and would leak beyond
   the host from there; anywhere a container may need one, `SWAP`.  The guardrail stops leakage, never a
   credential reaching where it has to go.  Where the name means something else in one tool, add the
   pair to `_TOOL_ARGUMENTS`.
   `test_every_argument_of_every_write_tool_that_can_hold_text_is_classified` in
   `tests/test_redaction_swap.py` fails until it is there.
2. An argument holding docker-py keyword arguments or an Engine spec goes in `_NESTED_KEYS_BY_ARGUMENT`,
   so the guardrail applies to the keys inside it.
3. Add a refused kind to the guardrail list in `REDACTION.md`.
4. In a CLI-backed tool, an argument whose value is delivered through the CLI's environment rather than
   its command line goes in `ENVIRONMENT_ARGUMENTS`; anything else is refused there, whatever the table
   says.

### Adding a file a write tool reads

1. A tool taking a path argument goes in `FILE_INPUTS` in `docker_mcp/redaction/disk.py`, returning what
   the call reads, or in `NOT_SEARCHED` with the reason.  Compose tools are matched by prefix.
   `test_every_write_tool_that_takes_a_path_is_searched_or_says_why_not` in
   `tests/test_redaction_disk.py` fails until it is one or the other; a new path argument name goes in
   its `_PATH_ARGUMENTS`.
2. Search what the SSH fallback stages for the same call, and update the list in `REDACTION.md`.

### Searching inside a compressed archive

1. Give the format's step in `_ARCHIVE_FORMATS` in `docker_mcp/redaction/disk.py` a function that
   streams the decompressed bytes through the same search, capped on decompressed size.
2. Replace `test_a_compressed_archive_is_refused` for that format with one that finds a marker inside it,
   and update the archive paragraph in `REDACTION.md`.

### Adding a harvest source

1. Read it in `HostHarvest._harvest` in `docker_mcp/redaction/harvest.py`, through `credential_values` for
   `NAME=value` data, and add whatever identifies a change to it to `HostHarvest._take_snapshot`: an ID,
   plus a version where the object can change under the same ID.
2. Extend `test_the_first_call_harvests_every_source` in `tests/test_redaction_harvest.py`, and the
   harvest paragraph in `REDACTION.md`.

### Adding a host option

1. Add the option to `_apply_bracket` in `docker_mcp/_hosts.py`, and a field to `_Options` and `Host`.
2. Pass it through in `_make_host`, and add it to the unknown-option message.
3. Add parsing cases to `tests/test_hosts.py`, in both the one-bracket and one-per-bracket forms.
4. Report it in `host_list`, and add it to the flag list in the `docker-mcp://hosts` resource's
   description (`get_hosts_resource` in `docker_mcp/tools/resources.py`), which repeats the field set.
5. Update the grammar in [hosts.md](hosts.md) and the README.
