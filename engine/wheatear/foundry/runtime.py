"""Load a compiled adapter and run it over records.

This is the fast path, and it is where the whole design pays off: no model, no
container, no probing -- just a function called ten thousand times. It is also
the one place a stored adapter is *executed*, so the checks that matter happen
here rather than being assumed from the build.

Two of them, and both are about the gap between build time and run time:

  **The guard runs again.** The sandbox verified this code weeks ago in a
  different process. Nothing about that run protects this one -- the file has
  been on disk in between -- so the allowlist is re-applied before the module
  is compiled.

  **Builtins are restricted.** The module executes in a namespace holding the
  builtins an adapter needs and no others, with an `__import__` that answers
  only for the guard's allowlist. Between the two, a stored adapter that has
  been edited to reach the filesystem fails at load rather than at scale.

Failure is per record, never per batch. One malformed record among ten
thousand produces one `RecordOutcome` marked failed and the run continues,
because the alternative -- a traceback four hours into a migration -- is the
thing this whole component exists to avoid.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from wheatear.foundry import guard
from wheatear.foundry.types import AdapterArtifact, EntityKind

# Builtins a pure mapping function can legitimately need. Everything that
# touches the outside world is absent, including `open`, `input` and `print` --
# an adapter has no business writing to a terminal in the middle of a batch.
SAFE_BUILTINS = (
    "abs all any bool bytes callable chr dict divmod enumerate filter float format "
    "frozenset getattr hasattr hash hex id int isinstance issubclass iter len list map "
    "max min next object oct ord pow range repr reversed round set setattr slice sorted "
    "str sum tuple type zip "
    "ArithmeticError AttributeError BaseException Exception IndexError KeyError "
    "LookupError NotImplementedError OverflowError RuntimeError StopIteration TypeError "
    "UnicodeDecodeError UnicodeEncodeError ValueError ZeroDivisionError "
    "True False None"
).split()


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    """`__import__` that answers only for the guard's allowlist.

    Needed at all because the guard permits a handful of pure modules for
    derived-field logic, and an import statement cannot execute without it.
    Re-checking here rather than trusting the guard means a module that reached
    disk by some other route still cannot import `socket`.
    """
    if level != 0 or not guard._module_allowed(name):  # noqa: SLF001 - one allowlist, two users
        raise ImportError(f"An adapter may not import '{name}'.")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _namespace(module_name: str) -> dict[str, Any]:
    safe = {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)}
    safe["__import__"] = _restricted_import
    return {"__name__": module_name, "__builtins__": safe}


@dataclass
class LoadedAdapter:
    """A compiled adapter, ready to call."""

    key_slug: str
    meta: dict
    review_flags: list[dict]
    _transform: Any
    _flags: Any

    def transform(self, record: Any) -> dict:
        return self._transform(record)

    def flags(self, record: Any) -> list[dict]:
        return self._flags(record)


def load(artifact: AdapterArtifact, verified_only: bool = True) -> LoadedAdapter:
    """Compile a stored adapter into callables.

    `verified_only` refuses an adapter whose tests never passed. That is the
    right default by a distance: an unverified adapter is worth reading and
    worth finishing by hand, and is not worth running unattended over a
    customer's tenant. Callers who want it anyway have to say so.
    """
    if verified_only and not artifact.verified:
        raise ValueError(
            f"The adapter for {artifact.key.family()} has not passed its tests "
            f"({artifact.report.summary()}); refusing to run it. Rebuild it, or pass "
            "verified_only=False to run it deliberately."
        )

    guard.check_and_raise(artifact.code)
    module_name = f"wheatear_adapter_{artifact.key.schema_fingerprint[:12]}"
    namespace = _namespace(module_name)
    exec(compile(artifact.code, f"<{module_name}>", "exec"), namespace)  # noqa: S102

    transform = namespace.get("transform")
    flags = namespace.get("flags")
    if not callable(transform) or not callable(flags):
        raise ValueError("The stored adapter does not define transform() and flags().")

    return LoadedAdapter(
        key_slug=artifact.key.slug(),
        meta=dict(namespace.get("ADAPTER_META") or {}),
        review_flags=list(namespace.get("REVIEW_FLAGS") or []),
        _transform=transform,
        _flags=flags,
    )


# ----------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------


class RecordOutcome(BaseModel):
    index: int
    ok: bool = True
    output: dict | None = None
    flags: list[dict] = Field(default_factory=list)
    error: str | None = None


class MigrationRun(BaseModel):
    """The aggregate result of running an adapter over a batch."""

    adapter: str
    total: int = 0
    converted: int = 0
    failed: int = 0
    flagged: int = 0
    # Bounded: a run over ten thousand records must not build a ten-thousand
    # entry model in memory to report on itself. Failures are kept because
    # they are what someone will act on; successes are counted.
    failures: list[RecordOutcome] = Field(default_factory=list)
    flag_counts: dict[str, int] = Field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{self.converted}/{self.total} converted"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.flagged:
            parts.append(f"{self.flagged} flagged for review")
        return ", ".join(parts)


MAX_KEPT_FAILURES = 50


def convert(adapter: LoadedAdapter, records: Iterable[Any]) -> Iterator[RecordOutcome]:
    """Map every record, yielding one outcome each.

    A generator so a caller can stream ten thousand results to disk without
    holding them. An adapter that raises despite its contract costs its own
    record and nothing else.
    """
    for index, record in enumerate(records):
        try:
            output = adapter.transform(record)
        except Exception as exc:  # noqa: BLE001 - one bad record must not halt a batch
            yield RecordOutcome(index=index, ok=False, error=f"{type(exc).__name__}: {exc}")
            continue
        try:
            found = adapter.flags(record)
        except Exception:  # noqa: BLE001 - flags are advisory; losing them is not a failure
            found = []
        yield RecordOutcome(index=index, ok=True, output=output, flags=list(found))


def convert_all(adapter: LoadedAdapter, records: Iterable[Any]) -> tuple[list[dict], MigrationRun]:
    """Convert a batch and summarise it.

    Returns the converted records and the run report. Records that failed have
    no entry in the output list -- a partial migration you can see the shape of
    beats a complete one with holes you cannot.
    """
    run = MigrationRun(adapter=adapter.key_slug)
    converted: list[dict] = []
    for outcome in convert(adapter, records):
        run.total += 1
        if not outcome.ok:
            run.failed += 1
            if len(run.failures) < MAX_KEPT_FAILURES:
                run.failures.append(outcome)
            continue
        run.converted += 1
        converted.append(outcome.output or {})
        if outcome.flags:
            run.flagged += 1
            for flag in outcome.flags:
                reason = str(flag.get("reason", "unknown"))
                run.flag_counts[reason] = run.flag_counts.get(reason, 0) + 1
    return converted, run


# ----------------------------------------------------------------------
# Landing in the IR
# ----------------------------------------------------------------------


@dataclass
class IRResult:
    """A converted record validated against the IR model it claims to be."""

    model: BaseModel | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.model is not None


def to_ir(entity_kind: EntityKind, payload: dict) -> IRResult:
    """Validate an import adapter's output as the IR model it maps onto.

    The adapter produces a dict; the IR is a pydantic contract. Running the
    dict through that contract is what turns "the mapping ran" into "the
    mapping produced something the rest of Wheatear can use", and it is where a
    field the spec got wrong actually surfaces.
    """
    from wheatear.foundry.inspector import IR_MODELS  # noqa: PLC0415 - avoids an import cycle

    model = IR_MODELS.get(entity_kind)
    if model is None:
        return IRResult(errors=[f"The IR has no model for `{entity_kind.value}` records."])
    try:
        return IRResult(model=model.model_validate(payload))
    except ValidationError as exc:
        return IRResult(
            errors=[
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:10]
            ]
        )
