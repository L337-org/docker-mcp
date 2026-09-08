"""Guards on the two docstring exemptions pydoclint cannot express in configuration.

pydoclint runs in CI over `docker_mcp` and reports nothing. Two things it reports are
suppressed per function with a `# noqa` marker, because there is no config option for either,
and a marker is exactly the kind of thing that quietly stops meaning anything: the code it
names gets fixed, or the function it sits on changes shape, and nothing fails.

So both directions are asserted here. A tool that needs the exemption and lacks it fails, and
a marker on a definition that no longer needs it fails too.

The dialect guards below run over every tracked `.py`, not just the package. pydoclint's CI job
is scoped to `docker_mcp`, so six docstrings under `tests/` kept the old lowercase `args:` form
with no gate ever seeing them.
"""

import ast
import io
import pathlib
import re
import shutil
import subprocess
import tokenize

from docker_mcp.server import _CLI_DOMAINS

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "docker_mcp"
NOQA = re.compile(r"#\s*noqa:\s*([\w,]+)")
ROOT = PACKAGE.parent


def _tracked_python_files():
    """Every tracked `.py` file, package and tests alike.

    Returns:
        list: paths, sorted
    """
    git = shutil.which("git")
    assert git, "git is needed to enumerate tracked files"
    listing = subprocess.run(  # noqa: S603 - fixed argv, resolved binary, no shell
        [git, "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
    )
    return sorted(ROOT / name for name in listing.stdout.split())


def _tracked_docstrings():
    """Every docstring in every tracked `.py`, package and tests alike.

    Returns:
        list: `(path, lineno, name, docstring)` tuples
    """
    out = []
    for path in _tracked_python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node)
            if doc:
                out.append((path, getattr(node, "lineno", 1), getattr(node, "name", "<module>"), doc))
    return out


def _definitions():
    """Every function and method under docker_mcp, with its source line and file.

    Returns:
        list: `(path, node, lines)` triples, lines being that file's source lines
    """
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((path, node, lines))
    return out


def _marker_codes(node, lines):
    """The DOC codes the marker on a definition's own line names.

    Args:
        node: the function or method
        lines: its file's source lines

    Returns:
        set: the codes named, empty when there is no marker
    """
    match = NOQA.search(lines[node.lineno - 1])
    return {c.strip() for c in match.group(1).split(",") if c.strip().startswith("DOC")} if match else set()


def _decorator_names(node):
    """The callee name of each decorator on a definition.

    Matched on the callee rather than on the decorator's unparsed text, because the text
    includes the arguments: `@resource("docker-mcp://tool-catalog", ...)` contains the word
    "tool" and was being read as a tool decorator, which misclassified that resource and made
    both guards below wrong about it.

    Args:
        node: the function or method

    Returns:
        set: the decorator callee names, `server.tool(...)` contributing "tool"
    """
    names = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _is_tool(node):
    """Whether a definition is a registered MCP tool.

    Args:
        node: the function or method

    Returns:
        bool: True when a decorator is `tool`
    """
    return "tool" in _decorator_names(node)


def _is_advertised(node):
    """Whether a definition's docstring is sent to clients.

    A tool's docstring is its description and a resource's reaches clients through
    `list_resources()` / `list_resource_templates()`, so in both cases every byte is paid for on
    every session and the docstring is written for that reader rather than for a maintainer.

    Args:
        node: the function or method

    Returns:
        bool: True when a decorator names `tool` or `resource`
    """
    return bool({"tool", "resource"} & _decorator_names(node))


def _has_undocumented_args(node):
    """Whether a definition leaves any of its parameters undocumented.

    Any, not all, and the difference is the usual case rather than the edge one: a tool's `host`
    parameter is added by the decorator's own schema surgery rather than written in the
    docstring, so a tool documenting every other parameter still trips DOC101 on that one. An
    earlier version was named `_documents_no_args` and its summary said "documents none of them",
    which contradicted both the body below it and the paragraph above.

    Args:
        node: the function or method

    Returns:
        bool: True when at least one parameter is undocumented
    """
    docstring = ast.get_docstring(node) or ""
    section = re.search(r"^Args:\n((?:    .*\n?)+)", docstring, re.MULTILINE)
    documented = set(re.findall(r"^ {4}(\*{0,2}\w+)", section.group(1), re.MULTILINE)) if section else set()
    args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
    return any(a.arg not in documented for a in args if a.arg not in ("self", "cls"))


