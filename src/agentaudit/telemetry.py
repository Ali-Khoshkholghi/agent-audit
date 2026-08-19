"""Append-only JSONL run ledger. Every run gets one record."""
import json
import time
from pathlib import Path

from claude_agent_sdk import ResultMessage

LEDGER_PATH = Path(__file__).resolve().parent.parent.parent / "runs" / "telemetry.jsonl"


def record_run(target: Path, result: ResultMessage) -> dict:
    record = {
        "timestamp": time.time(),
        "target": str(target),
        "session_id": result.session_id,
        "num_turns": result.num_turns,
        "subtype": result.subtype,
        "terminal_reason": result.terminal_reason,
        "total_cost_usd": result.total_cost_usd,
        # Whole-tree accounting, not `result.usage` — usage excludes
        # subagent tokens and this ledger needs to stay correct from M5 on.
        "model_usage": result.model_usage,
    }
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record
