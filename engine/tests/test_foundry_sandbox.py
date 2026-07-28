"""Running generated code, and loading it later.

The sandbox is the one place in Agent Liftoff where code that was written minutes
ago by a model is executed, so the containment flags are asserted directly
rather than assumed -- a future edit that quietly drops `--network none` should
fail a test, not a customer.

The subprocess runner is exercised for real (it needs nothing installed); the
container runner is asserted on its command line, because a test that required
a working Docker daemon would be skipped on exactly the machines where it
matters most.
"""

import ast
import json

import pytest

from agent_liftoff.foundry import runtime, sandbox
from agent_liftoff.foundry.sandbox import DockerSandbox, SubprocessSandbox, build_bootstrap, parse_report
from agent_liftoff.foundry.types import (
    AdapterArtifact,
    AdapterKey,
    Direction,
    EntityKind,
    MappingSpec,
    SandboxResult,
)

PASSING_ADAPTER = (
    "def transform(record):\n"
    "    if not isinstance(record, dict):\n"
    "        return {}\n"
    "    return {'name': record.get('n')} if 'n' in record else {}\n"
    "\n\n"
    "def flags(record):\n"
    "    return []\n"
)

PASSING_TESTS = (
    "import unittest\n"
    "import adapter\n"
    "\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_maps(self):\n"
    "        self.assertEqual(adapter.transform({'n': 'x'}), {'name': 'x'})\n"
    "\n"
    "    def test_omits(self):\n"
    "        self.assertEqual(adapter.transform({}), {})\n"
)

FAILING_TESTS = (
    "import unittest\n"
    "import adapter\n"
    "\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_wrong(self):\n"
    "        self.assertEqual(adapter.transform({'n': 'x'}), {'name': 'WRONG'})\n"
)


def _key() -> AdapterKey:
    return AdapterKey(
        platform="acme",
        direction=Direction.IMPORT,
        entity_kind=EntityKind.AGENT,
        schema_fingerprint="f" * 64,
    )


def _artifact(code=PASSING_ADAPTER, ok=True) -> AdapterArtifact:
    return AdapterArtifact(
        key=_key(),
        code=code,
        tests=PASSING_TESTS,
        spec=MappingSpec(
            platform="acme",
            direction=Direction.IMPORT,
            entity_kind=EntityKind.AGENT,
            schema_fingerprint="f" * 64,
        ),
        report=SandboxResult(ok=ok, passed=2 if ok else 0, runner="test"),
    )


# ----------------------------------------------------------------------
# Containment
# ----------------------------------------------------------------------


def test_the_container_has_no_network_interface_at_all():
    command = DockerSandbox(runtime="docker").command("c")
    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"


def test_nothing_from_the_host_filesystem_is_ever_mounted():
    """"No files left" is structural here rather than a cleanup step that might
    not run: there is no path from the container to the host to leave anything
    on. The code arrives on stdin.
    """
    command = DockerSandbox(runtime="docker").command("c")
    assert not {"-v", "--volume", "--mount"} & set(command)
    assert "--read-only" in command
    assert "--rm" in command
    assert command[-1] == "-"  # the program is read from stdin


def test_the_container_is_bounded_and_unprivileged():
    command = DockerSandbox(runtime="docker", memory_mb=256).command("c")
    assert command[command.index("--memory") + 1] == "256m"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--user") + 1] == sandbox.RUN_AS
    assert "--pids-limit" in command


def test_the_interpreter_ignores_ambient_python_configuration():
    command = DockerSandbox(runtime="docker").command("c")
    tail = command[command.index("python") :]
    assert {"-B", "-s", "-E"} <= set(tail)


def test_a_missing_runtime_is_reported_as_advice_not_a_crash():
    box = DockerSandbox()
    box.runtime = None  # as if neither docker nor podman were installed
    ready, why = box.available()
    assert ready is False
    assert "--unsandboxed" in why


# ----------------------------------------------------------------------
# The payload
# ----------------------------------------------------------------------


def test_the_bootstrap_carries_the_files_and_survives_quoting():
    """The adapter is embedded in a script as a literal. Generated code
    routinely contains both quote characters and backslashes, and a hand-rolled
    quoter would mangle exactly those.
    """
    awkward = "def transform(record):\n    return {'q': \"it's \\\"quoted\\\"\"}\n"
    script = build_bootstrap({"adapter.py": awkward})
    compile(script, "<bootstrap>", "exec")

    # A repr escapes newlines, so the first `)\n` is reliably the end of it.
    literal = script.split("PAYLOAD = json.loads(", 1)[1].split(")\n", 1)[0]
    payload = json.loads(ast.literal_eval(literal))
    assert payload["files"]["adapter.py"] == awkward


def test_a_run_that_produced_no_result_block_is_a_failure_with_an_explanation():
    result = sandbox._result_from(parse_report("garbage"), "docker", 1, 0.5, "garbage", "boom")
    assert result.ok is False
    assert "crashed before the tests ran" in result.failures[0].message


def test_the_result_block_is_found_even_after_test_output():
    report = parse_report("lots of output\n" + sandbox.MARKER + '{"passed": 3, "failed": 0}')
    assert report == {"passed": 3, "failed": 0}


