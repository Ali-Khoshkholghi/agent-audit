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
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
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
    argv: list[str],
    *,
    cwd: Path,
    scratch_dir: Path,
    timeout: float = 15.0,
    stdin: str = "",
    allow_network: bool = False,
    env: dict[str, str] | None = None,
    read_paths: list[Path] | None = None,
) -> SandboxResult:
    """Run `argv` with writes restricted to `scratch_dir`.

    `cwd` is readable but not writable — only `scratch_dir` is. Raises
    SandboxUnavailableError if this platform has no supported backend,
    rather than silently running unsandboxed.

    `stdin` (M10) is always passed explicitly to subprocess.run's `input=`,
    even when empty — never omitted. Omitting it would make subprocess.run
    inherit this harness's own real stdin, which is both non-deterministic
    and an unintended leak into a subprocess that's supposed to be sandboxed.

    `allow_network` (M11) defaults to False — the existing no-network
    posture for target execution (M6) is unchanged. The one caller that
    passes True is the dependency-install step, which needs to reach PyPI;
    see `install_target_dependencies`'s module docstring for why this is a
    soft (pip-level), not OS-enforced, boundary.

    `env` (M11) is passed straight through to subprocess.run. None (the
    default, and every pre-M11 caller) means full parent-env passthrough,
    unchanged from before this parameter existed.

    `read_paths` (M11) defaults to None, meaning "read access to the whole
    host filesystem" — the original, unchanged posture for target execution
    and venv creation (neither runs genuinely untrusted code with network
    open, so broad read is fine: even a compromised entry_point can't
    exfiltrate what it reads, since execution stays network-denied). When
    given, this replaces `--ro-bind / /` (bwrap) / blanket `(allow
    file-read*)` (Seatbelt) with read-only binds of just these specific
    paths. The one caller that passes this is the `pip install` step in
    `install_target_dependencies`, specifically *because* that's the one
    place both broad read and open network are true simultaneously — a
    malicious package's build script could otherwise read anything the
    harness process can (SSH keys, cloud credentials, its own source) and
    exfiltrate it. Narrowing what's readable there caps that, independent
    of the network boundary staying soft.
    """
    system = platform.system()
    if system == "Darwin":
        return _run_seatbelt(
            argv,
            cwd=cwd,
            scratch_dir=scratch_dir,
            timeout=timeout,
            stdin=stdin,
            allow_network=allow_network,
            env=env,
            read_paths=read_paths,
        )
    if system == "Linux":
        return _run_bubblewrap(
            argv,
            cwd=cwd,
            scratch_dir=scratch_dir,
            timeout=timeout,
            stdin=stdin,
            allow_network=allow_network,
            env=env,
            read_paths=read_paths,
        )
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

# M11: same profile, minus the network deny — used only for the
# dependency-install step (install_target_dependencies), never for target
# execution. Filesystem restrictions are identical to the network-denied
# profile above.
_SEATBELT_PROFILE_NETWORK = """\
(version 1)
(deny default)
(allow file-read*)
(allow file-write* (subpath "{scratch_dir}"))
(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow network*)
"""

# M11: used only when `read_paths` narrows the readable filesystem (the
# `pip install` sub-step specifically — see run_sandboxed's docstring for
# why). Replaces the blanket `(allow file-read*)` above with subpath rules
# for cwd, scratch_dir, and each entry in read_paths; network stays
# unconditionally allowed, since this profile is only ever used for that
# one network-enabled call.
_SEATBELT_PROFILE_NETWORK_NARROW_READ = """\
(version 1)
(deny default)
{read_rules}
(allow file-write* (subpath "{scratch_dir}"))
(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow network*)
"""


