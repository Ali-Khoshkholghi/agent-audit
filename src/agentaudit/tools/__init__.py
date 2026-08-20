"""In-process MCP server exposing AgentAudit's case-execution tools.

Three tools, meant to be called in this order for one case:
`load_target_spec` -> `execute_case_against_target` -> `record_result`.
`execute_case_against_target` really executes the target's entry_point
(M6), sandboxed via agentaudit.sandbox — no network, writes restricted to a
scratch directory. That sandboxing lives inside this tool's implementation,
so it applies no matter which harness code path calls it.
"""
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from agentaudit import sandbox

RUNS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "runs"
CASE_LEDGER_PATH = RUNS_DIR / "cases.jsonl"

# M6: every PreToolUse hook invocation on the run_case path appends here
# (see harness._audit_pretooluse_hook) — a complete, unconditional record
# of every tool call, independent of whether it was approved or denied.
AUDIT_LEDGER_PATH = RUNS_DIR / "audit.jsonl"

SCRATCH_ROOT = RUNS_DIR / "scratch"


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
    "`input` is the entry_point filename declared in the target's spec "
    "(from load_target_spec), executed relative to `target`. Execution is "
    "sandboxed: no network access, and writes are restricted to a scratch "
    "directory outside the target repo. Call this only after "
    "load_target_spec, using the entry_point value it reported.",
    {"case_id": str, "target": str, "input": str},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
)
async def execute_case_against_target(args: dict[str, Any]) -> dict[str, Any]:
    case_id = args["case_id"]
    target_dir = Path(args["target"]).resolve()
    entry_point = args["input"]

    script_path = (target_dir / entry_point).resolve()
    if not script_path.is_relative_to(target_dir):
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Refusing to execute {entry_point!r}: it resolves outside "
                        f"the target directory {target_dir} (possible path traversal)."
                    ),
                }
            ],
            "is_error": True,
        }
    if not script_path.is_file():
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"entry_point {entry_point!r} not found at {script_path}",
                }
            ],
            "is_error": True,
        }

    # Deliberately built from a fresh uuid alone, NOT from case_id: case_id
    # is attacker-influenced (in run_case it comes straight from the
    # caller; in M5's flow it comes from a case-generator subagent that
    # just read the untrusted target's own files) and this path gets
    # interpolated into the Seatbelt profile's subpath literal in
    # sandbox.py. A case_id containing `"` or `/` could otherwise break
    # out of that literal or relocate the "writable scratch" location
    # outside SCRATCH_ROOT, defeating the sandbox it's meant to constrain.
    scratch_dir = SCRATCH_ROOT / uuid.uuid4().hex
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = sandbox.run_sandboxed(
            [sys.executable, str(script_path)],
            cwd=target_dir,
            scratch_dir=scratch_dir,
            timeout=15.0,
        )
    except sandbox.SandboxUnavailableError as e:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Cannot execute target code: no sandbox backend available "
                        f"on this platform ({e}). Refusing to run unsandboxed."
                    ),
                }
            ],
            "is_error": True,
        }

    summary = (
        f"case={case_id} entry_point={entry_point!r} backend={result.backend} "
        f"returncode={result.returncode} timed_out={result.timed_out}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return {"content": [{"type": "text", "text": summary}]}


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