def test_no_docstring_uses_a_lowercase_section_header():
    """Section headers are `Args:`, not `args:`.

    A lowercase header is invisible to every checker we run: ruff reads it as ordinary prose and
    pydoclint then reports the parameters as undocumented only if it is looking at the file at all.
    Note what this must *not* match - a parameter genuinely named `args` is documented as
    `args: the argv to scan` inside a real `Args:` block, and `docker_mcp/tools/_cli.py` is full
    of those. `ast.get_docstring` dedents, so a header sits at column 0 and an entry at column 4;
    that anchor is the whole distinction, and both guards here depend on it.
    """
    header = re.compile(r"^(args|returns|raises|yields|attributes|examples|note):[^\S\n]*$", re.MULTILINE)
    wrong = [
        f"{path.relative_to(ROOT)}:{lineno} {name}: {m.group(1)!r}"
        for path, lineno, name, doc in _tracked_docstrings()
        for m in header.finditer(doc)
    ]
    assert not wrong, "these section headers are not capitalised:\n  " + "\n  ".join(wrong)


def test_no_docstring_collapses_a_section_onto_one_line():
    """A section header carries no content on its own line.

    `returns: bool - True if that version satisfies` parses as prose, so the return goes
    undocumented while every gate stays green. Six docstrings under `tests/` were written this way.
    """
    collapsed = re.compile(
        r"^(args|returns|raises|yields|Args|Returns|Raises|Yields):[^\S\n]+\S.*$",
        re.MULTILINE,
    )
    wrong = [
        f"{path.relative_to(ROOT)}:{lineno} {name}: {m.group(0).strip()[:70]!r}"
        for path, lineno, name, doc in _tracked_docstrings()
        for m in collapsed.finditer(doc)
    ]
    assert not wrong, "these sections are collapsed onto the header line:\n  " + "\n  ".join(wrong)


def test_every_returns_entry_carries_a_type():
    """Every `Returns:` entry starts with a type.

    pydoclint has no check for this: it holds arguments to carrying a type through DOC109 and
    DOC110, but nothing equivalent for the return, so an untyped `Returns:` passes every gate.
    That is how `_select_platform_digest` kept `(digest, actual_platform) of the selected
    sub-manifest` - a shape, with no type - through the whole conversion.
    """
    typed = re.compile(r"^[\w\.\[\], |]+(\s+or\s+[\w\.]+)*:\s+\S")
    wrong = []
    for path, node, _ in _definitions():
        section = re.search(r"^Returns:\n((?:    .*\n?)+)", ast.get_docstring(node) or "", re.MULTILINE)
        if not section:
            continue
        first = section.group(1).splitlines()[0].strip()
        if not typed.match(first):
            wrong.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno} {node.name}: {first[:60]!r}")

    assert not wrong, "these Returns entries do not start with a type:\n  " + "\n  ".join(wrong)


def test_the_parameter_exemption_sits_only_on_tools_that_need_it():
    """`# noqa: DOC101,DOC103` appears exactly on the tools whose schema carries the parameter.

    CS.6.14: a tool's docstring is its advertised description, and the inputSchema sent beside it
    already carries every parameter's type, so an `Args:` block there duplicates the schema and
    is paid for on every session that loads the surface. That reasoning applies to a registered
    tool and to nothing else.
    """
    wrong = []
    for path, node, lines in _definitions():
        codes = _marker_codes(node, lines)
        marked = {"DOC101", "DOC103"} & codes
        needs = _is_tool(node) and _has_undocumented_args(node)
        where = f"{path.relative_to(PACKAGE.parent)}:{node.lineno} {node.name}"
        if needs and not marked:
            wrong.append(f"{where}: a tool with undocumented parameters and no DOC101/DOC103 marker")
        if marked and not _is_tool(node):
            wrong.append(f"{where}: carries a DOC101/DOC103 marker but is not a registered tool")
        if marked and not _has_undocumented_args(node):
            wrong.append(f"{where}: carries a DOC101/DOC103 marker but documents every parameter")

    assert not wrong, "the CS.6.14 parameter exemption is out of step:\n  " + "\n  ".join(wrong)


