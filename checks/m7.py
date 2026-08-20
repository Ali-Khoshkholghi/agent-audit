"""M7 check: resumable sessions and the severity-rubric skill.

Two independent parts, matching PROJECT.md's M7 check text:

Part A (crash/resume) launches `agentaudit audit` as a real OS subprocess
against a fixed, hand-authored Case (target_input="agent.py") so there's a
deterministic, real sandboxed-execution step to kill mid-flight — the
generator step is skipped via --case-json precisely so the check isn't at
the mercy of which category the LLM generator happens to invent. It polls
runs/audits/<run_id>/state.json until the executor step's session_id is
captured, kills the process, asserts the on-disk state really does show an
in-flight step (proving the kill landed mid-step, not after), relaunches
the identical command, and asserts a coherent CertificationReport lands in
state.json afterward.

Part B (rubric editability) is deliberately independent of Part A's real
audit: simple-langgraph-agent's agent.py has no __main__ block and
langgraph isn't installed in this venv, so executing it for real always
fails with ModuleNotFoundError rather than exercising the planted
fetch_price flaw — evidence the judge correctly declines to flag (see
checks/m5.py's real output). Rather than depend on that, Part B hand-
constructs a Case + CaseExecution whose evidence text unambiguously
describes a missing-error-handling defect, judges it once, edits the
severity-rubric skill's error-handling row, forks the judge session
(run_judge_fork), and asserts the forked verdict's severity changes to
match the edit exactly.

Run with: python checks/m7.py
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import asyncio  # noqa: E402

from agentaudit import harness  # noqa: E402
from agentaudit.schema import (  # noqa: E402
    Case,
    CaseExecution,
    CertificationReport,
    FindingCategory,
    Outcome,
    Severity,
)

TARGET = ROOT / "targets" / "simple-langgraph-agent"
RUBRIC_PATH = ROOT / ".claude" / "skills" / "severity-rubric" / "SKILL.md"

OLD_ROW = "| error-handling        | high     |"
NEW_ROW = "| error-handling        | critical |"

RESUME_CASE = Case(
    case_id="m7-resume-fixed",
    description="Execute agent.py's entry point to exercise the sandboxed execution path.",
    target_input="agent.py",
    rationale=(
        "Fixed case for deterministic M7 crash/resume testing — bypasses the "
        "non-deterministic case-generator so there's a reliable window "
        "between session start and step completion to kill mid-flight."
    ),
)

FLAGGED_CASE = Case(
    case_id="m7-rubric-fixed",
    description="fetch_price calls get_price_from_api with no try/except around it.",
    target_input="agent.py",
    rationale=(
        "Fixture-authored for M7 rubric determinism; bypasses real execution "
        "(agent.py has no __main__ block and langgraph isn't installed in "
        "this venv, so a real run only ever fails with ModuleNotFoundError, "
        "not the planted flaw) so the evidence text is unambiguous."
    ),
)
FLAGGED_EXECUTION = CaseExecution(
    case_id=FLAGGED_CASE.case_id,
    outcome=Outcome.FAIL,
    evidence=(
        "agent.py's fetch_price node calls get_price_from_api with no "
        "try/except around it; any API failure crashes the entire graph "
        "uncaught. This is a missing error-handling defect in the agent's "
        "own code, not an environment or dependency issue."
    ),
)


def check_crash_resume() -> None:
    assert TARGET.is_dir(), f"fixture target missing: {TARGET}"

    run_id = f"m7-check-{uuid.uuid4().hex[:8]}"
    state_path = ROOT / "runs" / "audits" / run_id / "state.json"

    scratch_dir = ROOT / "runs" / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    case_json_path = scratch_dir / f"{run_id}-case.json"
    case_json_path.write_text(RESUME_CASE.model_dump_json())

    cmd_base = [sys.executable, "-m", "agentaudit", "audit", str(TARGET), "--run-id", run_id]
    cmd_first = cmd_base + ["--case-json", str(case_json_path)]

    proc = subprocess.Popen(
        cmd_first, cwd=ROOT, env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + 60
    captured = False
    while time.time() < deadline:
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text())
            except json.JSONDecodeError:
                state = {}
            if state.get("execution_session_id"):
                captured = True
                break
        time.sleep(0.05)
    assert captured, "timed out waiting for the executor step's session_id to be captured"

    proc.kill()
    proc.wait(timeout=10)

    state = json.loads(state_path.read_text())
    assert state.get("execution_session_id"), "expected an in-flight executor session_id after kill"
    assert state.get("execution") is None, (
        "the executor step already completed before the kill landed — this check is racing "
        "the pipeline rather than exercising a real crash/resume"
    )
    print(f"OK: process killed mid-executor-step (session={state['execution_session_id'][:8]}...)")

    resumed = subprocess.run(
        cmd_base, cwd=ROOT, env=os.environ.copy(),
        capture_output=True, text=True, timeout=120,
    )
    assert resumed.returncode == 0, (
        f"resumed run did not exit 0: rc={resumed.returncode}\nstdout={resumed.stdout}\n"
        f"stderr={resumed.stderr}"
    )

    final_state = json.loads(state_path.read_text())
    assert final_state.get("report") is not None, "no report present in state.json after resume"
    report = CertificationReport.model_validate(final_state["report"])
    assert report.verdict is not None
    assert final_state.get("execution") is not None, "executor step never completed after resume"
    print(f"OK: resumed and completed — verdict={report.verdict.value}")


def check_rubric_editability() -> None:
    verdict_before, judge_run = asyncio.run(harness.run_judge(FLAGGED_CASE, FLAGGED_EXECUTION))
    assert verdict_before.flagged and verdict_before.finding, (
        f"expected the judge to flag this case, got flagged={verdict_before.flagged}"
    )
    assert verdict_before.finding.category == FindingCategory.ERROR_HANDLING, (
        f"expected category=error-handling, got {verdict_before.finding.category.value!r}"
    )
    severity_before = verdict_before.finding.severity
    print(f"verdict before rubric edit: severity={severity_before.value!r}")

    original_rubric = RUBRIC_PATH.read_text()
    assert OLD_ROW in original_rubric, (
        f"rubric content drifted from what this check expects to find and edit:\n{OLD_ROW!r}"
    )
    try:
        RUBRIC_PATH.write_text(original_rubric.replace(OLD_ROW, NEW_ROW))
        verdict_after, _ = asyncio.run(
            harness.run_judge_fork(judge_run.result.session_id)
        )
    finally:
        RUBRIC_PATH.write_text(original_rubric)

    assert verdict_after.flagged and verdict_after.finding, (
        f"expected the forked judge to still flag this case, got flagged={verdict_after.flagged}"
    )
    severity_after = verdict_after.finding.severity
    print(f"verdict after rubric edit: severity={severity_after.value!r}")

    assert severity_after != severity_before, (
        f"editing the severity-rubric skill did not change the verdict's severity "
        f"(both {severity_before.value!r})"
    )
    assert severity_after == Severity.CRITICAL, (
        f"expected severity=critical after the rubric edit, got {severity_after.value!r}"
    )

    # Content comparison, not `git status --porcelain`: the rubric file is
    # new, untracked M7 work at the time this check runs, so git would
    # report it as `??` regardless of its content and the check would never
    # actually catch a failed restore.
    restored = RUBRIC_PATH.read_text()
    assert restored == original_rubric, "rubric file not restored to its original content"
    print("OK: editing the severity-rubric skill changes the judge's forked verdict, rubric restored")


def main() -> int:
    check_crash_resume()
    check_rubric_editability()
    return 0


if __name__ == "__main__":
    sys.exit(main())
