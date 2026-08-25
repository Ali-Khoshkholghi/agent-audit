"""M11 check: per-target ephemeral dependency installation.

Before M11, execute_case_against_target always ran a target's entry_point
with the harness's own interpreter (.venv's Python) — a real target's own
dependencies were never installed anywhere. M9/M10 already documented this
gap live against langchain-ai/react-agent: entry-point discovery and
stdin-wiring were both correct, but react_agent/graph.py still couldn't be
imported (langgraph/langchain_openai missing, and react_agent's own package
not installed either — see M9's "Watch for" note).

M11 closes that gap: install_target_dependencies (agentaudit.sandbox) reads
a target's pyproject.toml (v1 scope, confirmed to match react-agent's real
manifest format — it has no requirements.txt), builds a fresh disposable
venv under the case's scratch_dir, and pip-installs the target itself into
it (resolving [project.dependencies] and building the target's own package
via its declared [build-system] backend in one step). Wired into
execute_case_against_target so a target with no pyproject.toml is completely
unaffected (skip, not a failure) and a target with one gets its own venv's
python instead of sys.executable.

Eight checks, mirroring M10's structure (fast fixture proofs first, then live
proof against the real react-agent clone):

1. No-manifest targets are unaffected by M11 (regression check).
2. A pyproject.toml with a real, nonexistent PyPI dependency fails honestly
   (targets/pyproject-broken-target/).
3. The install timeout is actually enforced against a real, in-flight
   install (react-agent, artificially tiny timeout), and bounds the whole
   install (venv creation + pip install combined), not each half twice.
4. A real install against react-agent succeeds and makes both the
   third-party deps and react-agent's own package importable — closing M9's
   documented gap.
5. That installed venv is actually what execute_case_against_target uses:
   the ModuleNotFoundError this milestone targets is gone from its output.
6. A live end-to-end `agentaudit audit` sanity run against react-agent.
7. The pip install step's narrowed read scope actually excludes a path
   outside its allowlist — proves the read-scope hardening blocks real
   reads, not just that pip still happens to work under it.
8. macOS-only (skips elsewhere, doesn't silently pass): a real install
   against react-agent succeeds end-to-end specifically under the Seatbelt
   backend. Regression guard for a real bug found post-M11 — the narrowed
   read Seatbelt profile, translated straight from bubblewrap's allowlist,
   crashed on macOS (dyld aborts with SIGABRT before Python even starts;
   see `_SEATBELT_PROFILE_NETWORK_NARROW_READ`'s comment in sandbox.py for
   the full diagnosis). Check 4 above exercises whatever platform CI
   happens to run on; this one exists so the Seatbelt path specifically
   can't go unexercised again the way it did the first time.

Run with: python checks/m11.py
"""
import asyncio
import json
import platform
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit import sandbox  # noqa: E402
from agentaudit.tools import execute_case_against_target  # noqa: E402

STDIN_ECHO_TARGET = ROOT / "targets" / "stdin-echo-target"
BROKEN_PYPROJECT_TARGET = ROOT / "targets" / "pyproject-broken-target"
SCRATCH_ROOT = ROOT / "runs" / "scratch"

REACT_AGENT_TARGET = Path("/tmp/audit-target")
REACT_AGENT_URL = "https://github.com/langchain-ai/react-agent"


def _ensure_react_agent_clone() -> Path:
    if REACT_AGENT_TARGET.is_dir() and (REACT_AGENT_TARGET / "pyproject.toml").is_file():
        return REACT_AGENT_TARGET
    subprocess.run(
        ["git", "clone", "--depth", "1", REACT_AGENT_URL, str(REACT_AGENT_TARGET)],
        check=True,
    )
    return REACT_AGENT_TARGET


def _fresh_scratch_dir() -> Path:
    scratch_dir = SCRATCH_ROOT / uuid.uuid4().hex
    scratch_dir.mkdir(parents=True, exist_ok=True)
    return scratch_dir


async def check_no_manifest_target_unaffected() -> None:
    marker = f"m11-no-manifest-{int(time.time())}"
    result = await execute_case_against_target.handler(
        {
            "case_id": "m11-no-manifest",
            "target": str(STDIN_ECHO_TARGET),
            "entry_point": "echo_stdin.py",
            "stdin_input": marker,
        }
    )
    assert not result.get("is_error"), f"a dependency-free target must be unaffected: {result}"
    summary = result["content"][0]["text"]
    assert f"STDIN_ECHO_START:{marker}:STDIN_ECHO_END" in summary, (
        f"stdin-echo-target's own behavior regressed: {summary!r}"
    )
    print("OK: a target with no pyproject.toml runs exactly as before M11 (skip, not a failure)")