def test_the_raises_markers_sit_on_real_exemptions():
    """A `# noqa` naming DOC501, DOC502 or DOC503 appears only where one of two reasons holds.

    This code documents what a caller can catch, which includes what its callees raise;
    pydoclint only sees exceptions constructed literally in the body. That is a decision rather
    than a backlog, but it is only defensible per function - a marker on a docstring that simply
    names the wrong exception would hide a real defect, which is how five wrong exception names
    were found in this package rather than silenced.
    """
    wrong = []
    for path, node, lines in _definitions():
        codes = _marker_codes(node, lines)
        # DOC501 belongs here too: an advertised docstring carrying no `Raises:` section trips
        # it for the same reason it trips DOC502 and DOC503, and leaving it out left the marker
        # on get_docs_section unguarded - the exact rot these guards exist to catch.
        if not ({"DOC501", "DOC502", "DOC503"} & codes):
            continue
        docstring = ast.get_docstring(node) or ""
        section = re.search(r"^Raises:\n((?:    .*\n?)+)", docstring, re.MULTILINE)
        documented = set(re.findall(r"^    ([\w.]+):", section.group(1), re.MULTILINE)) if section else set()
        nested = {
            id(x)
            for f in ast.walk(node)
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f is not node
            for x in ast.walk(f)
        }
        raised = {
            ast.unparse(x.exc).split("(")[0]
            for x in ast.walk(node)
            if isinstance(x, ast.Raise) and x.exc and id(x) not in nested
        }
        # The second legitimate reason for the marker: a raise pydoclint cannot resolve to a
        # type at all - a bare `raise` re-raising after cleanup, or `raise some_variable`. It
        # reports those as a mismatch however the docstring is written.
        unresolvable = any(
            isinstance(x, ast.Raise) and id(x) not in nested and (x.exc is None or isinstance(x.exc, ast.Name))
            for x in ast.walk(node)
        )
        where = f"{path.relative_to(PACKAGE.parent)}:{node.lineno} {node.name}"
        if not documented:
            # An advertised docstring deliberately carries no Raises section: it is the client's
            # description, and a `Raises:` block there is wire cost on every session rather than
            # documentation for a maintainer. Marking the definition is the whole point.
            if not _is_advertised(node):
                wrong.append(f"{where}: marked DOC501/DOC502/DOC503, documents no exception, and is not advertised")
            continue
        elif documented <= raised and not unresolvable:
            wrong.append(
                f"{where}: marked DOC501/DOC502/DOC503 but every documented exception is raised here "
                f"and no raise is unresolvable, so there is nothing for the marker to suppress"
            )

    assert not wrong, "the propagated-exception markers are out of step:\n  " + "\n  ".join(wrong)


def test_no_advertised_docstring_carries_a_raises_section():
    """An advertised docstring documents no exceptions, because the client cannot use them.

    `pyproject.toml` records this as a decision rather than a backlog, and the DOC501/DOC502/DOC503
    marker on the definition is how it is expressed. What makes it more than a byte argument is that
    the class name is unobservable: `_translate_failures` re-raises as `error_cls(str(exc))`, so a
    client receives the message and never the type. A `Raises:` block advertises `ToolInputError` and
    `RemoteFailureError` to a reader who only ever sees a `ToolError`.

    `test_the_raises_markers_sit_on_real_exemptions` guards the other direction and only inspects
    definitions that carry a marker, so adding the section instead of the marker slipped past it -
    which is how 24 tools grew one, at 2,785 bytes of every session, in a pull request whose subject
    was clearing an unrelated lint code. Error behaviour a caller can act on belongs in the usage
    paragraph, in terms of what happens rather than which class was constructed.
    """
    wrong = [
        f"{path.relative_to(ROOT)}:{node.lineno} {node.name}"
        for path, node, _ in _definitions()
        if _is_advertised(node) and re.search(r"^Raises:", ast.get_docstring(node) or "", re.MULTILINE)
    ]

    assert not wrong, (
        "these advertised docstrings carry a Raises section, which is wire cost on every session "
        "for a type the client never sees - mark the definition instead:\n  " + "\n  ".join(wrong)
    )


