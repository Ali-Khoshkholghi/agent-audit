"""OS-level sandboxing for executing target-repository code.

This is deliberately independent of the SDK's `sandbox=SandboxSettings(...)`
option: that setting only configures Claude Code's own built-in Bash tool
sandbox (Seatbelt on macOS, bubblewrap on Linux) for commands *Claude*
chooses to run. It has no effect on code that runs inside our own `@tool`
Python handlers — see execute_case_against_target in tools/__init__.py,
which is what actually needs sandboxing here. So this module drives the
same OS primitives directly, ourselves.

Linux support (bubblewrap) is implemented per bubblewrap's documented flag
semantics but has not been exercised in this session — no Linux host was
available to test against. Treat it as unverified until it's been run for
real; the macOS (Seatbelt) backend has been smoke-tested directly.
"""
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    """No supported OS sandbox backend is available on this platform.

    Callers must fail closed on this — never fall back to running target
    code unsandboxed.
    """


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    backend: str


def run_sandboxed(
    argv: list[str], *, cwd: Path, scratch_dir: Path, timeout: float = 15.0
) -> SandboxResult:
    """Run `argv` with no network access and writes restricted to `scratch_dir`.

    `cwd` is readable but not writable — only `scratch_dir` is. Raises
    SandboxUnavailableError if this platform has no supported backend,
    rather than silently running unsandboxed.
    """
    system = platform.system()
    if system == "Darwin":
        return _run_seatbelt(argv, cwd=cwd, scratch_dir=scratch_dir, timeout=timeout)
    if system == "Linux":
        return _run_bubblewrap(argv, cwd=cwd, scratch_dir=scratch_dir, timeout=timeout)
    raise SandboxUnavailableError(
        f"no sandbox backend for platform {system!r} (supported: Darwin, Linux)"
    )


# macOS: Seatbelt via sandbox-exec. `(deny default)` blocks everything, then
# we carve out exactly what's needed: read anywhere (target code needs to
# read its own files plus the Python installation to import stdlib), write
# only under scratch_dir, network denied outright. subpath needs the
# *real* path — macOS routes /tmp through /private/tmp via a symlink, and
# Seatbelt subpath matching is literal, not symlink-aware.
_SEATBELT_PROFILE = """\
(version 1)
(deny default)
(allow file-read*)
(allow file-write* (subpath "{scratch_dir}"))
(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(deny network*)
"""


def _run_seatbelt(
    argv: list[str], *, cwd: Path, scratch_dir: Path, timeout: float
) -> SandboxResult:
    if shutil.which("sandbox-exec") is None:
        raise SandboxUnavailableError("sandbox-exec not found on PATH")

    profile = _SEATBELT_PROFILE.format(scratch_dir=str(scratch_dir.resolve()))
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sb", prefix="agentaudit-", delete=False
    ) as profile_file:
        profile_file.write(profile)
        profile_path = profile_file.name

    try:
        completed = subprocess.run(
            ["sandbox-exec", "-f", profile_path, *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SandboxResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            backend="seatbelt",
        )
    except subprocess.TimeoutExpired as e:
        return SandboxResult(
            returncode=-1,
            stdout=e.stdout or "",
            stderr=e.stderr or "",
            timed_out=True,
            backend="seatbelt",
        )
    finally:
        Path(profile_path).unlink(missing_ok=True)


# Linux/WSL2: bubblewrap. --ro-bind / / gives read access to the whole
# filesystem (matching the read side of the Seatbelt profile above);
# --bind scratch_dir scratch_dir (read-write, layered on top of the ro
# bind) is the only writable path; --unshare-net removes the network
# namespace entirely rather than trying to allowlist/denylist domains.
# UNTESTED in this session — see module docstring.
def _run_bubblewrap(
    argv: list[str], *, cwd: Path, scratch_dir: Path, timeout: float
) -> SandboxResult:
    if shutil.which("bwrap") is None:
        raise SandboxUnavailableError("bwrap (bubblewrap) not found on PATH")

    scratch_resolved = str(scratch_dir.resolve())
    bwrap_argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", scratch_resolved, scratch_resolved,
        "--unshare-net",
        "--die-with-parent",
        "--chdir", str(cwd),
        "--",
        *argv,
    ]

    try:
        completed = subprocess.run(
            bwrap_argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SandboxResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            backend="bubblewrap",
        )
    except subprocess.TimeoutExpired as e:
        return SandboxResult(
            returncode=-1,
            stdout=e.stdout or "",
            stderr=e.stderr or "",
            timed_out=True,
            backend="bubblewrap",
        )