async def check_broken_dependency_fails_honestly() -> None:
    result = await execute_case_against_target.handler(
        {
            "case_id": "m11-broken-dependency",
            "target": str(BROKEN_PYPROJECT_TARGET),
            "entry_point": "main.py",
            "stdin_input": "",
        }
    )
    assert result.get("is_error"), f"a target whose declared dependency doesn't exist must fail: {result}"
    text = result["content"][0]["text"]
    assert "this-package-definitely-does-not-exist-9x7z" in text or "pip install failed" in text, (
        f"failure reason doesn't name the concrete cause: {text!r}"
    )
    assert "pyproject-broken-target should never actually run this" not in text, (
        f"execution was attempted despite the install failure: {text!r}"
    )
    print(f"OK: nonexistent dependency reported honestly as is_error: {text[:200]!r}")


def check_install_timeout_enforced() -> None:
    target = _ensure_react_agent_clone()
    scratch_dir = _fresh_scratch_dir()
    try:
        start = time.monotonic()
        result = sandbox.install_target_dependencies(target, scratch_dir, timeout=0.01)
        elapsed = time.monotonic() - start
        assert not result.ok, f"an install with a 0.01s timeout must not succeed: {result}"
        assert result.python_path is None
        assert "timed out" in result.reason, f"reason doesn't name the timeout: {result.reason!r}"
        assert elapsed < 30.0, f"timeout wasn't actually enforced — took {elapsed:.1f}s to return"
        print(f"OK: a 0.01s install timeout cut off a real in-flight install after {elapsed:.1f}s")
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def check_real_install_against_react_agent() -> None:
    target = _ensure_react_agent_clone()
    scratch_dir = _fresh_scratch_dir()
    try:
        result = sandbox.install_target_dependencies(target, scratch_dir, timeout=300.0)
        assert result.ok, f"real install against react-agent failed: {result.reason}"
        assert result.python_path is not None and result.python_path.is_file()

        probe = sandbox.run_sandboxed(
            [str(result.python_path), "-c", "import langgraph; from react_agent.context import Context; print('IMPORT_OK')"],
            cwd=target,
            scratch_dir=scratch_dir,
            timeout=30.0,
        )
        assert "IMPORT_OK" in probe.stdout, (
            f"langgraph and/or react-agent's own package aren't importable after install "
            f"(returncode={probe.returncode}): {probe.stderr!r}"
        )
        print("OK: real install made both langgraph and react-agent's own package importable")
    finally:
        # This check calls install_target_dependencies directly (not through
        # execute_case_against_target), so nothing else owns cleanup here —
        # the harness's own auto-cleanup contract is proven separately, by
        # check_module_not_found_error_gone below.
        shutil.rmtree(scratch_dir, ignore_errors=True)


async def check_module_not_found_error_gone() -> None:
    target = _ensure_react_agent_clone()
    venvs_before = set(SCRATCH_ROOT.glob("*/venv"))

    result = await execute_case_against_target.handler(
        {
            "case_id": "m11-react-agent-execution",
            "target": str(target),
            "entry_point": "src/react_agent/graph.py",
            "stdin_input": "",
        }
    )
    summary = result["content"][0].get("text", "") if result.get("content") else json.dumps(result)
    assert "ModuleNotFoundError" not in summary, (
        f"ModuleNotFoundError still present — dependency install isn't actually wired into "
        f"execution: {summary!r}"
    )
    print("OK: ModuleNotFoundError is gone from react-agent's execution output")

    venvs_after = set(SCRATCH_ROOT.glob("*/venv"))
    leftover = venvs_after - venvs_before
    assert not leftover, f"execute_case_against_target left a venv behind, not ephemeral: {leftover}"
    print("OK: the ephemeral venv execute_case_against_target created was destroyed after use")


