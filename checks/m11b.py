"""M11b check: deterministic fail-vs-inconclusive override in run_case_executor.

PROJECT.md's M5 section flags a known gap: whether a case's outcome is
"inconclusive" (never actually executed) vs "fail" (executed and revealed
the problem) was judgment-based — CASE_EXECUTOR_AGENT_PROMPT told the
case-executor subagent how to tell the two apart, but nothing enforced it
deterministically. Confirmed live: the model_name_format_validation_missing
case against react-agent was labeled "fail" even though execution reported
returncode=0 with empty stdout/stderr (react-agent's graph.py has no
__main__/stdin-reading code, per M10 — nothing was ever actually exercised).

Fix: execute_case_against_target now writes a second, purely mechanical
ledger (EXECUTION_LEDGER_PATH / "executions.jsonl") of the raw SandboxResult
fields alongside the case-executor's own free-text evidence in cases.jsonl.
run_case_executor reads it back and deterministically overrides outcome to
"inconclusive" whenever it's "fail" or "pass" but the raw record shows a
clean exit (returncode=0, no timeout) with literally no stdout and no
stderr — regardless of what the subagent itself concluded. cases.jsonl
itself is never rewritten; only the CaseExecution returned to the rest of
the pipeline (run_judge, _assemble_report) is corrected.

Two checks:

1. A deterministic unit check on _is_structurally_silent itself (no LLM, no
   variance): a real execution record from targets/silent-target/ (which
   does nothing but `pass`) must read as silent; a non-zero returncode, a
   timeout, non-empty stdout, and non-empty stderr must each NOT read as
   silent — pins the exact boundary the rule draws.
2. A live end-to-end proof: a hand-built Case (bypassing the
   non-deterministic case-generator, same pattern as checks/m7.py) run for
   real through run_case_executor against targets/silent-target/. Asserts
   the returned CaseExecution.outcome is deterministically "inconclusive"
   regardless of whatever the real subagent concluded, and that cases.jsonl
   itself was not rewritten with the harness's override text.

Run with: python checks/m11b.py
"""
import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit import harness  # noqa: E402
from agentaudit.schema import Case, Outcome  # noqa: E402
from agentaudit.tools import execute_case_against_target  # noqa: E402

SILENT_TARGET = ROOT / "targets" / "silent-target"


async def check_is_structurally_silent_boundary() -> None:
    assert SILENT_TARGET.is_dir(), f"fixture missing: {SILENT_TARGET}"
    case_id = f"m11b-boundary-{uuid.uuid4().hex[:8]}"

    result = await execute_case_against_target.handler(
        {
            "case_id": case_id,
            "target": str(SILENT_TARGET),
            "entry_point": "silent.py",
            "stdin_input": "anything, ignored",
        }
    )
    assert not result.get("is_error"), f"silent-target execution unexpectedly failed: {result}"

    exec_record = harness._read_last_execution_record(case_id)
    assert exec_record is not None, "execute_case_against_target didn't write an execution record"
    assert exec_record["returncode"] == 0
    assert exec_record["stdout"] == "" and exec_record["stderr"] == ""
    assert harness._is_structurally_silent(exec_record), (
        f"a genuinely silent execution must read as structurally silent: {exec_record}"
    )
    print(f"OK: real silent-target execution reads as structurally silent: {exec_record}")

    non_silent_cases = {
        "non-zero returncode, no output": {
            "returncode": 1, "timed_out": False, "stdout": "", "stderr": "",
        },
        "timed out": {
            "returncode": 0, "timed_out": True, "stdout": "", "stderr": "",
        },
        "non-empty stdout": {
            "returncode": 0, "timed_out": False, "stdout": "hello", "stderr": "",
        },
        "non-empty stderr": {
            "returncode": 0, "timed_out": False, "stdout": "", "stderr": "warning: x",
        },
        "no record at all": None,
    }
    for label, record in non_silent_cases.items():
        assert not harness._is_structurally_silent(record), (
            f"{label!r} must NOT read as structurally silent: {record}"
        )
    print("OK: non-zero returncode, timeout, non-empty stdout/stderr, and a missing record all correctly read as NOT silent")


async def check_live_override_forces_inconclusive() -> None:
    case = Case(
        case_id=f"m11b-silent-fail-{uuid.uuid4().hex[:8]}",
        description=(
            "The target should print an error when given malformed input, but "
            "silently accepts anything without validation or feedback."
        ),
        target_input="malformed-input-that-should-be-rejected",
        rationale=(
            "Framed to plausibly read as a 'fail' case to a case-executor — the "
            "point is that no matter what it concludes, a structurally silent "
            "execution (returncode=0, no stdout, no stderr) must never survive "
            "as 'fail' or 'pass' in the CaseExecution the harness actually uses."
        ),
    )

    execution, _run = await harness.run_case_executor(SILENT_TARGET, case)

    assert execution.outcome == Outcome.INCONCLUSIVE, (
        f"a structurally silent execution must be forced to inconclusive "
        f"regardless of the subagent's own guess, got outcome={execution.outcome!r} "
        f"evidence={execution.evidence!r}"
    )

    ledger_record = harness._read_last_case_record(case.case_id)
    assert ledger_record is not None, "case-executor didn't write a cases.jsonl record at all"

    # The case's framing is deliberately biased toward "fail" so the
    # subagent actually mislabels it — otherwise this check could pass
    # trivially (outcome already "inconclusive" on its own) without ever
    # exercising the override at all. Assert that precondition explicitly,
    # not just the end state, so this check can't silently stop proving
    # anything if a future model happens to get it right unassisted.
    assert ledger_record["outcome"] in ("fail", "pass"), (
        f"expected the case-executor's own raw label to need correcting "
        f"(fail/pass) so this check actually exercises the override — got "
        f"outcome={ledger_record['outcome']!r} already, which doesn't prove "
        f"the override fired: {ledger_record}"
    )
    assert "Overridden by the harness" in execution.evidence, (
        f"the override must leave a fingerprint in the returned evidence "
        f"proving it actually intervened, not just that the final outcome "
        f"happens to match: {execution.evidence!r}"
    )
    print(
        f"OK: CaseExecution.outcome deterministically overridden to inconclusive "
        f"(raw label was {ledger_record['outcome']!r}): {execution.evidence!r}"
    )

    assert "Overridden by the harness" not in ledger_record["evidence"], (
        f"cases.jsonl must never be rewritten with the harness's override text — "
        f"forward-looking only, no history rewrite: {ledger_record}"
    )
    print(
        f"OK: cases.jsonl's own record is untouched (outcome={ledger_record['outcome']!r}, "
        f"the subagent's original, unoverridden conclusion)"
    )


def main() -> int:
    asyncio.run(check_is_structurally_silent_boundary())
    asyncio.run(check_live_override_forces_inconclusive())
    return 0


if __name__ == "__main__":
    sys.exit(main())