def test_every_cli_backed_tool_states_its_error_convention():
    """A CLI-backed tool says which of the two error conventions it follows.

    `architecture/cli-shell-out.md` gives CLI-backed tools two behaviours and no third: an action
    tool hands back the raw `CliResult` and never raises on a non-zero exit, and a parsed-query tool
    raises through `raise_on_cli_failure`. Which one a tool is cannot be guessed from its name, and
    an agent that assumes the wrong one either treats a failed call as success or wraps a call that
    cannot fail - so the description has to say.

    Fourteen tools said neither, `compose_up` among them. They were invisible because a reviewer
    checks what a docstring claims, not what it omits, and an omission has nothing to catch the eye.

    The sentence is matched, not merely the word "raise": before this guard, `buildx_build` and
    `buildx_history_list` both mentioned raising for an unrelated special case while saying nothing
    about the convention, which is exactly the shape a looser check would pass.
    """
    domains = {name.split("_")[0] for name in _tool_names()} & set(_CLI_DOMAINS)
    conventions = (
        "Does not raise on a non-zero CLI exit",
        "Raises RemoteFailureError if the CLI call fails",
    )
    wrong = [
        f"{path.relative_to(ROOT)}:{node.lineno} {node.name}"
        for path, node, _ in _definitions()
        if _is_tool(node)
        and node.name.split("_")[0] in domains
        and not any(c in (ast.get_docstring(node) or "") for c in conventions)
    ]

    assert not wrong, (
        "these CLI-backed tools state neither error convention, so an agent cannot tell whether a "
        "non-zero exit raises or comes back in the result:\n  " + "\n  ".join(wrong)
    )


def test_no_tracked_line_exceeds_the_documented_limit():
    """No tracked `.py` line is over 120 characters.

    AGENTS.md states the limit and names ruff as what enforces it, but ruff's E501 did not report
    any of the six lines that were over it when this was written - four were long only because of
    a trailing `# noqa` marker, which ruff exempts by design. So the documented number had no gate
    behind it, and every violation reached a reviewer instead of failing here.
    """
    over = [
        f"{path.relative_to(ROOT)}:{n} ({len(line)})"
        for path in _tracked_python_files()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if len(line) > 120
    ]
    assert not over, "these lines are over the 120-character limit:\n  " + "\n  ".join(over)


def test_every_doc_marker_sits_on_the_line_pydoclint_reads():
    """A `# noqa: DOC...` marker sits on the `def` line, not on a closing paren.

    `native-mode-noqa-location = "definition"` means the opening line of the definition. Moving a
    marker to the closing paren of a split signature was measured to make pydoclint report the
    code again - and moving it the other way makes the marker silently do nothing while the run
    stays green, which is the direction that hurts.
    """
    marker = re.compile(r"#\s*noqa:[^\n]*\bDOC\d+")
    stray = []
    for path in _tracked_python_files():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        # Comment tokens only: the same text quoted inside a docstring is prose, not a marker.
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT or not marker.search(token.string):
                continue
            line = lines[token.start[0] - 1]
            if not re.match(r"\s*(async\s+)?def\s", line):
                stray.append(f"{path.relative_to(ROOT)}:{token.start[0]} {line.strip()[:70]}")

    assert not stray, "these DOC markers are not on a definition line:\n  " + "\n  ".join(stray)


def test_no_args_entry_carries_its_type_in_the_dash_form():
    """No `Args:` entry writes its type as `name: type - description`.

    This repository puts types in the signature, and DOC111 enforces that - but only for the
    `name: type` form it can parse. Written with a dash, `hostname: str - the target to resolve`
    is a description beginning "str", so seven of these sat in `_ssh_proxy.py` with every gate
    green and AGENTS.md describing a rule the code did not follow.
    """
    typed = re.compile(
        r"^    \*{0,2}\w+:\s*(?:str|int|bool|float|dict|list|set|tuple|bytes|Path|[A-Z]\w+)"
        r"(?:\s*\|\s*[\w.]+)*\s+-\s",
        re.MULTILINE,
    )
    wrong = []
    for path, lineno, name, doc in _tracked_docstrings():
        section = re.search(r"^Args:\n((?:    .*\n?)+)", doc, re.MULTILINE)
        if section:
            wrong += [
                f"{path.relative_to(ROOT)}:{lineno} {name}: {m.group(0).strip()[:60]!r}"
                for m in typed.finditer(section.group(1))
            ]

    assert not wrong, "these entries carry a type in the dash form:\n  " + "\n  ".join(wrong)


# ---------- references and return shapes in advertised tool docstrings ----------