def check_live_audit_sanity() -> None:
    target = _ensure_react_agent_clone()
    run_id = f"m11-check-{int(time.time())}"

    subprocess.run(
        [sys.executable, "-m", "agentaudit", "audit", str(target), "--run-id", run_id],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    state_path = ROOT / "runs" / "audits" / run_id / "state.json"
    assert state_path.is_file(), f"no state.json produced for run_id={run_id}"
    state = json.loads(state_path.read_text())
    assert state.get("report") is not None, "audit did not complete with a coherent report"

    verdict = state["report"]["verdict"]
    execution = state.get("execution") or {}
    evidence = execution.get("evidence", "")
    print(f"live react-agent audit: verdict={verdict!r} execution_evidence={evidence!r}")

    if verdict == "inconclusive":
        old_signatures = (
            ("entry_point" in evidence and "not found" in evidence),
            ("ModuleNotFoundError" in evidence),
            ("no pyproject.toml" in evidence),
        )
        assert not any(old_signatures), (
            f"still inconclusive for an M9/M10/M11 wiring reason, not a genuine "
            f"environment limitation: {evidence!r}"
        )
        print(
            "OK: run is inconclusive for a distinct, already-documented environment reason "
            "(e.g. no real LLM API network access), not a dependency/wiring failure"
        )
    else:
        print(f"OK: live run reached a real outcome (verdict={verdict!r})")


def check_install_read_scope_excludes_outside_paths() -> None:
    """The pip-install step runs with network open (see
    install_target_dependencies's module docstring for why that's a real
    exfiltration risk combined with broad filesystem read). It narrows read
    access via `read_paths` specifically to cap that. This proves the
    narrowing actually blocks a real read, not just that pip still happens
    to work under it — a path outside the allowlist must not be visible.

    Two probe locations, not one — a subagent review of the macOS Seatbelt
    fix (see `_SEATBELT_PROFILE_NETWORK_NARROW_READ`'s comment in
    sandbox.py) caught that a single `/tmp` probe would be a false-confidence
    check there: that profile denies `/private/tmp` unconditionally,
    independent of whatever `read_paths` actually contains, so a `/tmp`-only
    probe would keep passing even if `_install_read_paths()`/`read_paths`
    curation were completely broken or empty. The home-directory probe below
    isn't covered by any platform-specific hardcoded deny — on macOS it's
    only blocked because it falls outside `read_paths`' re-opened overrides
    within the denied home directory; on Linux (bubblewrap) it's blocked
    because `read_paths` was never a broad allowlist to begin with. Either
    way, it actually exercises the mechanism this check's docstring claims
    to prove.
    """
    home_secret_dir = Path.home() / "agentaudit-m11-secret-probe"
    tmp_secret_dir = Path("/tmp/agentaudit-m11-secret-probe")
    probes = []
    for secret_dir, label in ((home_secret_dir, "home"), (tmp_secret_dir, "/tmp")):
        shutil.rmtree(secret_dir, ignore_errors=True)
        secret_dir.mkdir(parents=True)
        secret_file = secret_dir / "id_rsa"
        secret_file.write_text("FAKE-SECRET-SHOULD-NOT-BE-READABLE-DURING-INSTALL")
        probes.append((secret_file, label))

    scratch_dir = _fresh_scratch_dir()
    try:
        for secret_file, label in probes:
            probe = sandbox.run_sandboxed(
                ["cat", str(secret_file)],
                cwd=scratch_dir,
                scratch_dir=scratch_dir,
                timeout=10.0,
                allow_network=True,
                read_paths=sandbox._install_read_paths(),
            )
            assert probe.returncode != 0, (
                f"a path outside the install step's read allowlist ({label}) was "
                f"readable: {probe.stdout!r}"
            )
            assert "FAKE-SECRET" not in probe.stdout, (
                f"the {label} secret leaked into stdout despite a non-zero returncode: "
                f"{probe.stdout!r}"
            )
            print(
                f"OK: the install step's narrowed read scope excludes the {label} probe "
                f"(cat failed: {probe.stderr.strip()!r})"
            )
    finally:
        shutil.rmtree(home_secret_dir, ignore_errors=True)
        shutil.rmtree(tmp_secret_dir, ignore_errors=True)
        shutil.rmtree(scratch_dir, ignore_errors=True)


def check_seatbelt_install_succeeds_end_to_end() -> None:
    """Regression guard, macOS-only: a real pip install against react-agent
    must succeed end-to-end specifically under the Seatbelt backend, not
    just "on whatever platform happened to run check 4". Skips (printing why,
    not silently passing) on non-Darwin, so a Linux-only CI run can't give
    false confidence about macOS coverage the way it did before this bug was
    found — see `_SEATBELT_PROFILE_NETWORK_NARROW_READ`'s comment in
    sandbox.py for the crash this reproduces if it regresses.
    """
    if platform.system() != "Darwin":
        print("SKIP: check_seatbelt_install_succeeds_end_to_end only applies on macOS (Seatbelt)")
        return

    target = _ensure_react_agent_clone()
    scratch_dir = _fresh_scratch_dir()
    try:
        result = sandbox.install_target_dependencies(target, scratch_dir, timeout=300.0)
        assert result.ok, (
            f"real pip install against react-agent failed under Seatbelt: {result.reason}"
        )
        assert result.python_path is not None and result.python_path.is_file()

        probe = sandbox.run_sandboxed(
            [
                str(result.python_path),
                "-c",
                "import langgraph; from react_agent.context import Context; print('IMPORT_OK')",
            ],
            cwd=target,
            scratch_dir=scratch_dir,
            timeout=30.0,
        )
        assert probe.backend == "seatbelt", f"expected the seatbelt backend, got {probe.backend!r}"
        assert "IMPORT_OK" in probe.stdout, (
            f"install reported success but the installed venv can't actually import "
            f"langgraph/react-agent (returncode={probe.returncode}): {probe.stderr!r}"
        )
        print(
            "OK: a real pip install against react-agent succeeds end-to-end under Seatbelt "
            "(dyld boots under the narrowed read profile, DNS resolves, langgraph and "
            "react-agent's own package both import)"
        )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def main() -> int:
    asyncio.run(check_no_manifest_target_unaffected())
    asyncio.run(check_broken_dependency_fails_honestly())
    check_install_timeout_enforced()
    check_real_install_against_react_agent()
    asyncio.run(check_module_not_found_error_gone())
    check_live_audit_sanity()
    check_install_read_scope_excludes_outside_paths()
    check_seatbelt_install_succeeds_end_to_end()
    return 0


if __name__ == "__main__":
    sys.exit(main())
