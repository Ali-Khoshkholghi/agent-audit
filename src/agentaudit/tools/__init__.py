"""In-process MCP server exposing AgentAudit's case-execution tools.

Three tools, meant to be called in this order for one case:
`load_target_spec` -> `execute_case_against_target` -> `record_result`.
`execute_case_against_target` is a stub — no real execution happens until
M6 adds sandboxing.
"""
import json
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

CASE_LEDGER_PATH = Path(__file__).resolve().parent.parent.parent.parent / "runs" / "cases.jsonl"


@tool(
    "load_target_spec",
    "Read a target repository's declared capabilities. Looks for "
    "agentaudit.spec.json at the root of the given target directory and "
    "returns its contents.",
    {"path": str},
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def load_target_spec(args: dict[str, Any]) -> dict[str, Any]:
    target = Path(args["path"])
    spec_file = target / "agentaudit.spec.json"
    if not spec_file.is_file():
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No agentaudit.spec.json found at {spec_file}",
                }
            ],
            "is_error": True,
        }

    try:
        spec_text = spec_file.read_text()
        json.loads(spec_text)  # validate it parses before handing it to Claude
    except (OSError, json.JSONDecodeError) as e:
        return {
            "content": [{"type": "text", "text": f"Could not read spec: {e}"}],
            "is_error": True,
        }

    return {"content": [{"type": "text", "text": spec_text}]}


@tool(
    "execute_case_against_target",
    "Run one adversarial case against the target and capture its output. "
    "Execution is stubbed until M6 adds sandboxing — this does not run any "
    "target code. Call this only after load_target_spec.",
    {"case_id": str, "input": str},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
async def execute_case_against_target(args: dict[str, Any]) -> dict[str, Any]:
    case_id = args["case_id"]
    case_input = args["input"]
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"STUB EXECUTION (no sandboxing until M6): case={case_id} "
                    f"input={case_input!r} -> outcome=inconclusive"
                ),
            }
        ]
    }


@tool(
    "record_result",
    "Append one case's outcome and evidence to the run ledger. Call this "
    "only after execute_case_against_target, using the outcome and "
    "evidence it reported.",
    {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "outcome": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
            "evidence": {"type": "string"},
        },
        "required": ["case_id", "outcome", "evidence"],
    },
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
)
async def record_result(args: dict[str, Any]) -> dict[str, Any]:
    record = {
        "timestamp": time.time(),
        "case_id": args["case_id"],
        "outcome": args["outcome"],
        "evidence": args["evidence"],
    }
    CASE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CASE_LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")

    return {
        "content": [
            {
                "type": "text",
                "text": f"Recorded {args['outcome']} for case {args['case_id']}",
            }
        ]
    }


audit_server = create_sdk_mcp_server(
    name="agentaudit",
    version="1.0.0",
    tools=[load_target_spec, execute_case_against_target, record_result],
)
