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
import tomllib
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


# M9: most real target repos (confirmed live against langchain-ai/react-agent)
# have no agentaudit.spec.json. Before M9, that left the case-executor
# subagent to *guess* an entry_point from common filenames on its own —
# unreliable and untestable. These four tiers replace that guessing with
# deterministic discovery from real signals already in the repo, checked in
# priority order; each returns (relative_path, human-readable source) on a
# match or (None, None) to fall through to the next tier.
def _resolve_candidate(target: Path, relative: str) -> Path | None:
    """A discovered path counts only if it's a real file that actually
    stays inside `target` — same path-traversal posture
    execute_case_against_target's own check applies, since a malicious
    pyproject.toml/langgraph.json/package.json is untrusted input too.
    """
    candidate = (target / relative).resolve()
    if not candidate.is_relative_to(target.resolve()):
        return None
    return candidate if candidate.is_file() else None


def _discover_from_pyproject(target: Path) -> tuple[str | None, str | None]:
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        return None, None
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None, None

    project = data.get("project", {}) or {}
    scripts: dict[str, Any] = dict(project.get("scripts", {}) or {})
    console_scripts = (project.get("entry-points", {}) or {}).get("console_scripts", {})
    scripts.update(console_scripts or {})

    for name, entry in scripts.items():
        if not isinstance(entry, str):
            continue
        module = entry.split(":", 1)[0]
        rel = module.replace(".", "/") + ".py"
        for prefix in ("", "src/"):
            candidate = prefix + rel
            if _resolve_candidate(target, candidate) is not None:
                return candidate, f"pyproject.toml:project.scripts.{name}"
    return None, None


def _discover_from_langgraph_json(target: Path) -> tuple[str | None, str | None]:
    spec_file = target / "langgraph.json"
    if not spec_file.is_file():
        return None, None
    try:
        data = json.loads(spec_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None

    for name, value in (data.get("graphs", {}) or {}).items():
        if not isinstance(value, str):
            continue
        # langgraph.json's own convention is "path/to/file.py:attr" (the
        # graph object inside that module) — keep only the path component,
        # matching execute_case_against_target's existing bare-script
        # contract; the :attr half is a separate execution-semantics
        # question, out of scope here.
        path_part = value.split(":", 1)[0]
        if path_part.startswith("./"):
            path_part = path_part[2:]
        if _resolve_candidate(target, path_part) is not None:
            return path_part, f"langgraph.json:graphs.{name}"
    return None, None


def _discover_from_package_json(target: Path) -> tuple[str | None, str | None]:
    package_file = target / "package.json"
    if not package_file.is_file():
        return None, None
    try:
        data = json.loads(package_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None

    main = data.get("main")
    if isinstance(main, str) and _resolve_candidate(target, main) is not None:
        return main, "package.json:main"
    return None, None


# The filenames the case-executor subagent was, until M9, left to guess on
# its own against a spec-less target (see CASE_EXECUTOR_AGENT_PROMPT in
# harness.py and the live react-agent evidence that motivated this).
# Codified here as the deterministic last resort, tried only after the
# three real-signal tiers above find nothing.
_FALLBACK_FILENAMES = ("main.py", "app.py", "agent.py", "run.py", "__main__.py", "src/main.py")


def _discover_from_conventions(target: Path) -> tuple[str | None, str | None]:
    for name in _FALLBACK_FILENAMES:
        if _resolve_candidate(target, name) is not None:
            return name, f"convention:{name}"
    for graph_py in sorted(target.glob("src/*/graph.py")):
        rel = graph_py.relative_to(target).as_posix()
        if _resolve_candidate(target, rel) is not None:
            return rel, "convention:src/*/graph.py"
    return None, None


_DISCOVERY_TIERS = (
    ("pyproject.toml", _discover_from_pyproject),
    ("langgraph.json", _discover_from_langgraph_json),
    ("package.json", _discover_from_package_json),
    ("common conventions", _discover_from_conventions),
)


def _discover_entry_point(target: Path) -> tuple[str | None, str]:
    checked: list[str] = []
    for label, finder in _DISCOVERY_TIERS:
        entry_point, source = finder(target)
        if entry_point is not None:
            assert source is not None
            return entry_point, source
        checked.append(label)
    return None, (
        "checked " + ", ".join(checked) + f" (fallback filenames tried: "
        f"{', '.join(_FALLBACK_FILENAMES)}, src/*/graph.py) — none found"
    )


@tool(
    "load_target_spec",
    "Read a target repository's declared capabilities. Looks for "
    "agentaudit.spec.json at the root of the given target directory; most "
    "real repos don't have one, so if it's missing this discovers a likely "
    "entry_point instead from real signals — pyproject.toml's "
    "project.scripts/console_scripts, langgraph.json's graphs field, "
    "package.json's main field, then common filename conventions, in that "
    "order — rather than leaving the caller to guess. Returns is_error only "
    "if neither a declared spec nor discovery finds anything to run.",
    {"path": str},
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def load_target_spec(args: dict[str, Any]) -> dict[str, Any]:
    target = Path(args["path"])
    spec_file = target / "agentaudit.spec.json"
    if spec_file.is_file():
        try:
            spec_text = spec_file.read_text()
            json.loads(spec_text)  # validate it parses before handing it to Claude
        except (OSError, json.JSONDecodeError) as e:
            return {
                "content": [{"type": "text", "text": f"Could not read spec: {e}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": spec_text}]}

    entry_point, reason = _discover_entry_point(target)
    if entry_point is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"No agentaudit.spec.json found at {spec_file}. "
                        f"Entry-point discovery found nothing to run either: {reason}."
                    ),
                }
            ],
            "is_error": True,
        }

    discovered_spec = json.dumps({"entry_point": entry_point, "source": reason})
    return {"content": [{"type": "text", "text": discovered_spec}]}


@tool(
    "execute_case_against_target",
    "Run one adversarial case against the target and capture its output. "
    "`entry_point` is the entry_point filename declared or discovered by "
    "load_target_spec, executed relative to `target` as `python "
    "<entry_point>` — call this only after load_target_spec, using the "
    "entry_point value it reported, never a guessed or invented one. "
    "`stdin_input` is the case's literal input data (Case.target_input) — "
    "piped to the subprocess's stdin, which is how it actually reaches the "
    "target; it is never passed as a bare argument, never treated as a "
    "filename or command, and never imported/called in-process. Pass an "
    "empty string when a case has no input to feed. Execution is sandboxed: "
    "no network access, and writes are restricted to a scratch directory "
    "outside the target repo.",
    {"case_id": str, "target": str, "entry_point": str, "stdin_input": str},
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
)
async def execute_case_against_target(args: dict[str, Any]) -> dict[str, Any]:
    case_id = args["case_id"]
    target_dir = Path(args["target"]).resolve()
    entry_point = args["entry_point"]
    stdin_input = args.get("stdin_input", "") or ""

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
            stdin=stdin_input,
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
        f"case={case_id} entry_point={entry_point!r} "
        f"stdin_bytes={len(stdin_input.encode())} backend={result.backend} "
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
