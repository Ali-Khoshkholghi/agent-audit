"""M3 check: the in-process MCP tool server (`load_target_spec`,
`execute_case_against_target`, `record_result`) is wired up correctly and is
the *only* path Claude has to complete a case.

This is a scripted run: the target's spec declares an `entry_point` value
that isn't given to Claude anywhere in the prompt, and `execute_case_against_
target` is stubbed to always report outcome="inconclusive" — a value that
also isn't given anywhere. The only way to produce correct arguments to
`record_result` is to have actually called the first two tools and read
their results. We assert:

  - all three tools were called, and *only* those three (no built-ins are
    available — `run_case` sets `tools=[]`)
  - they were called in order: load_target_spec, execute_case_against_target,
    record_result
  - each call's arguments are valid and actually derived from the previous
    tool's output, not guessed
  - `record_result`'s side effect (the case ledger) really happened, not
    just that the model claimed it would

Run with: python checks/m3.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit import harness  # noqa: E402

CASE_LEDGER = ROOT / "runs" / "cases.jsonl"

EXPECTED_TOOL_ORDER = [
    "mcp__agentaudit__load_target_spec",
    "mcp__agentaudit__execute_case_against_target",
    "mcp__agentaudit__record_result",
]

# Distinctive value planted only in the target's spec file. Never mentioned
# in the prompt — the only way it ends up as an argument to
# execute_case_against_target is if Claude actually called load_target_spec
# and read the response.
ENTRY_POINT = "run_case_target_v3"

CASE_ID = "case-m3-001"


def read_ledger() -> list[dict]:
    if not CASE_LEDGER.exists():
        return []
    lines = [line for line in CASE_LEDGER.read_text().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def make_target(tmp: Path) -> Path:
    target = tmp / "stub-target"
    target.mkdir()
    spec = {
        "framework": "stub-for-m3-check",
        "entry_point": ENTRY_POINT,
        "declared_tools": [ENTRY_POINT],
    }
    (target / "agentaudit.spec.json").write_text(json.dumps(spec))
    return target


def check_tool_order_and_args(target: Path, run: "harness.CaseRun") -> None:
    calls = run.tool_calls
    names = [c.name for c in calls]
    print("--- tool calls ---")
    for c in calls:
        print(f"  {c.name}({c.input})")

    assert names == EXPECTED_TOOL_ORDER, (
        f"tool call sequence was {names}, expected exactly {EXPECTED_TOOL_ORDER} "
        f"(no built-ins should be reachable and no tool should run twice)"
    )

    load_call, exec_call, record_call = calls

    assert load_call.input.get("path") == str(target), (
        f"load_target_spec called with path={load_call.input.get('path')!r}, "
        f"expected {str(target)!r}"
    )

    assert exec_call.input.get("case_id") == CASE_ID, (
        f"execute_case_against_target case_id={exec_call.input.get('case_id')!r}, "
        f"expected {CASE_ID!r}"
    )
    exec_input = exec_call.input.get("input", "")
    assert ENTRY_POINT in exec_input, (
        f"execute_case_against_target input={exec_input!r} does not contain the "
        f"entry_point {ENTRY_POINT!r} declared in the spec — Claude didn't use "
        f"load_target_spec's result"
    )

    assert record_call.input.get("case_id") == CASE_ID, (
        f"record_result case_id={record_call.input.get('case_id')!r}, expected {CASE_ID!r}"
    )
    outcome = record_call.input.get("outcome")
    assert outcome == "inconclusive", (
        f"record_result outcome={outcome!r}, expected 'inconclusive' (the only "
        f"value execute_case_against_target's stub ever reports) — a different "
        f"value means Claude guessed instead of relaying the tool's result"
    )
    evidence = record_call.input.get("evidence", "")
    assert CASE_ID in evidence, f"record_result evidence does not reference {CASE_ID!r}: {evidence!r}"

    print("OK: tools called in order with valid, genuinely-derived arguments")


def check_ledger_side_effect(before: int) -> None:
    records = read_ledger()
    assert len(records) > before, f"no new record appended to {CASE_LEDGER}"
    record = records[-1]
    assert record.get("case_id") == CASE_ID, f"ledger record case_id mismatch: {record}"
    assert record.get("outcome") == "inconclusive", f"ledger record outcome mismatch: {record}"
    print(f"OK: record_result actually appended to the ledger: {record}")


def main() -> int:
    before = len(read_ledger())

    with tempfile.TemporaryDirectory() as tmp:
        target = make_target(Path(tmp))
        run = asyncio.run(harness.run_case(target, CASE_ID))

        print(
            f"[session={run.result.session_id} turns={run.result.num_turns} "
            f"subtype={run.result.subtype!r}]"
        )
        assert run.result.subtype == "success", (
            f"case run did not complete successfully: subtype={run.result.subtype!r}"
        )

        check_tool_order_and_args(target, run)

    check_ledger_side_effect(before)
    return 0


if __name__ == "__main__":
    sys.exit(main())
