"""M10 check: wiring a case's target_input into actual execution via stdin.

Before M10, execute_case_against_target's single `input` parameter was
overloaded to mean "the entry_point filename" — but EXECUTOR_INVOKE_PROMPT
handed the case-executor subagent case.target_input (the adversarial
payload) framed as "the input" for that same tool call. Which meaning won
was model-judgment-dependent: one live run against react-agent had the
executor pass the payload string directly as the tool's `input`, correctly
refused as a nonexistent file path; another had it guess filenames instead.
Either way, target_input never reached the target.

M10 splits that one overloaded field into two: `entry_point` (from
load_target_spec, never guessed) and `stdin_input` (the case's literal
input, piped to the subprocess's stdin).

Three checks:

1. Mechanism proof, against a new purpose-built fixture
   (targets/stdin-echo-target/) whose entry_point reads stdin and echoes it
   back in a stable marker line — proves stdin genuinely reaches a running
   target's own logic through the real OS sandbox, not just that our
   Python called subprocess.run(input=...).
2. Wiring correctness against the real, live react-agent repo
   (/tmp/audit-target): the right entry_point (M9's discovery) is used and
   the right byte count is piped, without asserting a specific pass/fail
   outcome — react-agent's own graph.py has no __main__/stdin-reading code
   at all, and even if it did, langgraph/langchain_openai aren't installed
   in .venv and graph.ainvoke() needs real network access, which M6's
   sandbox denies. A real pass/fail on the actual planted flaw is
   structurally unreachable here; what M10 can and does prove is that the
   plumbing itself is correct.
3. A live end-to-end `agentaudit audit` sanity run against react-agent:
   asserts a coherent report either way, and that IF it's inconclusive,
   the evidence no longer carries the old "entry_point ... not found"
   failure signature this milestone specifically targets.

Run with: python checks/m10.py
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit.tools import execute_case_against_target, load_target_spec  # noqa: E402

STDIN_ECHO_TARGET = ROOT / "targets" / "stdin-echo-target"

REACT_AGENT_TARGET = Path("/tmp/audit-target")
REACT_AGENT_URL = "https://github.com/langchain-ai/react-agent"


def _ensure_react_agent_clone() -> Path:
    if REACT_AGENT_TARGET.is_dir() and (REACT_AGENT_TARGET / "langgraph.json").is_file():
        return REACT_AGENT_TARGET
    subprocess.run(
        ["git", "clone", "--depth", "1", REACT_AGENT_URL, str(REACT_AGENT_TARGET)],
        check=True,
    )
    return REACT_AGENT_TARGET


async def check_stdin_reaches_running_target() -> None:
    assert STDIN_ECHO_TARGET.is_dir(), f"fixture missing: {STDIN_ECHO_TARGET}"
    marker = f"m10-stdin-proof-{int(time.time())}"

    result = await execute_case_against_target.handler(
        {
            "case_id": "m10-mechanism-proof",
            "target": str(STDIN_ECHO_TARGET),
            "entry_point": "echo_stdin.py",
            "stdin_input": marker,
        }
    )
    assert not result.get("is_error"), f"execution failed: {result}"
    summary = result["content"][0]["text"]
    assert f"stdin_bytes={len(marker.encode())}" in summary, (
        f"summary doesn't report the correct stdin byte count: {summary!r}"
    )
    assert f"STDIN_ECHO_START:{marker}:STDIN_ECHO_END" in summary, (
        f"the target's own echo of stdin doesn't contain what was sent — stdin "
        f"did not reach the running target's logic: {summary!r}"
    )
    print(f"OK: stdin_input {marker!r} reached the running sandboxed target and was echoed back")


async def check_react_agent_wiring() -> None:
    target = _ensure_react_agent_clone()

    spec_result = await load_target_spec.handler({"path": str(target)})
    assert not spec_result.get("is_error"), f"discovery failed: {spec_result}"
    discovered = json.loads(spec_result["content"][0]["text"])
    entry_point = discovered["entry_point"]
    assert entry_point == "src/react_agent/graph.py", (
        f"expected M9's discovery to find src/react_agent/graph.py, got {entry_point!r}"
    )

    stdin_payload = "{'messages': [('user', 'what is the capital of France?')]}"
    result = await execute_case_against_target.handler(
        {
            "case_id": "m10-react-agent-wiring",
            "target": str(target),
            "entry_point": entry_point,
            "stdin_input": stdin_payload,
        }
    )
    summary = result["content"][0]["text"]

    # Not asserting is_error / returncode / a specific outcome here: even
    # with correct wiring, graph.py has no code that reads stdin, and even
    # if it did, langgraph/langchain_openai aren't installed in .venv and
    # graph.ainvoke() needs real network access, which M6's sandbox denies.
    # What's actually being proven is that the plumbing is correct, not
    # that react-agent's planted flaw gets exercised end to end.
    assert f"entry_point={entry_point!r}" in summary, (
        f"execute_case_against_target didn't use the discovered entry_point: {summary!r}"
    )
    assert f"stdin_bytes={len(stdin_payload.encode())}" in summary, (
        f"summary doesn't report the correct stdin byte count: {summary!r}"
    )
    assert "not found at" not in summary, (
        f"execution still couldn't find the entry_point — M9's discovery and M10's "
        f"wiring aren't actually connected: {summary!r}"
    )
    print(
        f"OK: real execution attempted against react-agent's discovered entry_point "
        f"{entry_point!r} with {len(stdin_payload.encode())} bytes correctly piped to stdin"
    )


def check_live_audit_sanity() -> None:
    target = _ensure_react_agent_clone()
    run_id = f"m10-check-{int(time.time())}"

    proc = subprocess.run(
        [sys.executable, "-m", "agentaudit", "audit", str(target), "--run-id", run_id],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
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
        old_failure_signature = "entry_point" in evidence and "not found" in evidence
        assert not old_failure_signature, (
            f"still inconclusive for the OLD reason (entry_point not found) — M9/M10 "
            f"discovery+wiring regressed: {evidence!r}"
        )
        print(
            "OK: run is inconclusive for an environment reason (missing deps / no "
            "network), not the old entry_point-not-found failure this milestone fixes"
        )
    else:
        print(f"OK: live run reached a real outcome (verdict={verdict!r})")


def main() -> int:
    asyncio.run(check_stdin_reaches_running_target())
    asyncio.run(check_react_agent_wiring())
    check_live_audit_sanity()
    return 0


if __name__ == "__main__":
    sys.exit(main())
