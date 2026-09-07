"""Guards on the two docstring exemptions pydoclint cannot express in configuration.

pydoclint runs in CI over `docker_mcp` and reports nothing. Two things it reports are
suppressed per function with a `# noqa` marker, because there is no config option for either,
and a marker is exactly the kind of thing that quietly stops meaning anything: the code it
names gets fixed, or the function it sits on changes shape, and nothing fails.

So both directions are asserted here. A tool that needs the exemption and lacks it fails, and
a marker on a definition that no longer needs it fails too.
"""

import ast
import pathlib
import re

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "docker_mcp"
NOQA = re.compile(r"#\s*noqa:\s*([\w,]+)")


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


def _is_tool(node):
    """Whether a definition is a registered MCP tool.

    Args:
        node: the function or method

    Returns:
        bool: True when a decorator names `tool`
    """
    return any(re.search(r"\btool\b", ast.unparse(d)) for d in node.decorator_list)


def _documents_no_args(node):
    """Whether a definition takes parameters but documents none of them.

    A tool's `host` parameter is added by the decorator's own schema surgery rather than written
    in the docstring, so a tool documenting every other parameter still trips DOC101 on it.

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
        needs = _is_tool(node) and _documents_no_args(node)
        where = f"{path.relative_to(PACKAGE.parent)}:{node.lineno} {node.name}"
        if needs and not marked:
            wrong.append(f"{where}: a tool with undocumented parameters and no DOC101/DOC103 marker")
        if marked and not _is_tool(node):
            wrong.append(f"{where}: carries a DOC101/DOC103 marker but is not a registered tool")
        if marked and not _documents_no_args(node):
            wrong.append(f"{where}: carries a DOC101/DOC103 marker but documents every parameter")

    assert not wrong, "the CS.6.14 parameter exemption is out of step:\n  " + "\n  ".join(wrong)


def test_the_propagated_exception_markers_sit_on_real_propagation():
    """`# noqa: DOC502` / `DOC503` appears only where a documented exception is not raised here.

    This code documents what a caller can catch, which includes what its callees raise;
    pydoclint only sees exceptions constructed literally in the body. That is a decision rather
    than a backlog, but it is only defensible per function - a marker on a docstring that simply
    names the wrong exception would hide a real defect, which is how five wrong exception names
    were found in this package rather than silenced.
    """
    wrong = []
    for path, node, lines in _definitions():
        codes = _marker_codes(node, lines)
        if not ({"DOC502", "DOC503"} & codes):
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
            wrong.append(f"{where}: marked DOC502/DOC503 but documents no exception at all")
        elif documented <= raised and not unresolvable:
            wrong.append(
                f"{where}: marked DOC502/DOC503 but every documented exception is raised here "
                f"and no raise is unresolvable, so there is nothing for the marker to suppress"
            )

    assert not wrong, "the propagated-exception markers are out of step:\n  " + "\n  ".join(wrong)
