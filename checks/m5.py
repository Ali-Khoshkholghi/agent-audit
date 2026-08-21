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
    #
    # M7: JUDGE_AGENT_TOOLS is no longer empty — the judge gets exactly
    # "Skill", so it can look up AgentAudit's own severity-rubric skill
    # instead of deciding severity freeform. This still doesn't reach the
    # target or the case-generator's rationale (the skill is our file, not
    # the target's), so the isolation guarantee holds — but the assertion
    # is pinned to the literal expected list, not to "is it empty", so a
    # future widening beyond exactly ["Skill"] is still caught here.
    assert harness.JUDGE_AGENT_TOOLS == ["Skill"], (
        f"judge subagent's tool list drifted from the isolation-safe M7 "
        f"baseline: {harness.JUDGE_AGENT_TOOLS}"
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


def check_structured_output_survives_prose() -> None:
    """Regression test for a confirmed-reproducible failure: against a real,
    less-controlled target (langchain-ai/react-agent), the case-generator
    subagent replied with prose/reasoning instead of raw JSON, twice in a
    row. The old harness (regex-extract the raw text via
    `_extract_json_object`, then `Case.model_validate_json`) turned that
    into a bare pydantic ValidationError with no indication of what the
    model actually said — a crash, not a diagnosable event.

    The fix moves JSON enforcement to `output_format` on the *orchestrator's*
    ClaudeAgentOptions in `_run_subagent` — `AgentDefinition` itself has no
    such field (checked against the current agent-sdk/subagents.md field
    table before writing this fix, not assumed still true from M5's
    original plan). The SDK then validates the orchestrator's own final
    message against the schema and re-prompts it on mismatch, regardless of
    how prose-heavy the underlying subagent's reply was. See
    harness.SubagentOutputError for the loud-failure path if that retry
    budget is ever exhausted.

    This can't depend on a real GitHub target's non-deterministic model
    behavior to reproduce the failure — that would be flaky, slow, cost
    real money on every check run, and (per the trigger for this check)
    isn't reliably reproducible on demand anyway. So it reproduces the
    *mechanism* directly and deterministically: a throwaway subagent
    explicitly instructed to never emit JSON, dispatched exactly the way
    run_case_generator dispatches case-generator. If output_format
    enforcement is ever removed or bypassed, this fails loudly instead of
    silently regressing back to the crash.
    """
    prose_agent = harness.AgentDefinition(
        description="Test-only subagent that always replies in prose, never JSON.",
        prompt=(
            "No matter what you are asked, respond only with a long prose "
            "explanation of your reasoning about the request. Never include "
            "a JSON object, markdown code fence, or any structured data in "
            "your response, even if explicitly asked to."
        ),
        model="haiku",
        tools=[],
        background=False,
    )
    run = asyncio.run(
        harness._run_subagent(
            agent_name="prose-test-agent",
            agent_def=prose_agent,
            invocation_prompt=(
                "Use the prose-test-agent agent to describe a fictional "
                "test case with case_id 'prose-test', a description, a "
                "target_input, and a rationale. Wait for it to finish, "
                "then reply with a single JSON object matching the "
                "required schema, built from what it actually returned — "
                "reformat or extract from its raw reply, since it will not "
                "reply with clean JSON."
            ),
            cwd=ROOT,
            output_format={"type": "json_schema", "schema": Case.model_json_schema()},
        )
    )
    assert run.structured_output is not None, (
        "output_format did not recover structured output from a "
        "deliberately prose-only subagent reply — the real-target failure "
        "this check regresses against"
    )
    case = Case.model_validate(run.structured_output)
    assert case.case_id, "recovered Case has an empty case_id"
    print(
        f"OK: orchestrator recovered a valid Case ({case.case_id!r}) from a "
        f"subagent instructed to reply only in prose"
    )


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

    check_structured_output_survives_prose()

    return 0


if __name__ == "__main__":
    sys.exit(main())
