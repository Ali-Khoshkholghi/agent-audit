"""OS-level sandboxing for executing target-repository code.

This is deliberately independent of the SDK's `sandbox=SandboxSettings(...)`
option: that setting only configures Claude Code's own built-in Bash tool
sandbox (Seatbelt on macOS, bubblewrap on Linux) for commands *Claude*
chooses to run. It has no effect on code that runs inside our own `@tool`
Python handlers — see execute_case_against_target in tools/__init__.py,
which is what actually needs sandboxing here. So this module drives the
same OS primitives directly, ourselves.

Linux support (bubblewrap) is confirmed working, verified live on a real
Debian VM (M10) — see `_run_bubblewrap`'s docstring-comment for the one real
bug that exercising it for the first time found and fixed (`--tmpfs /tmp`
shadowing a target under `/tmp`). Post-fix, `checks/m6.py` passed with
ground-truth confirmation, not just the harness's own self-report: the
`/etc` write marker file genuinely did not exist on the real filesystem
afterward, and the audit ledger's tool-call count matched the message
stream's exactly. The macOS (Seatbelt) backend has been smoke-tested
directly, separately.
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
    argv: list[str], *, cwd: Path, scratch_dir: Path, timeout: float = 15.0, stdin: str = ""
) -> SandboxResult:
    """Run `argv` with no network access and writes restricted to `scratch_dir`.

    `cwd` is readable but not writable — only `scratch_dir` is. Raises
    SandboxUnavailableError if this platform has no supported backend,
    rather than silently running unsandboxed.

    `stdin` (M10) is always passed explicitly to subprocess.run's `input=`,
    even when empty — never omitted. Omitting it would make subprocess.run
    inherit this harness's own real stdin, which is both non-deterministic
    and an unintended leak into a subprocess that's supposed to be sandboxed.
    """
    system = platform.system()
    if system == "Darwin":
        return _run_seatbelt(argv, cwd=cwd, scratch_dir=scratch_dir, timeout=timeout, stdin=stdin)
    if system == "Linux":
        return _run_bubblewrap(argv, cwd=cwd, scratch_dir=scratch_dir, timeout=timeout, stdin=stdin)
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
    argv: list[str], *, cwd: Path, scratch_dir: Path, timeout: float, stdin: str = ""
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
            input=stdin,
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
#
# Exercised for real for the first time in M10 (audited a target cloned to
# /tmp) and found broken: --tmpfs /tmp mounts a fresh, empty tmpfs over
# /tmp, which shadows *everything* already ro-bound there by --ro-bind / /
# — including a target `cwd` that happens to live under /tmp (a cloned
# repo, or checks/m3.py's own tempfile.TemporaryDirectory() fixture; both
# hit `bwrap: Can't chdir to <path>: No such file or directory`).
# scratch_dir already avoided this by explicitly re-binding itself after
# the tmpfs mount; cwd gets the same explicit re-bind here for the same
# reason, unconditionally — harmless (redundant with --ro-bind / /) when
# cwd isn't under /tmp, load-bearing when it is. Post-fix, confirmed
# working live on a real Debian VM: checks/m6.py passed with ground-truth
# confirmation (the /etc write marker file genuinely absent afterward, the
# audit ledger's tool-call count matching the message stream exactly) —
# not just the harness's own self-report.
def _run_bubblewrap(
    argv: list[str], *, cwd: Path, scratch_dir: Path, timeout: float, stdin: str = ""
) -> SandboxResult:
    if shutil.which("bwrap") is None:
        raise SandboxUnavailableError("bwrap (bubblewrap) not found on PATH")

    cwd_resolved = str(cwd.resolve())
    scratch_resolved = str(scratch_dir.resolve())
    bwrap_argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--ro-bind", cwd_resolved, cwd_resolved,
        "--bind", scratch_resolved, scratch_resolved,
        "--unshare-net",
        "--die-with-parent",
        "--chdir", cwd_resolved,
        "--",
        *argv,
    ]

    try:
        completed = subprocess.run(
            bwrap_argv,
            input=stdin,
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