# ----------------------------------------------------------------------
# The fallback runner, for real
# ----------------------------------------------------------------------


def test_a_passing_suite_runs_and_is_reported():
    result = SubprocessSandbox(timeout_s=60).run(
        {"adapter.py": PASSING_ADAPTER, "test_adapter.py": PASSING_TESTS}
    )
    assert result.ok is True
    assert result.passed == 2
    assert result.runner == "subprocess"


def test_a_failing_suite_returns_the_failure_text_for_the_repair_loop():
    result = SubprocessSandbox(timeout_s=60).run(
        {"adapter.py": PASSING_ADAPTER, "test_adapter.py": FAILING_TESTS}
    )
    assert result.ok is False
    assert result.failed == 1
    assert "test_wrong" in result.feedback()


def test_a_suite_that_never_finishes_is_killed_rather_than_hanging_the_build():
    spinning = (
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_spin(self):\n"
        "        while True:\n"
        "            pass\n"
    )
    result = SubprocessSandbox(timeout_s=2).run(
        {"adapter.py": PASSING_ADAPTER, "test_adapter.py": spinning}
    )
    assert result.ok is False


def test_the_fallback_runner_says_what_it_does_not_protect_against():
    _, why = SubprocessSandbox().available()
    assert "no container isolation" in why


# ----------------------------------------------------------------------
# Loading a stored adapter
# ----------------------------------------------------------------------


def test_an_adapter_whose_tests_never_passed_is_refused_by_default():
    """An unverified adapter is worth reading and worth finishing by hand. It
    is not worth running unattended over a customer's tenant.
    """
    with pytest.raises(ValueError, match="has not passed its tests"):
        runtime.load(_artifact(ok=False))
    assert runtime.load(_artifact(ok=False), verified_only=False)


def test_the_guard_runs_again_at_load_time():
    """The sandbox verified this code weeks ago in a different process. The
    file has been on disk in between; nothing about that run protects this one.
    """
    tampered = _artifact(code="import os\n" + PASSING_ADAPTER)
    with pytest.raises(ValueError, match="failed the safety check"):
        runtime.load(tampered)


def test_an_adapter_module_cannot_import_its_way_out():
    """Second layer, in case a module reached disk by some route the guard
    never saw.
    """
    with pytest.raises(ImportError):
        runtime._restricted_import("socket")
    with pytest.raises(ImportError):
        runtime._restricted_import("urllib.request")
    assert runtime._restricted_import("re")


def test_the_execution_namespace_has_no_io_builtins():
    assert "open" not in runtime.SAFE_BUILTINS
    assert "eval" not in runtime.SAFE_BUILTINS
    assert "exec" not in runtime.SAFE_BUILTINS
    assert "print" not in runtime.SAFE_BUILTINS
    assert "isinstance" in runtime.SAFE_BUILTINS


# ----------------------------------------------------------------------
# Running over a batch
# ----------------------------------------------------------------------


def test_one_bad_record_does_not_halt_the_batch():
    """A traceback four hours into a ten-thousand-agent migration is the thing
    this whole component exists to avoid.
    """
    exploding = (
        "def transform(record):\n"
        "    if record.get('boom'):\n"
        "        raise ValueError('bad record')\n"
        "    return {'name': record.get('n')}\n"
        "\n\n"
        "def flags(record):\n"
        "    return []\n"
    )
    adapter = runtime.load(_artifact(code=exploding), verified_only=False)
    records = [{"n": "a"}, {"boom": True}, {"n": "c"}]
    converted, report = runtime.convert_all(adapter, records)

    assert report.total == 3
    assert report.converted == 2
    assert report.failed == 1
    assert report.failures[0].index == 1
    assert "bad record" in report.failures[0].error
    assert converted == [{"name": "a"}, {"name": "c"}]


def test_flags_are_counted_by_reason_across_the_batch():
    flagging = (
        "def transform(record):\n"
        "    return {}\n"
        "\n\n"
        "def flags(record):\n"
        "    if record.get('secret'):\n"
        "        return [{'path': 'secret', 'reason': 'requires_auth', 'severity': 'warn'}]\n"
        "    return []\n"
    )
    adapter = runtime.load(_artifact(code=flagging), verified_only=False)
    _, report = runtime.convert_all(adapter, [{"secret": 1}, {}, {"secret": 2}])
    assert report.flagged == 2
    assert report.flag_counts == {"requires_auth": 2}
    assert "2 flagged for review" in report.summary()


def test_a_converted_record_is_validated_against_the_ir_contract():
    """The adapter produces a dict; the IR is a pydantic contract. Running the
    dict through it is where a field the spec got wrong actually surfaces.
    """
    good = runtime.to_ir(
        EntityKind.AGENT, {"name": "HR", "source_platform": "copilot-studio", "instructions": "x"}
    )
    assert good.ok
    assert good.model.name == "HR"

    bad = runtime.to_ir(EntityKind.AGENT, {"instructions": "x"})
    assert not bad.ok
    assert any("name" in error for error in bad.errors)


def test_an_entity_kind_the_ir_cannot_represent_says_so():
    result = runtime.to_ir(EntityKind.TRIGGER, {"schedule": "daily"})
    assert not result.ok
    assert "no model for" in result.errors[0]
