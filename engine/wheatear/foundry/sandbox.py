"""Run generated adapter tests in isolation: no network, nothing left behind.

The Engineer executes code it just generated. That is the one place in Wheatear
where untrusted-by-construction code runs, so the containment is the feature,
not an implementation detail.

**No network.** `--network none` gives the container no interface at all. An
adapter is a pure function; if the generated code tries to call out, it fails
by construction rather than by our noticing.

**Nothing left behind.** There is no bind mount and no volume. The adapter and
its tests are serialised into a bootstrap script fed to the container on
stdin, which unpacks them into a tmpfs and runs them there. The host filesystem
is never exposed to the container in either direction, so "no files left" is
structural rather than a cleanup step that might not run. `--rm` disposes of
the writable layer, `--read-only` means there was nothing in it to dispose of,
and the tmpfs dies with the process.

**Bounded.** Memory, CPU, pid count and wall clock are all capped, all
dropped capabilities, `no-new-privileges`, and an unprivileged uid. A runaway
generation costs one timeout, not the machine.

The image needs pulling once (`docker pull python:3.11-slim`), which does need
network -- the *test run* does not. Nothing beyond the standard library is ever
installed, which is why generated adapters are stdlib-only: it keeps the image
stock and the run reproducible.

`SubprocessSandbox` is the fallback for machines with no container runtime. It
is honestly weaker -- rlimits and the static guard, but a real network stack --
and it is opt-in for that reason, never a silent substitute.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Protocol

from wheatear.foundry.types import CaseFailure, SandboxResult

# Stock image, no build step. Pinned to a minor version so a rebuild months
# later runs against the same interpreter the adapter was verified on.
DEFAULT_IMAGE = "python:3.11-slim"

DEFAULT_TIMEOUT_S = 180
DEFAULT_MEMORY_MB = 512
DEFAULT_PIDS = 128
DEFAULT_CPUS = 1.0

# Unprivileged uid present in essentially every base image.
RUN_AS = "65534:65534"

# How the bootstrap hands its structured result back across stdout, which also
# carries whatever the tests printed.
MARKER = "<<<WHEATEAR-FOUNDRY-RESULT>>>"

MAX_CAPTURED_OUTPUT = 8_000

BOOTSTRAP = '''
import io, json, os, sys, tempfile, unittest

PAYLOAD = json.loads({payload})
MARKER = {marker}

work = tempfile.mkdtemp(prefix="foundry-")
for name, text in PAYLOAD["files"].items():
    with open(os.path.join(work, name), "w", encoding="utf-8") as handle:
        handle.write(text)

sys.path.insert(0, work)
os.chdir(work)

stream = io.StringIO()
suite = unittest.TestLoader().discover(work, pattern="test_*.py")
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)

failures = [
    {{"name": str(test), "message": trace.strip().splitlines()[-1] if trace.strip() else ""}}
    for test, trace in list(result.failures) + list(result.errors)
]
detailed = [
    {{"name": str(test), "message": trace.strip()[-1200:]}}
    for test, trace in list(result.failures) + list(result.errors)
]
report = {{
    "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
    "failed": len(result.failures),
    "errors": len(result.errors),
    "total": result.testsRun,
    "failures": detailed or failures,
    "output": stream.getvalue()[-8000:],
}}
sys.stdout.write(MARKER + json.dumps(report))
sys.exit(0 if result.wasSuccessful() else 1)
'''


class Sandbox(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def run(self, files: dict[str, str]) -> SandboxResult: ...


def build_bootstrap(files: dict[str, str]) -> str:
    """Serialise the files into a self-unpacking script.

    Passing code on stdin rather than mounting a directory is what makes "no
    files left" a property of the design instead of a promise: there is no path
    from the container to the host filesystem to leave anything on.
    """
    payload = json.dumps({"files": files}, ensure_ascii=False)
    return BOOTSTRAP.format(payload=repr(payload), marker=repr(MARKER))


def parse_report(stdout: str) -> dict | None:
    marker = stdout.rfind(MARKER)
    if marker < 0:
        return None
    try:
        return json.loads(stdout[marker + len(MARKER) :])
    except ValueError:
        return None


def _result_from(report: dict | None, runner: str, exit_code: int, elapsed: float,
                 stdout: str, stderr: str) -> SandboxResult:
    if report is None:
        return SandboxResult(
            ok=False,
            runner=runner,
            exit_code=exit_code,
            duration_s=round(elapsed, 3),
            stdout=stdout[-MAX_CAPTURED_OUTPUT:],
            stderr=stderr[-MAX_CAPTURED_OUTPUT:],
            failures=[
                CaseFailure(
                    name="sandbox",
                    message=(
                        "The test run produced no result block. It most likely crashed "
                        "before the tests ran; see stderr."
                    ),
                )
            ],
        )
    failed = int(report.get("failed", 0))
    errors = int(report.get("errors", 0))
    return SandboxResult(
        ok=exit_code == 0 and failed == 0 and errors == 0,
        runner=runner,
        exit_code=exit_code,
        passed=int(report.get("passed", 0)),
        failed=failed,
        errors=errors,
        duration_s=round(elapsed, 3),
        failures=[
            CaseFailure(name=str(f.get("name", "?")), message=str(f.get("message", "")))
            for f in report.get("failures", [])
        ],
        stdout=str(report.get("output", ""))[-MAX_CAPTURED_OUTPUT:],
        stderr=stderr[-MAX_CAPTURED_OUTPUT:],
    )


# ----------------------------------------------------------------------
# Container runner
# ----------------------------------------------------------------------


class DockerSandbox:
    """Run the tests in a throwaway container with no network and no mounts."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        runtime: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        memory_mb: int = DEFAULT_MEMORY_MB,
        cpus: float = DEFAULT_CPUS,
    ) -> None:
        self.image = image
        self.runtime = runtime or _detect_runtime()
        self.timeout_s = timeout_s
        self.memory_mb = memory_mb
        self.cpus = cpus

    @property
    def name(self) -> str:
        return self.runtime or "container"

    def available(self) -> tuple[bool, str]:
        """Whether this machine can actually run a container, and why not.

        The common failure is not a missing binary but a socket the user can't
        talk to, and "permission denied on /var/run/docker.sock" deserves a
        different answer from "docker isn't installed" -- one is a group
        membership, the other is an install.
        """
        if not self.runtime:
            return False, (
                "No container runtime found. Install Docker or Podman, or pass "
                "--unsandboxed to run the tests in a plain subprocess (weaker isolation)."
            )
        try:
            probe = subprocess.run(
                [self.runtime, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Could not run `{self.runtime} version`: {exc}"

        if probe.returncode != 0:
            detail = " ".join((probe.stderr or probe.stdout).split())[:200]
            if "permission denied" in detail.lower():
                return False, (
                    f"`{self.runtime}` is installed but this user cannot reach its daemon "
                    f"({detail}). Add yourself to the `{self.runtime}` group and start a new "
                    "session, or use rootless Podman."
                )
            return False, f"`{self.runtime}` is installed but not usable: {detail}"
        return True, f"{self.runtime} {probe.stdout.strip()}"

    def image_present(self) -> bool:
        if not self.runtime:
            return False
        probe = subprocess.run(
            [self.runtime, "image", "inspect", self.image],
            capture_output=True,
            text=True,
        )
        return probe.returncode == 0

    def command(self, container_name: str) -> list[str]:
        """The full run command. Separated out so it can be asserted on.

        Every flag here is load-bearing, and a test that reads them is how a
        future edit that quietly drops `--network none` gets caught.
        """
        return [
            self.runtime or "docker",
            "run",
            "--rm",
            "--interactive",
            "--name",
            container_name,
            # No interface at all, not merely no route out.
            "--network",
            "none",
            # Nothing writable except an explicit, ephemeral tmpfs.
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m,mode=1777,noexec,nosuid,nodev",
            "--memory",
            f"{self.memory_mb}m",
            "--memory-swap",
            f"{self.memory_mb}m",
            "--pids-limit",
            str(DEFAULT_PIDS),
            "--cpus",
            str(self.cpus),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            RUN_AS,
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--workdir",
            "/tmp",
            self.image,
            # -B no bytecode, -s no user site, -E ignore PYTHON* env.
            "python",
            "-B",
            "-s",
            "-E",
            "-",
        ]

    def run(self, files: dict[str, str]) -> SandboxResult:
        ready, why = self.available()
        if not ready:
            return SandboxResult(ok=False, runner=self.name, stderr=why,
                                 failures=[CaseFailure(name="sandbox", message=why)])
        if not self.image_present():
            message = (
                f"The image `{self.image}` is not present locally. Run "
                f"`{self.runtime} pull {self.image}` once; the test run itself needs no network."
            )
            return SandboxResult(ok=False, runner=self.name, stderr=message,
                                 failures=[CaseFailure(name="sandbox", message=message)])

        container = f"wheatear-foundry-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                self.command(container),
                input=build_bootstrap(files),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            # The CLI has been killed; the container has not. Removing it by
            # name is the only way to be sure nothing is left running.
            subprocess.run(
                [self.runtime or "docker", "rm", "-f", container],
                capture_output=True,
                text=True,
            )
            message = (
                f"The test run exceeded {self.timeout_s}s and the container was destroyed. "
                "The generated adapter most likely contains an unbounded loop."
            )
            return SandboxResult(
                ok=False,
                runner=self.name,
                exit_code=124,
                duration_s=float(self.timeout_s),
                stderr=message,
                failures=[CaseFailure(name="sandbox", message=message)],
            )
        except OSError as exc:
            return SandboxResult(ok=False, runner=self.name, stderr=str(exc),
                                 failures=[CaseFailure(name="sandbox", message=str(exc))])

        elapsed = time.monotonic() - started
        return _result_from(
            parse_report(completed.stdout),
            self.name,
            completed.returncode,
            elapsed,
            completed.stdout,
            completed.stderr,
        )


def _detect_runtime() -> str | None:
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


# ----------------------------------------------------------------------
# Fallback runner
# ----------------------------------------------------------------------


class SubprocessSandbox:
    """Run the tests in a resource-limited subprocess.

    For machines with no container runtime. Weaker, and the difference is worth
    stating plainly rather than burying: the process has a real network stack.
    Nothing but the static guard stops generated code from opening a socket,
    which is why this runner is opt-in and why the guard's import allowlist has
    no networking module on it.

    What it does still enforce: an address-space cap, a CPU-seconds cap, a file
    -size limit of zero so nothing can be written anywhere, a wall-clock
    timeout, and a scratch directory removed afterwards whatever happens.
    """

    name = "subprocess"

    def __init__(
        self,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        memory_mb: int = DEFAULT_MEMORY_MB,
        python: str | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.memory_mb = memory_mb
        self.python = python or _current_python()

    def available(self) -> tuple[bool, str]:
        return (True, f"{self.python} (no container isolation)")

    def _limits(self):
        """rlimits applied in the child, before exec.

        Returns None where `resource` is unavailable (Windows), in which case
        the wall-clock timeout is the only bound and `available()` has already
        said what this runner is.
        """
        try:
            import resource  # noqa: PLC0415 - POSIX only, imported where used
        except ImportError:
            return None

        memory_bytes = self.memory_mb * 1024 * 1024
        timeout = self.timeout_s

        # RLIMIT_AS is the one limit that doesn't hold everywhere: macOS does
        # not enforce an address-space cap and setrlimit(RLIMIT_AS) raises
        # there, which -- from a preexec_fn -- kills the child with exit 255.
        # So it is skipped *only on darwin* (a developer machine, where the
        # wall-clock timeout is bound enough). Every other platform, Linux
        # servers included, keeps the full set applied strictly: a limit that
        # fails to set there is raised, not swallowed, because a silently
        # unbounded child on a server is worse than a loud error. This is
        # deliberately not a blanket try/except -- masking a real failure on the
        # host that actually enforces these limits is the thing to avoid.
        skip_memory_limit = sys.platform == "darwin"

        def apply() -> None:
            if not skip_memory_limit:
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
            # Zero-length file limit: the harness reads its inputs from a
            # directory the host wrote, and has no reason to create anything.
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

        return apply

    def run(self, files: dict[str, str]) -> SandboxResult:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="wheatear-sandbox-") as work:
            root = Path(work)
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            # Same bootstrap as the container path, with one substitution: the
            # files are already on disk here, so it runs in this directory
            # instead of unpacking a payload into a fresh one. Reusing the
            # script keeps both runners reporting results in one format.
            runner = root / "_run.py"
            runner.write_text(
                build_bootstrap({}).replace(
                    'work = tempfile.mkdtemp(prefix="foundry-")',
                    "work = os.path.dirname(os.path.abspath(__file__))",
                ),
                encoding="utf-8",
            )

            try:
                completed = subprocess.run(
                    [self.python, "-B", "-s", "-E", str(runner)],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    cwd=str(root),
                    env={"PATH": os.environ.get("PATH", ""), "HOME": str(root)},
                    preexec_fn=self._limits(),  # noqa: PLW1509 - bounding the child is the point
                )
            except subprocess.TimeoutExpired:
                message = f"The test run exceeded {self.timeout_s}s and was killed."
                return SandboxResult(
                    ok=False,
                    runner=self.name,
                    exit_code=124,
                    duration_s=float(self.timeout_s),
                    stderr=message,
                    failures=[CaseFailure(name="sandbox", message=message)],
                )
            except OSError as exc:
                return SandboxResult(
                    ok=False,
                    runner=self.name,
                    stderr=str(exc),
                    failures=[CaseFailure(name="sandbox", message=str(exc))],
                )

            elapsed = time.monotonic() - started
            return _result_from(
                parse_report(completed.stdout),
                self.name,
                completed.returncode,
                elapsed,
                completed.stdout,
                completed.stderr,
            )


def _current_python() -> str:
    import sys  # noqa: PLC0415 - only needed to locate the interpreter

    return sys.executable or "python3"


def default_sandbox(allow_subprocess: bool = False, **kwargs) -> Sandbox:
    """The best available runner.

    A container if one can actually be reached, the subprocess fallback only
    when explicitly permitted. Silently degrading from "no network" to "full
    network" would be exactly the wrong default for the one component that
    executes generated code.
    """
    container = DockerSandbox(**kwargs)
    ready, _ = container.available()
    if ready or not allow_subprocess:
        return container
    return SubprocessSandbox(
        timeout_s=kwargs.get("timeout_s", DEFAULT_TIMEOUT_S),
        memory_mb=kwargs.get("memory_mb", DEFAULT_MEMORY_MB),
    )
