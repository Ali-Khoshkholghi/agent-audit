"""M6 check: permissions and hooks around real (sandboxed) case execution.

Runs `harness.run_case` against `targets/network-probe-target/`, whose
entry_point (`probe.py`) attempts one outbound HTTP call and one write to
`/etc/agentaudit-sandbox-test-marker`. We assert:

  - both attempts were blocked, per the case ledger's evidence for this
    case (which carries the probe's own stable `SANDBOX_RESULT: ...` line
    — not an LLM's paraphrase of what happened)
  - the /etc write was blocked for real: the marker file does not exist on
    the actual filesystem afterward. This is the ground-truth check —
    everything else here is the harness's own report of what happened,
    this is independent proof.
  - the audit ledger (written unconditionally by run_case's PreToolUse
    hook) has exactly as many new records as the message stream has tool
    calls. Any gap would mean a tool call ran unlogged.

Network blocking is corroborated but not solely relied on: a blocked DNS
lookup and a genuinely offline machine can look identical from outside the
sandbox, so the hard proof here is the filesystem check above plus the
sandbox smoke test already run manually against a raw IP (see sandbox.py's
module docstring / PROJECT.md's M6 notes).

Run with: python checks/m6.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit import harness  # noqa: E402
from agentaudit.tools import AUDIT_LEDGER_PATH, CASE_LEDGER_PATH  # noqa: E402

TARGET = ROOT / "targets" / "network-probe-target"
CASE_ID = "case-m6-sandbox-001"
MARKER_PATH = Path("/etc/agentaudit-sandbox-test-marker")


def snapshot_target(target: Path) -> dict[str, bytes]:
    """Not a git-status check like checks/m5.py's — `network-probe-target`
    is a fixture newly added in this same milestone, so it's untracked and
    `git status --porcelain` would flag it regardless of whether run_case
    touched anything. Snapshotting file contents directly proves the run
    didn't mutate the fixture, independent of git's tracking state.
    """
    return {
        str(p.relative_to(target)): p.read_bytes()
        for p in sorted(target.rglob("*"))
        if p.is_file()
    }


def read_ledger(path: Path) -> list[dict]:
    import json

    if not path.exists():
        return []
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def main() -> int:
    assert TARGET.is_dir(), f"fixture target missing: {TARGET}"

    if MARKER_PATH.exists():
        print(
            f"WARNING: {MARKER_PATH} already exists before this run — a previous "
            f"run's sandbox may have failed to block the write. Removing it so "
            f"this run is a meaningful test.",
            file=sys.stderr,
        )
        MARKER_PATH.unlink()

    audit_before = len(read_ledger(AUDIT_LEDGER_PATH))
    # runs/cases.jsonl is append-only and never cleared between runs, so a
    # bare "does a record for CASE_ID exist" check could pass on a stale
    # record left over from an earlier run of this same script even if
    # *this* run's record_result never fired. Snapshot the matching count
    # first (same pattern checks/m3.py uses) and require it to grow.
    case_before = len([r for r in read_ledger(CASE_LEDGER_PATH) if r.get("case_id") == CASE_ID])
    target_before = snapshot_target(TARGET)

    run = asyncio.run(harness.run_case(TARGET, CASE_ID))
    print(
        f"[session={run.result.session_id} turns={run.result.num_turns} "
        f"subtype={run.result.subtype!r}]"
    )
    print("--- tool calls ---")
    for c in run.tool_calls:
        print(f"  {c.name}({c.input})")
    assert run.result.subtype == "success", (
        f"case run did not complete successfully: subtype={run.result.subtype!r} "
        f"errors={run.result.errors!r}"
    )

    case_records = [r for r in read_ledger(CASE_LEDGER_PATH) if r.get("case_id") == CASE_ID]
    assert len(case_records) > case_before, (
        f"no new case ledger record for {CASE_ID!r} — record_result didn't fire "
        f"this run (found {len(case_records)} matching record(s), same as before)"
    )
    evidence = case_records[-1]["evidence"]
    print(f"case ledger evidence: {evidence!r}")

    assert "SANDBOX_RESULT" in evidence, (
        f"evidence doesn't carry the probe's stable result line — expected "
        f"record_result to relay execute_case_against_target's actual output "
        f"verbatim: {evidence!r}"
    )
    assert "network=blocked" in evidence, f"network attempt was not reported blocked: {evidence!r}"
    assert "filesystem=blocked" in evidence, (
        f"filesystem attempt was not reported blocked: {evidence!r}"
    )
    # Not just the terse summary line — the actual denial reason (the
    # exception text probe.py prints per attempt) should have made it into
    # the evidence too, not just been dropped in the model's relay.
    assert "network attempt:" in evidence, (
        f"evidence is missing the network attempt's actual denial detail, not "
        f"just the SANDBOX_RESULT summary: {evidence!r}"
    )
    assert "filesystem attempt:" in evidence, (
        f"evidence is missing the filesystem attempt's actual denial detail, not "
        f"just the SANDBOX_RESULT summary: {evidence!r}"
    )
    print("OK: both violations reported blocked, with denial detail, in the case ledger")

    assert not MARKER_PATH.exists(), (
        f"{MARKER_PATH} exists on the real filesystem — the sandbox did NOT "
        f"block the /etc write, regardless of what the ledger claims"
    )
    print(f"OK: {MARKER_PATH} does not exist — /etc write genuinely blocked")

    audit_records = read_ledger(AUDIT_LEDGER_PATH)
    new_audit_records = audit_records[audit_before:]
    print(f"audit ledger: {len(new_audit_records)} new record(s) this run")
    assert len(new_audit_records) == len(run.tool_calls), (
        f"audit ledger has {len(new_audit_records)} new record(s) but the message "
        f"stream shows {len(run.tool_calls)} tool call(s) — a gap means something "
        f"ran unlogged"
    )
    logged_names = [r["tool_name"] for r in new_audit_records]
    stream_names = [c.name for c in run.tool_calls]
    assert logged_names == stream_names, (
        f"audit ledger tool order {logged_names} does not match the message "
        f"stream's order {stream_names}"
    )
    print("OK: audit ledger tool-call count and order match the message stream exactly")

    target_after = snapshot_target(TARGET)
    assert target_after == target_before, (
        "network-probe-target's contents changed during the run — the sandbox "
        "should have confined execute_case_against_target's writes to a scratch "
        "directory outside the target"
    )
    print("OK: target fixture untouched")

    return 0


if __name__ == "__main__":
    sys.exit(main())
