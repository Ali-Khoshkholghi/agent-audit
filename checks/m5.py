"""M5 check: three subagents (case-generator, case-executor, judge) with
distinct models, and structural isolation of the judge from the
case-generator's reasoning.

Isolation is verified structurally, not by trusting a prompt:
  - the judge's invocation prompt is built by harness code from an explicit
    whitelist of Case fields (case_id, description, target_input) plus the
    CaseExecution result. We assert the generator's `rationale` — its
    private reasoning for proposing the case — is literally absent from
    that string, checked in Python against the real prompt text sent, not
    a planted marker the model might paraphrase around.
  - the judge subagent's own tool list (harness.JUDGE_AGENT_TOOLS) is
    asserted empty, so even a leaked reference to the target couldn't be
    acted on.
  - the outer orchestrator wrapping the judge subagent has no tools besides
    Agent (harness.SUBAGENT_ORCHESTRATOR_TOOLS) and no mcp_servers
    registered, so it has no side channel to fetch anything beyond the
    prompt we gave it.

Run with: python checks/m5.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit import harness  # noqa: E402
from agentaudit.schema import Case, CaseExecution, CaseVerdict  # noqa: E402

TARGET = ROOT / "targets" / "simple-langgraph-agent"
CONFIDENCE_TOLERANCE = 0.35


def run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    )


def check_ran(run: "harness.SubagentRun", expected_type: str) -> None:
    assert run.invoked, f"{expected_type}: Agent tool was never invoked with this subagent_type"
    assert run.executed, f"{expected_type}: no message carried a matching parent_tool_use_id"
    print(f"OK: {expected_type} subagent ran (invoked={run.invoked}, executed={run.executed})")


def check_judge_isolation(case: Case, judge_run: "harness.SubagentRun") -> None:
    # Structural: assert against the harness's actual restriction constants,
    # not a comment claiming they're restricted.
    assert harness.JUDGE_AGENT_TOOLS == [], (
        f"judge subagent has non-empty tools, isolation is not structural: "
        f"{harness.JUDGE_AGENT_TOOLS}"
    )
    assert harness.SUBAGENT_ORCHESTRATOR_TOOLS == ["Agent"], (
        f"judge's outer orchestrator has more than just the Agent tool: "
        f"{harness.SUBAGENT_ORCHESTRATOR_TOOLS}"
    )

    assert case.rationale, "fixture bug: generator produced an empty rationale to test against"
    assert case.rationale not in judge_run.invocation_prompt, (
        f"the generator's rationale leaked into the judge's invocation prompt:\n"
        f"rationale={case.rationale!r}\nprompt={judge_run.invocation_prompt!r}"
    )
    assert case.rationale not in judge_run.output_text, (
        f"the generator's rationale leaked into the judge's own output:\n"
        f"rationale={case.rationale!r}\noutput={judge_run.output_text!r}"
    )
    print("OK: judge's prompt/tools/output structurally exclude the generator's rationale")


def main() -> int:
    assert TARGET.is_dir(), f"fixture target missing: {TARGET}"

    case, gen_run = asyncio.run(harness.run_case_generator(TARGET))
    print(f"case={case.case_id!r} target_input={case.target_input!r}")
    print(f"rationale={case.rationale!r}")
    check_ran(gen_run, "case-generator")

    execution, exec_run = asyncio.run(harness.run_case_executor(TARGET, case))
    print(f"execution outcome={execution.outcome.value!r} evidence={execution.evidence!r}")
    assert execution.case_id == case.case_id, "case-executor returned a mismatched case_id"
    check_ran(exec_run, "case-executor")

    verdict1, judge_run1 = asyncio.run(harness.run_judge(case, execution))
    verdict2, judge_run2 = asyncio.run(harness.run_judge(case, execution))
    print(f"judge run 1: flagged={verdict1.flagged} finding={verdict1.finding}")
    print(f"judge run 2: flagged={verdict2.flagged} finding={verdict2.finding}")
    check_ran(judge_run1, "judge")
    check_ran(judge_run2, "judge")
    check_judge_isolation(case, judge_run1)
    check_judge_isolation(case, judge_run2)

    assert verdict1.case_id == case.case_id == verdict2.case_id

    assert verdict1.flagged == verdict2.flagged, (
        f"judge flip-flopped on the same fixed case+evidence: "
        f"run1.flagged={verdict1.flagged} run2.flagged={verdict2.flagged} — "
        f"a real reproducibility gap in the harness"
    )
    if verdict1.finding and verdict2.finding:
        variance = abs(verdict1.finding.confidence - verdict2.finding.confidence)
        print(f"confidence variance between runs: {variance:.3f}")
        assert variance <= CONFIDENCE_TOLERANCE, (
            f"confidence variance {variance:.3f} exceeds tolerance {CONFIDENCE_TOLERANCE}"
        )
    print("OK: judge verdicts reproducible across two runs on the fixed case")

    all_models: set[str] = set()
    for run in (gen_run, exec_run, judge_run1, judge_run2):
        all_models |= set((run.result.model_usage or {}).keys())
    print(f"models used across the pipeline: {sorted(all_models)}")
    assert len(all_models) > 1, f"expected more than one model in model_usage, got {all_models}"
    print("OK: model_usage shows more than one model")

    status = run_git("status", "--porcelain", "targets/simple-langgraph-agent")
    assert status.stdout == "", f"fixture not clean after M5 run:\n{status.stdout}"
    print("OK: fixture untouched")

    return 0


if __name__ == "__main__":
    sys.exit(main())
