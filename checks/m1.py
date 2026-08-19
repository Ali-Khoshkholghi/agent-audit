"""M1 check: `agentaudit inspect` runs one agent turn over the fixture target
and appends a JSONL telemetry record with a real cost, a terminal_reason of
"completed", and a bounded turn count.

Run with: python checks/m1.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "targets" / "simple-langgraph-agent"
LEDGER = ROOT / "runs" / "telemetry.jsonl"

# Upper bound for a 2-file, ~40-line fixture. Observed turn counts across
# real runs of a correct implementation cluster at 4-7 (the exact count
# varies with how many Glob/Read calls Claude chooses before answering), so
# this sits well above that range rather than inside it — it exists to
# catch a genuine runaway loop, not to pin down an exact turn count.
MAX_TURNS = 20


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    lines = [line for line in LEDGER.read_text().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def main() -> int:
    assert TARGET.is_dir(), f"fixture target missing: {TARGET}"

    before = len(read_ledger())

    proc = subprocess.run(
        [sys.executable, "-m", "agentaudit", "inspect", str(TARGET)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    print("--- stdout ---")
    print(proc.stdout)
    if proc.stderr:
        print("--- stderr ---")
        print(proc.stderr)

    assert proc.returncode == 0, f"inspect exited {proc.returncode}"

    records = read_ledger()
    assert len(records) > before, (
        f"no new JSONL record appended to {LEDGER.relative_to(ROOT)}"
    )
    record = records[-1]

    assert record.get("total_cost_usd") is not None, f"total_cost_usd is null: {record}"

    terminal_reason = record.get("terminal_reason")
    assert terminal_reason == "completed", (
        f"terminal_reason was {terminal_reason!r}, expected 'completed': {record}"
    )

    num_turns = record.get("num_turns")
    assert num_turns is not None and num_turns < MAX_TURNS, (
        f"num_turns={num_turns} did not stay under {MAX_TURNS}: {record}"
    )

    print(
        f"OK: session={record.get('session_id')} turns={num_turns} "
        f"cost=${record['total_cost_usd']:.4f} terminal_reason={terminal_reason!r}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
