"""Static check on generated adapter code, before anything runs it.

The sandbox is the real containment. This is the layer in front of it, and it
earns its place three ways: it catches a bad generation in milliseconds instead
of a container start, it still applies on the fallback runner where containment
is weaker, and it applies again at *load* time -- when a cached adapter is read
back off disk weeks later and executed over a whole tenant, nothing about the
original sandbox run protects that.

The rule is an allowlist, not a blocklist. An adapter is a pure dict-to-dict
function: it has no business opening files, resolving hostnames, spawning
processes, or reaching through `__class__` into anything. So the check is not
"does this look dangerous" but "is this within the small vocabulary an adapter
needs", and everything outside it is rejected with the line it was on.

Deliberately not claimed: this is not a Python sandbox. Determined escape from
an AST allowlist is possible and the literature is long. It is a correctness
and defence-in-depth check that runs alongside a container with no network, not
a substitute for one.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Modules an adapter may import. Pure computation only: no I/O, no network, no
# process control, no dynamic import. `emit.py` generates code that imports
# nothing at all; this list exists for the derived-field logic a model writes,
# where parsing a date or a string genuinely needs help.
ALLOWED_IMPORTS = frozenset(
    {
        "base64",
        "binascii",
        "calendar",
        "collections",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "html",
        "itertools",
        "json",
        "math",
        "numbers",
        "operator",
        "re",
        "statistics",
        "string",
        "textwrap",
        "types",
        "typing",
        "unicodedata",
        "urllib.parse",  # parsing only; `urllib.request` is not on this list
        "uuid",
    }
)

# Builtins that turn data into code, or code into I/O. `getattr`/`setattr` are
# absent from this list on purpose: they are ordinary in mapping code, and the
# dangerous thing they reach -- dunder attributes -- is blocked directly below.
FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "exit",
        "globals",
        "input",
        "locals",
        "memoryview",
        "open",
        "quit",
        "vars",
    }
)

# Dunder attributes are the standard route from an allowed object to a
# forbidden one (`().__class__.__base__.__subclasses__()`). No field mapping
# needs one, so all of them are refused rather than a blocklist of the known
# escape chains, which would only ever be as current as its last update.
ALLOWED_DUNDER_ATTRS = frozenset({"__name__", "__doc__"})

# An adapter that doesn't answer to the contract isn't an adapter, whatever
# else is true of it.
REQUIRED_FUNCTIONS = ("transform", "flags")

# Generated adapters are hundreds of lines. A megabyte of them is a runaway
# generation, and parsing it is the expensive way to find that out.
MAX_SOURCE_BYTES = 512 * 1024


@dataclass
class GuardReport:
    ok: bool = True
    violations: list[str] = field(default_factory=list)

    def add(self, message: str, node: ast.AST | None = None) -> None:
        line = getattr(node, "lineno", None)
        self.violations.append(f"line {line}: {message}" if line else message)
        self.ok = False

    def summary(self) -> str:
        if self.ok:
            return "clean"
        return "; ".join(self.violations[:8]) + (
            f" (+{len(self.violations) - 8} more)" if len(self.violations) > 8 else ""
        )


def _module_allowed(name: str) -> bool:
    """Whether a dotted module name is covered by the allowlist.

    `urllib.parse` being allowed must not imply `urllib`, and `urllib` being
    absent must not accidentally allow `urllib.request`. So a name matches only
    if it is listed, or if a listed name is a proper prefix of it -- which for
    `urllib.parse` permits `urllib.parse.quote` and nothing else.
    """
    if name in ALLOWED_IMPORTS:
        return True
    return any(name.startswith(f"{allowed}.") for allowed in ALLOWED_IMPORTS)


class _Visitor(ast.NodeVisitor):
    def __init__(self, report: GuardReport) -> None:
        self.report = report

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _module_allowed(alias.name):
                self.report.add(f"import of '{alias.name}' is not allowed in an adapter", node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.report.add("relative imports are not allowed in an adapter", node)
        elif not node.module or not _module_allowed(node.module):
            self.report.add(
                f"import from '{node.module or '.'}' is not allowed in an adapter", node
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self.report.add(f"'{node.id}' is not allowed in an adapter", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = node.attr
        if name.startswith("__") and name.endswith("__") and name not in ALLOWED_DUNDER_ATTRS:
            self.report.add(f"access to '{name}' is not allowed in an adapter", node)
        self.generic_visit(node)

    def _reject(self, what: str, node: ast.AST) -> None:
        self.report.add(f"{what} is not allowed in an adapter", node)

    def visit_AsyncFunctionDef(self, node) -> None:
        self._reject("async def", node)

    def visit_Await(self, node) -> None:
        self._reject("await", node)

    def visit_AsyncFor(self, node) -> None:
        self._reject("async for", node)

    def visit_AsyncWith(self, node) -> None:
        self._reject("async with", node)

    def visit_With(self, node) -> None:
        # `with` is how a file or a socket is held open. A pure mapping has no
        # resource to manage, so its presence means something else is going on.
        self._reject("`with` blocks", node)


def check_tree(tree: ast.AST) -> GuardReport:
    """Apply the allowlist to an already-parsed tree.

    Separate from `check_source` so a single function the Engineer wrote can be
    checked in isolation, before it is spliced into a module -- one set of
    rules, applied at both granularities.
    """
    report = GuardReport()
    _Visitor(report).visit(tree)
    return report


def check_source(source: str) -> GuardReport:
    """Check adapter source against the allowlist.

    Returns a report rather than raising: a violation is feedback for the
    Engineer's repair loop, and a loop that has to catch exceptions to read its
    own results is harder to follow than one that reads a value.
    """
    report = GuardReport()

    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        report.add(f"source exceeds {MAX_SOURCE_BYTES} bytes")
        return report

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.add(f"line {exc.lineno}: the adapter does not parse: {exc.msg}")
        return report

    _Visitor(report).visit(tree)

    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for required in REQUIRED_FUNCTIONS:
        if required not in defined:
            report.add(f"the adapter defines no module-level `{required}()`")

    return report


def check_and_raise(source: str) -> None:
    """Guard a module that is about to be executed.

    Used on the load path, where there is no repair loop to hand a report to
    and no sandbox behind it -- if a cached adapter has been tampered with, the
    only safe action is to refuse.
    """
    report = check_source(source)
    if not report.ok:
        raise ValueError(f"This adapter failed the safety check: {report.summary()}")