def _run_seatbelt(
    argv: list[str],
    *,
    cwd: Path,
    scratch_dir: Path,
    timeout: float,
    stdin: str = "",
    allow_network: bool = False,
    env: dict[str, str] | None = None,
    read_paths: list[Path] | None = None,
) -> SandboxResult:
    if shutil.which("sandbox-exec") is None:
        raise SandboxUnavailableError("sandbox-exec not found on PATH")

    if read_paths is not None:
        rules = [f'(allow file-read* (subpath "{p.resolve()}"))' for p in read_paths if p.exists()]
        rules.append(f'(allow file-read* (subpath "{cwd.resolve()}"))')
        rules.append(f'(allow file-read* (subpath "{scratch_dir.resolve()}"))')
        profile = _SEATBELT_PROFILE_NETWORK_NARROW_READ.format(
            read_rules="\n".join(rules), scratch_dir=str(scratch_dir.resolve())
        )
    else:
        template = _SEATBELT_PROFILE_NETWORK if allow_network else _SEATBELT_PROFILE
        profile = template.format(scratch_dir=str(scratch_dir.resolve()))
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
            env=env,
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
    argv: list[str],
    *,
    cwd: Path,
    scratch_dir: Path,
    timeout: float,
    stdin: str = "",
    allow_network: bool = False,
    env: dict[str, str] | None = None,
    read_paths: list[Path] | None = None,
) -> SandboxResult:
    if shutil.which("bwrap") is None:
        raise SandboxUnavailableError("bwrap (bubblewrap) not found on PATH")

    cwd_resolved = str(cwd.resolve())
    scratch_resolved = str(scratch_dir.resolve())

    if read_paths is None:
        root_args = ["--ro-bind", "/", "/"]
    else:
        # M11: narrowed read scope for the one caller (pip install) that
        # has both broad read and open network at the same time — see
        # run_sandboxed's docstring. --tmpfs / gives an empty root instead
        # of the whole host; only these specific paths get bound onto it.
        #
        # Deliberately NOT .resolve()'d: on a usrmerge system (e.g. Debian),
        # /bin, /sbin, /lib, /lib64 are symlinks into /usr. Resolving them
        # first collapses their *destination* path onto /usr/bin etc too —
        # nesting that bind under the already-bound /usr mountpoint. That
        # nested-under-nested pattern corrupts bwrap's mount table badly
        # enough that unrelated later binds (the scratch_dir bind that's
        # supposed to expose venv/bin/python) silently stop working, with
        # no error pointing back at the real cause (confirmed by manually
        # bisecting the exact bind list — this is not a hypothetical).
        # Binding at the literal requested path instead makes /bin and
        # /usr siblings in the new namespace's tmpfs root, not nested, and
        # the kernel resolves the symlink on the *source* side normally.
        root_args = ["--tmpfs", "/"]
        for p in read_paths:
            if p.exists():
                literal = str(p)
                root_args += ["--ro-bind", literal, literal]

    bwrap_argv = [
        "bwrap",
        *root_args,
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--ro-bind", cwd_resolved, cwd_resolved,
        "--bind", scratch_resolved, scratch_resolved,
        *([] if allow_network else ["--unshare-net"]),
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
            env=env,
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


# M11: per-target ephemeral dependency installation. v1 supports
# pyproject.toml only (confirmed live against langchain-ai/react-agent: it
# has one, with PEP 621 [project.dependencies] and a setuptools
# [build-system] — no requirements.txt anywhere in the repo).
#
# Network posture: the install subprocess runs through the same
# run_sandboxed() as target execution (filesystem confinement unchanged —
# only scratch_dir is writable) but with allow_network=True, since pip has
# to actually reach PyPI. This environment has neither iptables/nft nor
# passwordless root, so there is no OS-level per-host firewall available to
# build inside the sandbox's network namespace — bubblewrap's own network
# control is all-or-nothing (--unshare-net or not). What scoping there is
# comes from pip itself: --index-url pinned to pypi.org, matching
# --trusted-host entries, nothing else configured. This is a soft,
# pip-level boundary, not an OS-enforced one — a malicious sdist's build
# script could in principle still reach other hosts during its own build
# step. Documented here deliberately, same as M6's SandboxSettings
# trust-boundary note, rather than implied to be stronger than it is.
#
# That said, network being open here is not the only thing that changes
# for this one call: target execution and venv creation also get read
# access to the *whole* host filesystem (`--ro-bind / /` / blanket
# `(allow file-read*)`) — fine for them, since neither has network, so
# even a compromised entry_point can't exfiltrate what it reads. `pip
# install` of an untrusted package is different: it genuinely executes
# attacker-controlled code (setup.py / PEP 517 build hooks), and with both
# broad read *and* open network true at once, that code could read
# anything the harness process can (SSH keys, cloud credentials, this
# repo's own source) and send it anywhere — a real exfiltration channel,
# not just "an install that might fetch from an unexpected host." So this
# one call additionally passes `read_paths` (see run_sandboxed's
# docstring), replacing read-everywhere with a curated allowlist: this
# process's own Python installation (`sys.base_prefix`/`base_exec_prefix`
# — resolved at runtime rather than hardcoded, since it may not live under
# /usr; a venv or conda env commonly doesn't), the base OS directories
# needed to exec anything at all (/usr, /bin, /sbin, /lib, /lib64 — shipped
# software, not secrets), and a handful of specific /etc files needed for
# DNS resolution and TLS (not all of /etc, which also holds real secrets
# like /etc/ssh's host keys). This narrows what a malicious build script
# could steal even though network stays open; it does not, and cannot
# within this environment's constraints, close the network side itself.
_INSTALL_READ_PATHS = [
    Path("/usr"), Path("/bin"), Path("/sbin"),
    Path("/lib"), Path("/lib64"),
    Path("/etc/resolv.conf"), Path("/etc/nsswitch.conf"), Path("/etc/hosts"),
    Path("/etc/host.conf"), Path("/etc/gai.conf"),
    Path("/etc/ld.so.cache"), Path("/etc/ld.so.conf"), Path("/etc/ld.so.conf.d"),
    Path("/etc/ssl"),
]


def _install_read_paths() -> list[Path]:
    paths = list(_INSTALL_READ_PATHS)
    for p in (sys.base_prefix, sys.base_exec_prefix):
        resolved = Path(p)
        if resolved not in paths:
            paths.append(resolved)
    return paths
#
# The install command is `pip install <target_dir>` (the target itself),
# not a hand-parsed list of dependency strings: pip resolves
# [project.dependencies] *and* builds/installs the target's own package via
# its declared [build-system] backend in one step. That second half matters
# — react_agent/graph.py does `from react_agent.context import Context`,
# which only resolves once the react_agent package itself is installed
# (M9's "Watch for" note), not just its third-party dependencies.
_PYPI_INDEX_ARGS = [
    "--index-url", "https://pypi.org/simple",
    "--trusted-host", "pypi.org",
    "--trusted-host", "files.pythonhosted.org",
]


@dataclass
class DependencyInstallResult:
    ok: bool
    python_path: Path | None
    reason: str  # populated whenever ok is False; concrete, never fabricated
    # True only for "target has no pyproject.toml at all" — callers must
    # treat that as "nothing to install" (fall back to sys.executable), not
    # as an install failure. A structural field, not a substring match on
    # `reason`: `reason` also carries raw pip/venv stderr tails for real
    # failures, and a coincidental phrase match there would silently
    # misclassify a genuine failure as a skip.
    skip: bool = False


def _venv_python(venv_dir: Path) -> Path:
    # Windows isn't a supported platform here (run_sandboxed only handles
    # Darwin/Linux), so venv's own "Scripts" layout never applies.
    return venv_dir / "bin" / "python"


def install_target_dependencies(
    target: Path, scratch_dir: Path, *, timeout: float = 180.0
) -> DependencyInstallResult:
    """Create a fresh venv under `scratch_dir` and install `target`'s own
    declared dependencies into it, sandboxed the same way target execution
    is. Returns is_ok=False with `python_path=None` and a concrete `reason`
    for every way this can fail to produce a usable interpreter — including
    the case where `target` has no pyproject.toml at all, which callers
    should treat as "nothing to install" (skip), not "install failed": see
    execute_case_against_target's docstring for that distinction.
    """
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        return DependencyInstallResult(
            ok=False,
            python_path=None,
            reason=f"no pyproject.toml found at {pyproject} — dependency installation not attempted",
            skip=True,
        )

    try:
        manifest = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        return DependencyInstallResult(
            ok=False, python_path=None, reason=f"could not parse {pyproject}: {e}"
        )
    if "project" not in manifest:
        return DependencyInstallResult(
            ok=False,
            python_path=None,
            reason=(
                f"{pyproject} has no [project] table (checked for PEP 621 metadata; "
                f"e.g. a poetry-only [tool.poetry] manifest isn't supported in v1)"
            ),
        )

    # pip's build backend (setuptools, here) writes build metadata
    # (<pkg>.egg-info/) directly into the source directory it's given —
    # this is standard pip behaviour for a local-path install, not
    # something we opted into. `target` is read-only in the sandbox (never
    # mutate the thing under audit), so install from a throwaway copy under
    # scratch_dir instead of `target` itself. Torn down with the rest of
    # scratch_dir's venv cleanup.
    src_copy = scratch_dir / "install_src"
    shutil.rmtree(src_copy, ignore_errors=True)
    shutil.copytree(target, src_copy, ignore=shutil.ignore_patterns(".git"))

    venv_dir = scratch_dir / "venv"
    venv_env = {
        **os.environ,
        "HOME": str(scratch_dir),
        "TMPDIR": str(scratch_dir),
        "PIP_NO_CACHE_DIR": "1",
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }

    # `timeout` bounds the *whole* install (venv creation + pip install)
    # combined, not each subprocess independently — otherwise a slow venv
    # creation plus a slow pip install could together take up to ~2x
    # `timeout` before this time-box actually kicks in.
    deadline = time.monotonic() + timeout

    try:
        try:
            create = run_sandboxed(
                [sys.executable, "-m", "venv", str(venv_dir)],
                cwd=target,
                scratch_dir=scratch_dir,
                timeout=timeout,
                allow_network=False,
                env=venv_env,
            )
        except SandboxUnavailableError as e:
            return DependencyInstallResult(
                ok=False, python_path=None, reason=f"cannot create venv: no sandbox backend ({e})"
            )
        if create.timed_out:
            shutil.rmtree(venv_dir, ignore_errors=True)
            return DependencyInstallResult(
                ok=False, python_path=None, reason=f"venv creation timed out after {timeout}s"
            )
        if create.returncode != 0:
            shutil.rmtree(venv_dir, ignore_errors=True)
            return DependencyInstallResult(
                ok=False,
                python_path=None,
                reason=f"venv creation failed (returncode={create.returncode}): {create.stderr[-2000:]}",
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            shutil.rmtree(venv_dir, ignore_errors=True)
            return DependencyInstallResult(
                ok=False,
                python_path=None,
                reason=(
                    f"install timed out: venv creation alone consumed the full "
                    f"{timeout}s budget, no time left to run pip install"
                ),
            )

        python_path = _venv_python(venv_dir)
        install = run_sandboxed(
            [str(python_path), "-m", "pip", "install", *_PYPI_INDEX_ARGS, str(src_copy)],
            cwd=src_copy,
            scratch_dir=scratch_dir,
            timeout=remaining,
            allow_network=True,
            env=venv_env,
            read_paths=_install_read_paths(),
        )
        if install.timed_out:
            shutil.rmtree(venv_dir, ignore_errors=True)
            return DependencyInstallResult(
                ok=False,
                python_path=None,
                reason=(
                    f"pip install timed out after {remaining:.1f}s "
                    f"({timeout}s total budget, remainder after venv creation)"
                ),
            )
        if install.returncode != 0:
            shutil.rmtree(venv_dir, ignore_errors=True)
            return DependencyInstallResult(
                ok=False,
                python_path=None,
                reason=f"pip install failed (returncode={install.returncode}): {install.stderr[-2000:]}",
            )

        return DependencyInstallResult(ok=True, python_path=python_path, reason="")
    finally:
        # install_src is a purely internal implementation detail (a
        # writable copy made only because setuptools writes .egg-info into
        # its source dir) — never needed after this call returns, success
        # or failure alike. venv_dir, by contrast, is the caller's actual
        # result on success, so it's deliberately NOT touched here.
        shutil.rmtree(src_copy, ignore_errors=True)