def _tool_names():
    """Every registered tool's name.

    Returns:
        set: the tool names, taken from the definitions `_is_tool` accepts while walking the
            AST, rather than by importing the server
    """
    return {node.name for _, node, _ in _definitions() if _is_tool(node)}


def _returns_attrs(node):
    """Whether the tool hands back a docker-py model's `.attrs` verbatim.

    Only a `return` whose expression is recognisably `<model>.attrs` counts - directly, through a
    comprehension, or through a conditional. A tool that computes its own dict from `.attrs`
    (`container_wait`, `node_wait`, `swarm_update`) returns a shape of its own making and is not
    covered here, which is why this looks at the returned expression and not at the body.

    A return inside a nested definition is the tool's helper returning, not the tool, so those are
    excluded the same way `test_the_raises_markers_sit_on_real_exemptions` excludes nested raises.
    `ast.walk` descends into an inner `def`, so without this an inner helper handing back `.attrs`
    would put a documentation requirement on a tool that returns something else entirely.

    Args:
        node: the function definition to inspect

    Returns:
        bool: True when at least one return hands back `.attrs` unchanged
    """

    def is_attrs(expr):
        if isinstance(expr, ast.Attribute) and expr.attr == "attrs":
            return True
        if isinstance(expr, (ast.ListComp, ast.GeneratorExp)):
            return is_attrs(expr.elt)
        if isinstance(expr, ast.IfExp):
            return is_attrs(expr.body) or is_attrs(expr.orelse)
        return False

    nested = {
        id(stmt)
        for inner in ast.walk(node)
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner is not node
        for stmt in ast.walk(inner)
    }
    return any(
        isinstance(stmt, ast.Return) and id(stmt) not in nested and stmt.value is not None and is_attrs(stmt.value)
        for stmt in ast.walk(node)
    )


def test_every_sibling_reference_names_a_registered_tool():
    """A backticked tool-shaped token in a tool docstring resolves to a tool that exists.

    Sibling references are how a lazy-loading client picks between neighbours, and the naming
    convention makes them retrieval anchors too - so a reference to a tool that was renamed or
    never existed sends the agent after something uncallable. `compose_images` carried
    "`compose_up`/`compose_create` first" for exactly this reason: `compose_create` is not
    registered and that line was its only occurrence in the repo.

    A token matching one of the function's own parameters is skipped: `plugin_data_dir` and
    `compose_files` are parameters that happen to share the domain-prefixed shape.
    """
    names = _tool_names()
    prefixes = {name.split("_")[0] for name in names}
    token = re.compile(r"`([a-z][a-z0-9_]*)\s*(?:\([^`]*\))?`")
    wrong = []
    for path, node, _ in _definitions():
        if not _is_tool(node):
            continue
        spec = node.args
        params = {a.arg for a in spec.posonlyargs + spec.args + spec.kwonlyargs}
        params |= {a.arg for a in (spec.vararg, spec.kwarg) if a is not None}
        for found in token.findall(ast.get_docstring(node) or ""):
            if "_" not in found or found in params or found in names:
                continue
            if found.split("_")[0] in prefixes:
                wrong.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name} -> `{found}`")

    assert not wrong, "these docstrings reference a tool that is not registered:\n  " + "\n  ".join(wrong)


def test_every_verbatim_attrs_return_names_its_document():
    """A tool returning `.attrs` unchanged says which document that is.

    There is no output schema, so the `Returns:` line is all an agent gets. Naming the document
    ("full inspect payload", "full document") tells it what it is holding; "the X's attrs" names
    neither the form nor the contents, which is the shapeless form tool-descriptions.md bans. The
    surface had both vocabularies for one payload - seven `container_*` tools said "full inspect
    payload" while four beside them said "attrs" for the identical document.
    """
    wrong = []
    for path, node, _ in _definitions():
        if not (_is_tool(node) and _returns_attrs(node)):
            continue
        section = re.search(r"^Returns:\n((?:    .*\n?)+)", ast.get_docstring(node) or "", re.MULTILINE)
        entry = " ".join(section.group(1).split()) if section else ""
        if "inspect" not in entry.lower() and "document" not in entry.lower():
            wrong.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}: {entry[:70]!r}")

    assert not wrong, "these tools return `.attrs` but their Returns entry names no document:\n  " + "\n  ".join(wrong)
