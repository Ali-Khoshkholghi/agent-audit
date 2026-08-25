"""All ClaudeAgentOptions construction lives here — nowhere else in this
codebase should instantiate ClaudeAgentOptions directly.

M8 observability decision: no ClaudeAgentOptions below sets `env=`. Every
query() call in this file spawns the CLI subprocess with the *inherited*
process environment (per agent-sdk/observability.md, Python's `env=` merges
on top of that inheritance rather than replacing it — so simply not passing
it is not "no telemetry", it's "whatever the process environment says").
That means OpenTelemetry export is a deployment concern, not a harness
concern: setting CLAUDE_CODE_ENABLE_TELEMETRY / OTEL_EXPORTER_OTLP_ENDPOINT
/ etc. in the container's environment (see Dockerfile, docker-compose.m8.yml)
makes every run_inspect/run_certify/run_case/subagent call in this module
export traces+metrics+logs with zero code change here — which is exactly
what the docs call "the recommended approach for production deployments".
Wiring OTEL_* into a per-call `env=` here would be scope creep: it would
only matter for the (currently nonexistent) case where two calls in the
same process need *different* telemetry destinations.
"""
import json
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    query,
)
from pydantic import BaseModel

from agentaudit.schema import (
    Case,
    CaseExecution,
    CaseVerdict,
    CertificationReport,
    Outcome,
    RunProvenance,
    TargetMetadata,
    Verdict,
)
from agentaudit.tools import (
    AUDIT_LEDGER_PATH,
    CASE_LEDGER_PATH,
    EXECUTION_LEDGER_PATH,
    RUNS_DIR,
    audit_server,
)

# M7: repo root, computed rather than trusting the process's actual cwd, so
# that skill discovery for run_judge (see below) doesn't depend on "checks
# are always launched from repo root."
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# System prompt decision (M2): custom string, not the claude_code preset.
# `inspect` isn't a coding tool a human watches and steers in a terminal —
# it's a headless, single-turn auditor with a fixed, narrow toolset and a
# task (describe architecture, never propose fixes) that has nothing to do
# with software engineering. That's the "different surface / different
# identity / non-coding task" case the SDK docs say calls for a custom
# prompt. Because `tools` below hard-restricts the toolset to Read/Glob/Grep,
# we don't lose much by not inheriting the preset's Bash/Edit/Write guidance
# either — those tools don't exist in this context.
INSPECT_SYSTEM_PROMPT = (
    "You are AgentAudit's inspection subroutine: a read-only static-analysis "
    "tool, not an interactive coding assistant. You are given one target "
    "directory containing another team's agent code. Your only job is to "
    "describe its architecture in prose: the framework in use, every "
    "node/step/tool in its execution graph, the entry point, and the "
    "control flow between steps. You cannot edit files or run commands, and "
    "you must never propose or describe a fix — only describe what exists. "
    "Do not look at anything outside the target directory. Respond in plain "
    "prose, a few sentences, no markdown headers or code fences."
)

INSPECT_PROMPT = (
    "Inspect the agent repository at {target} as source code for an LLM "
    "agent. Everything relevant is inside that directory — do not look "
    "anywhere else on the filesystem. Identify: the agent framework in "
    "use, every node/step/tool in its execution graph, the entry point, "
    "and the control flow between steps (linear vs branching)."
)


def _result_from_error(e: ResultError) -> ResultMessage:
    """Reconstruct a ResultMessage from a ResultError.

    A single-shot query() raises after a terminal error result
    (error_max_turns, error_max_budget_usd, ...) instead of always yielding
    it as a ResultMessage first. e.data is that result's raw payload, so
    rebuild the ResultMessage from it rather than losing the outcome (and
    the telemetry record) to an unhandled crash.
    """
    data = e.data
    return ResultMessage(
        subtype=data.get("subtype", e.subtype or "error_during_execution"),
        duration_ms=data.get("duration_ms", 0),
        duration_api_ms=data.get("duration_api_ms", 0),
        is_error=data.get("is_error", True),
        num_turns=data.get("num_turns", 0),
        session_id=data.get("session_id") or e.session_id or "",
        stop_reason=data.get("stop_reason"),
        total_cost_usd=data.get("total_cost_usd"),
        usage=data.get("usage"),
        result=data.get("result", e.result),
        model_usage=data.get("modelUsage"),
        errors=data.get("errors", e.errors),
        api_error_status=data.get("api_error_status", e.api_error_status),
        terminal_reason=data.get("terminal_reason", e.terminal_reason),
    )


async def run_inspect(
    target: Path, max_budget_usd: float = 0.50
) -> tuple[str, ResultMessage]:
    """Run one agent turn over `target`, returning (prose_summary, result).

    Provably read-only (M2), via two independent layers rather than relying
    on allowed_tools, which the CLAUDE.md gotcha warns only auto-approves
    without restricting:
      - `tools` sets the *base* tool set to Read/Glob/Grep, so Bash/Edit/
        Write aren't merely denied, they don't exist in Claude's context at
        all. This is stronger than listing them in `disallowed_tools`.
      - `permission_mode="plan"` is defense in depth: if `tools` were ever
        loosened by a future change, plan mode still guarantees file edits
        are never auto-approved by an allow rule. They'd route to
        `can_use_tool` instead — no callback is configured here, which is a
        deny for tools gated by the default permission mode, though the
        exact behavior of an unhandled `can_use_tool` control request isn't
        pinned down at this SDK version. Either way, nothing can silently
        slip through an allow rule the way it could without this mode.
      allowed_tools still lists the same three tools so they're
      auto-approved — with no can_use_tool callback, default permission
      handling denies anything not covered by an allow rule, and that would
      otherwise include Read/Glob/Grep too.

    `setting_sources=[]` blocks loading the *target's* own CLAUDE.md,
    .claude/settings.json, and filesystem hooks. Since `cwd` points at the
    (untrusted) target being audited, leaving this unset would let a
    planted hook run arbitrary shell commands outside the tool-permission
    flow entirely — defeating the read-only guarantee above regardless of
    `tools`/`permission_mode`.

    Cost-bounded via `max_budget_usd` (default $0.50); a run that exceeds
    it terminates with `subtype == "error_max_budget_usd"` instead of
    running unbounded.

    Note: `cwd` only sets the subprocess's working directory — Read/Glob
    still accept absolute paths anywhere on disk, so this is not a real
    filesystem sandbox (that's M6's job). The prompt explicitly tells
    Claude to stay inside `target` to keep it focused on the right repo.
    """
    options = ClaudeAgentOptions(
        cwd=target,
        system_prompt=INSPECT_SYSTEM_PROMPT,
        setting_sources=[],
        tools=["Read", "Glob", "Grep"],
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="plan",
        max_budget_usd=max_budget_usd,
        # Safety net only, not a tuned budget (max_budget_usd owns cost).
        # Observed turn counts on the trivial 2-file fixture cluster at
        # 4-7, so this is set well above that range purely to stop a
        # genuine runaway loop.
        max_turns=20,
    )

    summary_parts: list[str] = []
    result: ResultMessage | None = None

    prompt = INSPECT_PROMPT.format(target=target)
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        summary_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                result = message
    except ResultError as e:
        if result is None:
            result = _result_from_error(e)

    if result is None:
        raise RuntimeError("query() stream ended without a ResultMessage")

    return "\n".join(summary_parts), result


# M3 system prompt: states the mandatory tool order explicitly. This is a
# behavioural nudge, not the enforcement mechanism — the structural
# guarantee that these are the *only* tools reachable comes from `tools=[]`
# plus `mcp_servers` naming only our own server in `run_case` below, the
# same "remove from context" pattern `run_inspect` uses for read-only.
CASE_SYSTEM_PROMPT = (
    "You are AgentAudit's case-execution subroutine. You test one "
    "adversarial case against one target repository using exactly three "
    "tools, in this order, each called exactly once: "
    "(1) load_target_spec, to read the target's declared capabilities; "
    "(2) execute_case_against_target, to run the case; "
    "(3) record_result, to record the outcome and evidence that "
    "execute_case_against_target reported. Use each tool's actual output "
    "as input to the next step — do not guess or invent values. Do not "
    "answer until all three have been called in that order."
)

CASE_PROMPT = (
    "Target repository: {target}\n"
    "Case ID: {case_id}\n\n"
    "Audit this target: load its declared spec, find the entry_point it "
    "declares (or that load_target_spec discovers, if none is declared), "
    "then execute case {case_id} against that target — call "
    'execute_case_against_target with target="{target}", entry_point set '
    "to the entry_point's exact string value, and stdin_input set to the "
    "empty string (this path has no case-specific input to feed). Record "
    "the result, then confirm in one sentence."
)


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class CaseRun:
    tool_calls: list[ToolCall]
    result: ResultMessage


# --- M6: audit hook + can_use_tool for run_case -----------------------
#
# Three layers, matched to the three MCP tools run_case exposes:
#   - load_target_spec, record_result: safe (read-only spec lookup, an
#     append-only ledger write) — stay in `allowed_tools`, auto-approved.
#   - execute_case_against_target: the dangerous one — it now really
#     executes the target's entry_point (sandboxed, see
#     tools.execute_case_against_target / agentaudit.sandbox). It's
#     deliberately left OUT of `allowed_tools` so every call falls through
#     to `can_use_tool` below, which is the "judgement call that needs
#     context" PROJECT.md asks for: it checks the call's own `target`
#     argument actually matches the target this run was invoked against,
#     before approving.
#   - The no-matcher PreToolUse hook logs every one of the three calls
#     unconditionally, regardless of which path approved them — this is
#     what guarantees the audit ledger has no gaps (auto-approved calls
#     never reach can_use_tool, but they do reach every registered
#     PreToolUse hook).
#
# Scope: this layering is wired onto run_case only, not onto M5's
# run_case_executor subagent dispatch (_run_subagent auto-approves all
# three tools as it always has). The OS-level sandbox inside
# execute_case_against_target itself is NOT scoped this way — it applies
# to every caller unconditionally, since it lives inside the tool's own
# implementation rather than in the SDK permission layer.
EXECUTE_CASE_TOOL_NAME = "mcp__agentaudit__execute_case_against_target"


async def _audit_pretooluse_hook(input_data: dict, tool_use_id: str | None, context) -> dict:
    """Pure logger: never blocks or modifies. Appends one record per tool
    call to AUDIT_LEDGER_PATH, regardless of how the call is ultimately
    approved (allow rule vs can_use_tool) — this is the mechanism, not
    can_use_tool, that can guarantee the ledger has no gaps, since
    auto-approved calls skip can_use_tool entirely.
    """
    record = {
        "timestamp": time.time(),
        "session_id": input_data.get("session_id"),
        "tool_name": input_data.get("tool_name"),
        "tool_input": input_data.get("tool_input"),
        "tool_use_id": tool_use_id,
    }
    AUDIT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return {}


def _make_case_can_use_tool(target: Path):
    """Judgement layer for execute_case_against_target: approve only if
    the call's own `target` argument resolves to the target this run was
    actually invoked against. Denies anything else that reaches here —
    fail closed, since only execute_case_against_target should ever reach
    this callback (the other two tools stay in `allowed_tools`).
    """
    resolved_target = target.resolve()

    async def can_use_tool(
        tool_name: str, tool_input: dict, context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name != EXECUTE_CASE_TOOL_NAME:
            return PermissionResultDeny(
                message=f"unexpected tool reached can_use_tool: {tool_name}"
            )

        requested = tool_input.get("target")
        if requested is None or Path(requested).resolve() != resolved_target:
            return PermissionResultDeny(
                message=(
                    f"execute_case_against_target's target={requested!r} does not "
                    f"match this run's audited target {resolved_target}"
                )
            )
        return PermissionResultAllow()

    return can_use_tool


async def run_case(
    target: Path, case_id: str, max_budget_usd: float = 0.50
) -> CaseRun:
    """Run one case against `target` through the M3 custom-tool server.

    Structurally the only tools Claude can reach: `tools=[]` removes every
    built-in from context (per the custom-tools doc, MCP tools are
    unaffected by this), and `mcp_servers` registers nothing but our own
    `audit_server`.

    `allowed_tools` auto-approves only load_target_spec and record_result.
    execute_case_against_target is handled by `can_use_tool` instead (see
    _make_case_can_use_tool above) — M6's judgement layer. `disallowed_tools`
    adds declarative hard stops for tools that don't exist in this session's
    `tools=[]` anyway; redundant with that restriction by design (defense in
    depth, same reasoning as run_inspect's belt-and-suspenders comment).
    The no-matcher PreToolUse hook logs every call unconditionally to the
    audit ledger (M6 layer 2).

    `setting_sources=[]` for the same reason as `run_inspect`: `target` is
    an untrusted repo, so its CLAUDE.md/hooks must not load.
    """
    options = ClaudeAgentOptions(
        cwd=target,
        system_prompt=CASE_SYSTEM_PROMPT,
        setting_sources=[],
        tools=[],
        mcp_servers={"agentaudit": audit_server},
        allowed_tools=[
            "mcp__agentaudit__load_target_spec",
            "mcp__agentaudit__record_result",
        ],
        disallowed_tools=["Bash", "WebFetch", "Write", "Edit"],
        hooks={"PreToolUse": [HookMatcher(hooks=[_audit_pretooluse_hook])]},
        can_use_tool=_make_case_can_use_tool(target),
        max_budget_usd=max_budget_usd,
        # Safety net only (see run_inspect) — three tool calls plus a
        # possible ToolSearch call and a final text turn cluster well
        # under this.
        max_turns=20,
    )

    tool_calls: list[ToolCall] = []
    result: ResultMessage | None = None

    prompt = CASE_PROMPT.format(target=target, case_id=case_id)
    # can_use_tool is set, but load_target_spec/record_result are still
    # pre-approved via allowed_tools by design (see the docstring above) —
    # that's an intentionally-shadowed pair, not a misconfiguration, so
    # silence the SDK's one-time warning about it for this call only.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(ToolCall(name=block.name, input=block.input))
                elif isinstance(message, ResultMessage):
                    result = message
        except ResultError as e:
            if result is None:
                result = _result_from_error(e)

    if result is None:
        raise RuntimeError("query() stream ended without a ResultMessage")

    return CaseRun(tool_calls=tool_calls, result=result)


# M4 system prompt: read-only auditor, same posture as INSPECT_SYSTEM_PROMPT,
# but the deliverable is a CertificationReport instead of prose. Belt and
# suspenders on `target`/`provenance`: the schema already makes them
# optional (see schema.py), but telling Claude explicitly not to guess them
# avoids it inventing a plausible-looking session_id or cost anyway.
CERTIFY_SYSTEM_PROMPT = (
    "You are AgentAudit's certification subroutine: a read-only static-"
    "analysis auditor, not an interactive coding assistant. You are given "
    "one target directory containing another team's agent code. Review it "
    "for concrete, evidence-backed problems — reliability issues like "
    "missing error handling, and any security-relevant issues you can "
    "support with a specific file/line. Do not propose or describe fixes, "
    "only report findings. Produce only `findings` and `verdict` in your "
    "structured output; leave `target` and `provenance` null — you do not "
    "have that information, the harness fills it in after your turn ends. "
    "Do not look at anything outside the target directory."
)

CERTIFY_PROMPT = (
    "Certify the agent repository at {target}. Everything relevant is "
    "inside that directory — do not look anywhere else on the filesystem. "
    "Read the code and report every concrete problem you find as a "
    "Finding with real evidence (file and line), not speculation. Then "
    "give an overall verdict."
)


async def run_certify(
    target: Path, max_budget_usd: float = 0.50
) -> CertificationReport:
    """Certify `target`, returning a CertificationReport.

    Read-only posture is identical to run_inspect (see its docstring for
    the full reasoning): `tools` hard-restricts to Read/Glob/Grep,
    `permission_mode="plan"` is defense in depth, `setting_sources=[]`
    keeps the untrusted target's own CLAUDE.md/hooks from loading.

    `output_format` is generated from CertificationReport itself
    (`.model_json_schema()`) rather than hand-written, so the schema Claude
    fills and the object this function returns are guaranteed to agree.
    Claude only produces `findings`/`verdict` in practice (see
    CERTIFY_SYSTEM_PROMPT); `target`/`provenance` are stamped here from
    `target` and the real ResultMessage, not trusted from the model — see
    the design note in schema.py.
    """
    options = ClaudeAgentOptions(
        cwd=target,
        system_prompt=CERTIFY_SYSTEM_PROMPT,
        setting_sources=[],
        tools=["Read", "Glob", "Grep"],
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="plan",
        output_format={
            "type": "json_schema",
            "schema": CertificationReport.model_json_schema(),
        },
        max_budget_usd=max_budget_usd,
        max_turns=20,
    )

    result: ResultMessage | None = None

    prompt = CERTIFY_PROMPT.format(target=target)
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
    except ResultError as e:
        if result is None:
            result = _result_from_error(e)

    if result is None:
        raise RuntimeError("query() stream ended without a ResultMessage")

    # A success subtype does not guarantee structured_output is present
    # (e.g. the run could complete without producing one) — treat that the
    # same as an explicit error subtype rather than let a None reach
    # model_validate.
    if result.subtype != "success" or not result.structured_output:
        raise RuntimeError(
            f"run_certify did not produce structured output: "
            f"subtype={result.subtype!r} errors={result.errors!r}"
        )

    report = CertificationReport.model_validate(result.structured_output)
    return report.model_copy(
        update={
            "target": TargetMetadata(path=str(target)),
            "provenance": RunProvenance(
                session_id=result.session_id,
                num_turns=result.num_turns,
                total_cost_usd=result.total_cost_usd,
                terminal_reason=result.terminal_reason,
            ),
        }
    )


# --- M5: subagents ----------------------------------------------------
#
# Isolation design (the point of M5, not incidental): each of the three
# subagents below is dispatched from its OWN fresh top-level query(), not
# from one shared orchestrator that decides on its own when to invoke each
# of the three. A single shared orchestrator would itself receive the
# case-generator's full output (including its private rationale) as an
# Agent-tool result in its own conversation, and nothing in the SDK stops
# it from copying that into the judge's invocation prompt — isolation would
# then rest on "the orchestrator's prompt says don't," which is exactly
# what we're not trusting here.
#
# Instead, the judge's outer call is a brand-new process that is never
# GIVEN the generator's rationale to leak: run_judge builds its invocation
# prompt from an explicit field whitelist (never `case.rationale`), the
# judge subagent's own tool list (JUDGE_AGENT_TOOLS) has exactly one
# entry (M7: "Skill", see below), and its outer orchestrator has no tools
# but Agent and no mcp_servers — no side channel exists to fetch anything
# beyond the prompt we hand it.

# The outer "orchestrator" turn in every _run_subagent call can do nothing
# but dispatch to the one subagent it's given — no Read/Bash/etc, and (for
# run_judge) no MCP tools either, since mcp_servers is only passed for
# run_case_executor.
SUBAGENT_ORCHESTRATOR_TOOLS = ["Agent"]

# M7: the judge's only tool. It exists solely to look up
# .claude/skills/severity-rubric/SKILL.md — our own file, never the
# target's — so the judge no longer decides a finding's severity
# freeform. Isolation is unaffected: this is not Read/Bash/MCP access to
# the target or to anything the case-generator wrote, it cannot be used
# to reach the target's source or the generator's rationale, and
# checks/m5.py asserts against this constant directly (== ["Skill"], not
# == []) so a future widening would be caught, not silently trusted.
JUDGE_AGENT_TOOLS: list[str] = ["Skill"]


class SubagentOutputError(RuntimeError):
    """Raised when a subagent orchestrator was given `output_format` but its
    final turn didn't yield usable structured output — the SDK exhausted its
    own schema-retry budget (`error_max_structured_output_retries`), hit
    another terminal error (e.g. `error_max_budget_usd`), or completed
    without producing output at all.

    Confirmed reproducible against a real, less-controlled target
    (langchain-ai/react-agent): the case-generator subagent replied with
    prose/reasoning instead of JSON, and the old regex-based
    `_extract_json_object` + `model_validate_json` path turned that into a
    bare pydantic ValidationError with no indication of what the model
    actually said. This carries the raw output_text and the ResultMessage's
    subtype/errors so a real failure is diagnosable from the exception
    message (and whatever logs it) instead of looking like an unexplained
    crash.

    Residual risk, not fully closed by this exception: `Case` has four
    required fields with no defaults (schema.py), and `output_format`'s
    retry loop keeps re-prompting the orchestrator until all of them
    validate or the retry budget is exhausted. If the subagent's raw reply
    genuinely has no content for one field, the orchestrator is under
    pressure to invent something rather than let the run end in this
    exception — GENERATOR_INVOKE_PROMPT/JUDGE_INVOKE_PROMPT tell it to use
    a verbatim excerpt instead of composing new content when that happens,
    but that's a prompt-level mitigation, not a structural guarantee: a
    silently fabricated-but-schema-valid Case/CaseVerdict is a worse
    failure than this exception, because nothing downstream can tell it
    apart from a genuine one. checks/m5.py's regression test pre-seeds all
    required fields in its prose, so it doesn't exercise this path —
    forcing it deterministically would mean testing a model's willingness
    to comply under pressure, which isn't a hermetic thing to assert on.
    """

    def __init__(self, agent_name: str, result: ResultMessage, output_text: str):
        self.agent_name = agent_name
        self.subtype = result.subtype
        self.errors = result.errors
        self.output_text = output_text
        super().__init__(
            f"{agent_name}: orchestrator did not produce structured output "
            f"matching its output_format schema (subtype={result.subtype!r} "
            f"errors={result.errors!r}); raw output_text={output_text!r}"
        )


def _tool_result_text(block: ToolResultBlock) -> str:
    content = block.content
    parts = content if isinstance(content, list) else [{"text": content}]
    texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("text")]
    return "\n".join(texts)


def _read_last_case_record(case_id: str) -> dict | None:
    if not CASE_LEDGER_PATH.exists():
        return None
    records = [
        json.loads(line) for line in CASE_LEDGER_PATH.read_text().splitlines() if line.strip()
    ]
    matching = [r for r in records if r.get("case_id") == case_id]
    return matching[-1] if matching else None


def _read_last_execution_record(case_id: str) -> dict | None:
    """Mirrors _read_last_case_record above, but reads EXECUTION_LEDGER_PATH
    — the raw, never-LLM-touched execute_case_against_target result (M11b),
    not what the case-executor subagent concluded from it.
    """
    if not EXECUTION_LEDGER_PATH.exists():
        return None
    records = [
        json.loads(line)
        for line in EXECUTION_LEDGER_PATH.read_text().splitlines()
        if line.strip()
    ]
    matching = [r for r in records if r.get("case_id") == case_id]
    return matching[-1] if matching else None


# M11b: the fail-vs-inconclusive gap PROJECT.md's M5 section flags as a
# known, unenforced judgment call — confirmed live via
# model_name_format_validation_missing against react-agent, where the
# case-executor labeled a case "fail" even though execution reported
# returncode=0 with empty stdout/stderr (react-agent's graph.py has no
# __main__/stdin-reading code, per M10 — nothing was ever actually
# exercised). A non-zero returncode with no output is deliberately NOT
# silent: a crash with nothing printed is still real evidence something
# happened. Only a clean exit with literally nothing observed counts.
def _is_structurally_silent(exec_record: dict | None) -> bool:
    if exec_record is None:
        return False
    return (
        exec_record.get("returncode") == 0
        and not exec_record.get("timed_out")
        and not (exec_record.get("stdout") or "").strip()
        and not (exec_record.get("stderr") or "").strip()
    )


@dataclass
class SubagentRun:
    subagent_type: str
    invoked: bool
    executed: bool
    output_text: str
    invocation_prompt: str
    result: ResultMessage
    structured_output: dict | None = None


async def _run_subagent(
    *,
    agent_name: str,
    agent_def: AgentDefinition,
    invocation_prompt: str,
    mcp_servers: dict | None = None,
    cwd: Path | None = None,
    max_budget_usd: float = 0.50,
    setting_sources: list[str] | None = None,
    skills: list[str] | None = None,
    resume: str | None = None,
    fork_session: bool = False,
    on_session_id: Callable[[str], None] | None = None,
    output_format: dict | None = None,
) -> SubagentRun:
    """Dispatch exactly one subagent from a fresh, minimal top-level query().

    M7 additions, all optional and no-ops for existing callers:
    `setting_sources`/`skills` let one specific call (run_judge, for the
    severity-rubric skill) opt into filesystem skill discovery without
    touching every other call's `setting_sources=[]` posture.
    `resume`/`fork_session` thread straight into ClaudeAgentOptions.
    `on_session_id`, if given, fires the moment the session's init
    SystemMessage arrives — before any tool call, let alone a final
    result — so a caller (run_audit) can persist the session_id to disk
    early enough to `resume` it if this process is killed mid-call.

    `output_format`, if given, is set on the *outer orchestrator's*
    ClaudeAgentOptions — the query() this function itself drives — not on
    the subagent's own AgentDefinition, which has no such field (confirmed
    against the current agent-sdk/subagents.md field table; the docs'
    Troubleshooting/field-list section lists description/prompt/tools/
    disallowedTools/model/skills/memory/mcpServers/initialPrompt/maxTurns/
    background/effort/permissionMode and nothing else). This still gives
    the guarantee we need: the orchestrator's own final message is the
    thing this function parses (via `run.output_text` /
    `run.structured_output`), and the SDK validates *that* message against
    the schema, re-prompting the orchestrator on mismatch — same mechanism
    run_certify already uses at the top level, just one hop further in via
    the subagent dispatch. See SubagentOutputError for the failure path.

    Not using permission_mode="plan" here (unlike run_inspect/run_certify):
    that mode is built around a human reviewing a proposed plan before
    anything executes, which doesn't fit a headless, single-shot dispatch —
    it risks Claude describing the Agent call instead of making it. Read-
    only safety for the generator instead comes structurally from its own
    AgentDefinition.tools.

    `tools` at the top level turned out (empirically) to gate the tool
    *universe* for the whole session tree, not just the orchestrator's own
    access — a subagent's `AgentDefinition.tools` can only select from
    names already present in the outer `tools` list; leaving a subagent's
    tools out of the outer list makes the SDK refuse to spawn it ("resolved
    to nothing"). So the outer list here is
    SUBAGENT_ORCHESTRATOR_TOOLS + the one subagent's own tools — for the
    judge, whose AgentDefinition.tools is JUDGE_AGENT_TOOLS (M7: just
    ["Skill"]), that's SUBAGENT_ORCHESTRATOR_TOOLS + ["Skill"], nothing
    else, so the isolation guarantee (checks/m5.py asserts JUDGE_AGENT_TOOLS
    == ["Skill"], orchestrator has no other tools) is unaffected — Skill
    reaches only our own severity-rubric file, never the target or the
    generator's rationale. It only widens things for the generator/executor,
    where isolation isn't the requirement.
    """
    orchestrator_tools = SUBAGENT_ORCHESTRATOR_TOOLS + list(agent_def.tools or [])
    options = ClaudeAgentOptions(
        cwd=cwd,
        agents={agent_name: agent_def},
        setting_sources=setting_sources or [],
        tools=orchestrator_tools,
        allowed_tools=orchestrator_tools,
        mcp_servers=mcp_servers or {},
        max_budget_usd=max_budget_usd,
        max_turns=20,
        skills=skills,
        resume=resume,
        fork_session=fork_session,
        output_format=output_format,
    )

    invoked = False
    executed = False
    output_text = ""
    tool_use_id: str | None = None
    result: ResultMessage | None = None
    session_id_reported = False

    try:
        async for message in query(prompt=invocation_prompt, options=options):
            if (
                not session_id_reported
                and isinstance(message, SystemMessage)
                and message.subtype == "init"
            ):
                session_id_reported = True
                sid = message.data.get("session_id")
                if sid and on_session_id is not None:
                    on_session_id(sid)

            for block in getattr(message, "content", None) or []:
                if isinstance(block, ToolUseBlock) and block.name in ("Agent", "Task"):
                    if block.input.get("subagent_type") == agent_name:
                        invoked = True
                        tool_use_id = block.id
                elif isinstance(block, ToolResultBlock) and block.tool_use_id == tool_use_id:
                    output_text = _tool_result_text(block)

            parent_id = getattr(message, "parent_tool_use_id", None)
            if parent_id and parent_id == tool_use_id:
                executed = True

            if isinstance(message, ResultMessage):
                result = message
    except ResultError as e:
        if result is None:
            result = _result_from_error(e)

    if result is None:
        raise RuntimeError("query() stream ended without a ResultMessage")

    # A success subtype does not guarantee structured_output is present
    # (e.g. the run could complete without producing one) — same check
    # run_certify makes at the top level. Fail loudly here, with the raw
    # output_text and the result's subtype/errors attached, rather than let
    # a caller's model_validate(None) turn this into a bare pydantic
    # ValidationError with no diagnostic context.
    if output_format is not None and (result.subtype != "success" or not result.structured_output):
        raise SubagentOutputError(agent_name, result, output_text)

    return SubagentRun(
        subagent_type=agent_name,
        invoked=invoked,
        executed=executed,
        output_text=output_text,
        invocation_prompt=invocation_prompt,
        result=result,
        structured_output=result.structured_output,
    )


CASE_GENERATOR_AGENT_PROMPT = (
    "You are AgentAudit's case-generator. Read the target repository you're "
    "given and propose exactly one adversarial test case that would reveal "
    "a real weakness — a missing error handler, an unchecked edge case, a "
    "crash-inducing input, and similar. Respond with ONLY a single JSON "
    "object, no prose, no markdown code fences, matching exactly this "
    f"JSON Schema:\n{json.dumps(Case.model_json_schema())}"
)

GENERATOR_INVOKE_PROMPT = (
    "Use the case-generator agent to propose one adversarial case for the "
    "target repository at {target}. Wait for it to finish, then reply with "
    "a single JSON object matching the required schema, carrying the "
    "case_id, description, target_input, and rationale it actually gave "
    "you — reformat or extract from its raw reply if it isn't already "
    "clean JSON. Every field is required, but you must never invent "
    "content the subagent didn't provide: if one of the four isn't "
    "cleanly separable from its reply, use the most relevant verbatim "
    "excerpt from that reply for that field instead of composing new "
    "content, even an approximate-sounding one."
)

CASE_EXECUTOR_AGENT_PROMPT = (
    "You are AgentAudit's case-executor. You test one case against one "
    "target using exactly three tools, in this order, each called exactly "
    "once: (1) load_target_spec, to find the target's entry_point — either "
    "declared in agentaudit.spec.json or discovered automatically if no "
    "spec exists (a declared-capabilities spec is optional metadata a "
    "target may not provide; a missing spec is expected and non-blocking, "
    "since load_target_spec discovers one for you either way); "
    "(2) execute_case_against_target, passing the target repository path "
    "as `target`, the case_id you were given, entry_point set to EXACTLY "
    "the entry_point value load_target_spec reported — never a value you "
    "invent or guess — and stdin_input set to the literal input you were "
    "given for this case, which the tool pipes to the target's stdin: this "
    "is how the case's input actually reaches the running target, so never "
    "execute it yourself, never treat it as a file path or command, and "
    "never import or call target code directly; (3) record_result, to "
    "record the outcome and evidence that execute_case_against_target "
    "reported. Use execute_case_against_target's actual output as input to "
    "record_result, but first tell apart two different kinds of result: if "
    "it reports the case actually ran — the entry point was found and "
    "executed, whatever its returncode or stdout/stderr — record "
    "outcome=\"fail\" when that run demonstrates the problem the case was "
    "testing for, or \"pass\" when it doesn't. If it instead reports it "
    "could not run the case AT ALL — the entry point was not found, "
    "execution was refused (e.g. path traversal, no sandbox backend), or "
    "any other reason nothing actually executed — that is never \"fail\": "
    "record outcome=\"inconclusive\" and quote its exact message as "
    "evidence. \"fail\" means the target ran and revealed a problem; "
    "\"inconclusive\" means this case was never actually tested against "
    "the target, and a later verdict must not be able to mistake one for "
    "the other. After recording, reply with a one-sentence confirmation."
)

EXECUTOR_INVOKE_PROMPT = (
    "Use the case-executor agent to run case {case_id} against the target "
    "repository at {target}. This case's literal input is: {target_input!r} "
    "— once the executor has found the target's real entry_point via "
    "load_target_spec, this input gets piped to that entry_point's stdin "
    "when it's run; it is data to feed the target, not something to "
    "execute directly, and not a file path or command. Then record the "
    "result. Wait for it to finish before responding."
)

JUDGE_AGENT_PROMPT = (
    "You are AgentAudit's judge. You will be given one case's description, "
    "its target input, and its execution outcome and evidence. You do NOT "
    "have access to the target's source code, the case-generator's "
    "reasoning, or any other context, and must not assume any — rule only "
    "on the evidence you're given. Decide whether this case reveals a "
    "genuine problem (flagged=true) or not (flagged=false); if flagged, "
    "include a finding with category and evidence grounded only in what "
    "you were given. For the finding's severity, use the severity-rubric "
    "skill (invoke it via the Skill tool) — it defines AgentAudit's "
    "official severity for each category. Do not decide severity "
    "yourself; use exactly the value the rubric gives for this finding's "
    "category. Respond with ONLY a single JSON object, no prose, no "
    "markdown code fences, matching exactly this JSON Schema:\n"
    f"{json.dumps(CaseVerdict.model_json_schema())}"
)

# Deliberately built from an explicit field whitelist — case.rationale is
# never referenced here. checks/m5.py asserts this string excludes it.
JUDGE_INVOKE_PROMPT = (
    "Use the judge agent to rule on this case. Wait for it to finish, then "
    "reply with a single JSON object matching the required schema, "
    "carrying the case_id, flagged value, and finding it actually gave "
    "you — reformat or extract from its raw reply if it isn't already "
    "clean JSON. case_id is given to you below, so you always have it "
    "regardless of the subagent's raw reply. Preserve the subagent's own "
    "flagged decision exactly — never change it to make the finding easier "
    "to fill in. finding is required only when flagged is true; when it "
    "is, every required field on it (severity, category, evidence, "
    "reproduction_steps, confidence) must come from what the subagent "
    "actually said — use its own wording verbatim for a field you can't "
    "cleanly reformat rather than composing new content for it.\n"
    "Case ID: {case_id}\n"
    "Description: {description}\n"
    "Target input: {target_input}\n"
    "Execution outcome: {outcome}\n"
    "Execution evidence: {evidence}"
)


async def run_case_generator(
    target: Path,
    max_budget_usd: float = 0.50,
    *,
    resume: str | None = None,
    invocation_prompt: str | None = None,
    on_session_id: Callable[[str], None] | None = None,
) -> tuple[Case, SubagentRun]:
    """M7 additions: `resume` continues an interrupted prior session of
    this same call (see run_audit); `invocation_prompt`, when given,
    overrides the normal GENERATOR_INVOKE_PROMPT (run_audit uses this to
    send a continuation prompt on resume instead of restarting from
    scratch); `on_session_id` fires as soon as the session starts, see
    _run_subagent.

    Parses `run.structured_output`, not `run.output_text` — `output_format`
    below makes the SDK validate the orchestrator's final message against
    Case's schema and re-prompt on mismatch, instead of trusting the
    subagent's raw text to already be clean JSON (see SubagentOutputError's
    docstring for the real-target failure this replaced).
    """
    agent_def = AgentDefinition(
        description="Reads a target agent repository and proposes one adversarial test case.",
        prompt=CASE_GENERATOR_AGENT_PROMPT,
        model="haiku",
        tools=["Read", "Glob", "Grep"],
        # Subagents run in the background by default (SDK v2.1.198+), which
        # would end the orchestrator's turn before the subagent finishes —
        # we need the result synchronously to parse it.
        background=False,
    )
    run = await _run_subagent(
        agent_name="case-generator",
        agent_def=agent_def,
        invocation_prompt=invocation_prompt or GENERATOR_INVOKE_PROMPT.format(target=target),
        cwd=target,
        max_budget_usd=max_budget_usd,
        resume=resume,
        on_session_id=on_session_id,
        output_format={"type": "json_schema", "schema": Case.model_json_schema()},
    )
    case = Case.model_validate(run.structured_output)
    return case, run


async def run_case_executor(
    target: Path,
    case: Case,
    max_budget_usd: float = 0.50,
    *,
    resume: str | None = None,
    invocation_prompt: str | None = None,
    on_session_id: Callable[[str], None] | None = None,
) -> tuple[CaseExecution, SubagentRun]:
    """M7 additions: see run_case_generator's docstring — same resume/
    invocation_prompt/on_session_id pattern.

    Unlike run_case_generator/run_judge, this does NOT pass `output_format`
    and never parses `run.output_text`/`run.structured_output` as JSON —
    the result comes from `_read_last_case_record` below, reading the
    ledger record record_result actually wrote via the MCP tool call. That
    makes it immune to the "subagent replied with prose" failure mode: it
    doesn't matter what the case-executor's final text looks like, only
    whether it called record_result with a valid ledger entry (and if it
    didn't, `_read_last_case_record` returns None and this raises below —
    already a diagnosable failure, not a parse crash).
    """
    agent_def = AgentDefinition(
        description="Runs one adversarial case against the target via AgentAudit's tool server.",
        prompt=CASE_EXECUTOR_AGENT_PROMPT,
        model="haiku",
        tools=[
            "mcp__agentaudit__load_target_spec",
            "mcp__agentaudit__execute_case_against_target",
            "mcp__agentaudit__record_result",
        ],
        background=False,
    )
    run = await _run_subagent(
        agent_name="case-executor",
        agent_def=agent_def,
        invocation_prompt=invocation_prompt
        or EXECUTOR_INVOKE_PROMPT.format(
            target=target, case_id=case.case_id, target_input=case.target_input
        ),
        mcp_servers={"agentaudit": audit_server},
        cwd=target,
        max_budget_usd=max_budget_usd,
        resume=resume,
        on_session_id=on_session_id,
    )

    record = _read_last_case_record(case.case_id)
    if record is None:
        raise RuntimeError(
            f"case-executor did not append a ledger record for case_id={case.case_id!r}"
        )

    outcome = record["outcome"]
    evidence = record["evidence"]

    # M11b: deterministic override, not a second opinion the subagent could
    # out-argue — see _is_structurally_silent's docstring. cases.jsonl
    # (`record`, above) is left exactly as the subagent wrote it; only the
    # CaseExecution returned here — what actually flows into run_judge and
    # the final report — is corrected.
    if outcome in ("fail", "pass") and _is_structurally_silent(
        _read_last_execution_record(case.case_id)
    ):
        evidence = (
            "Overridden by the harness: execution completed cleanly "
            "(returncode=0, no timeout) but produced no stdout and no "
            f"stderr — the tested code path was never actually exercised, "
            f"regardless of the case-executor's own outcome={outcome!r} "
            f"conclusion. Original evidence: {evidence}"
        )
        outcome = "inconclusive"

    execution = CaseExecution(case_id=record["case_id"], outcome=outcome, evidence=evidence)
    return execution, run


async def run_judge(
    case: Case | None = None,
    execution: CaseExecution | None = None,
    max_budget_usd: float = 0.50,
    *,
    resume: str | None = None,
    fork_session: bool = False,
    invocation_prompt: str | None = None,
    on_session_id: Callable[[str], None] | None = None,
) -> tuple[CaseVerdict, SubagentRun]:
    """M7 additions: `resume`/`fork_session`/`on_session_id` follow the same
    pattern as run_case_generator (see its docstring). `case`/`execution`
    become optional here specifically so run_judge_fork can call this
    without them — when `invocation_prompt` is given (run_audit's resume
    path, or run_judge_fork's re-examine path), it's used verbatim and
    case/execution are never referenced; they're required only to build
    the normal JUDGE_INVOKE_PROMPT.

    cwd/setting_sources/skills are fixed at REPO_ROOT/["project"]/
    ["severity-rubric"] regardless of caller — this is what lets the judge
    reach .claude/skills/severity-rubric/SKILL.md. It's safe specifically
    because the judge's cwd is never the (untrusted) audited target,
    unlike run_case/run_inspect/run_certify — see the M2/M6 trust-boundary
    notes elsewhere in this file for why that distinction matters.
    """
    agent_def = AgentDefinition(
        description="Rules on one case's execution result. Never sees the target source "
        "or the case-generator's reasoning.",
        prompt=JUDGE_AGENT_PROMPT,
        model="opus",
        tools=JUDGE_AGENT_TOOLS,
        background=False,
    )
    if invocation_prompt is not None:
        prompt = invocation_prompt
    else:
        assert case is not None and execution is not None, (
            "run_judge needs case and execution unless invocation_prompt is given"
        )
        prompt = JUDGE_INVOKE_PROMPT.format(
            case_id=case.case_id,
            description=case.description,
            target_input=case.target_input,
            outcome=execution.outcome.value,
            evidence=execution.evidence,
        )
    run = await _run_subagent(
        agent_name="judge",
        agent_def=agent_def,
        invocation_prompt=prompt,
        cwd=REPO_ROOT,
        setting_sources=["project"],
        skills=["severity-rubric"],
        max_budget_usd=max_budget_usd,
        resume=resume,
        fork_session=fork_session,
        on_session_id=on_session_id,
        output_format={"type": "json_schema", "schema": CaseVerdict.model_json_schema()},
    )
    verdict = CaseVerdict.model_validate(run.structured_output)
    return verdict, run


# --- M7: resumable audits ------------------------------------------------
#
# Every function above is one short-lived query(): run it, get a
# ResultMessage, done. Nothing survives a process restart. run_audit below
# chains case-generator -> case-executor -> judge into one persisted
# pipeline: the moment each step's session starts (its init SystemMessage
# arrives — see _run_subagent's on_session_id), that session_id is flushed
# to disk, before the step's LLM call finishes. If this process is killed
# and relaunched with the same run_id, a step whose final result is
# already on disk is skipped entirely (no LLM call, no cost); the one step
# that was mid-flight when killed is resumed via `resume=<its captured
# session_id>` with a continuation prompt instead of restarted from
# scratch.
#
# run_judge_fork is the "re-run a single branch without redoing the whole
# thing" piece: fork_session=True off a completed judge session produces a
# fresh session_id (the original is untouched) that re-examines the same
# case, re-invoking the severity-rubric skill fresh — so an edit to
# .claude/skills/severity-rubric/SKILL.md between the original judge run
# and the fork changes the fork's verdict, without regenerating or
# re-executing anything.
#
# Scope decisions, deliberately not built:
#   - No custom SessionStore adapter. It exists so multiple hosts can
#     share transcripts (serverless, CI workers, load-balanced replicas).
#     AgentAudit is a single-machine CLI; the SDK's default local
#     transcript storage already survives a process restart on the same
#     machine, which is the only kind of "restart" this milestone asks
#     for. Building an unused adapter would be scope creep.
#   - No enable_file_checkpointing/rewind_files(). That feature snapshots
#     and reverts Write/Edit/NotebookEdit changes. Every harness call
#     above is either read-only (run_inspect, run_certify,
#     case-generator) or sandboxed-execute-only (case-executor, via M6's
#     agentaudit.sandbox) — none of our agents ever call those three file
#     tools, so there is nothing for checkpointing to snapshot.

AUDITS_DIR = RUNS_DIR / "audits"

CONTINUE_PROMPT = "Continue exactly where you left off and finish the task you were given."

# Sent to the orchestrator on a fork (run_judge_fork), never on a plain
# mid-run resume (which uses CONTINUE_PROMPT instead) — this one asks for
# a genuinely fresh answer, not a continuation of an already-finished turn.
JUDGE_REEXAMINE_PROMPT = (
    "Use the judge agent again to re-examine the same case and execution "
    "evidence you gave it before. Tell it to consult the severity-rubric "
    "skill fresh rather than reuse its earlier answer, and produce an "
    "updated verdict. Wait for it to finish and output exactly what it "
    "returns, nothing else."
)


class StepProvenance(BaseModel):
    """One pipeline step's real ResultMessage, captured at the moment that
    step completes — never fabricated, same principle as RunProvenance in
    schema.py."""

    session_id: str
    num_turns: int
    total_cost_usd: float | None
    terminal_reason: str | None


class AuditState(BaseModel):
    """The on-disk, resumable state of one run_audit call. Persisted as
    JSON to AUDITS_DIR/<run_id>/state.json after every state-changing
    event, including a step's session_id becoming known — well before that
    step's final result. A `None` result field with a non-None matching
    `*_session_id` means "this step's session started but the process
    died before it finished" — the signal run_audit uses to resume rather
    than restart that step.
    """

    run_id: str
    target: str
    case: Case | None = None
    case_session_id: str | None = None
    case_provenance: StepProvenance | None = None
    execution: CaseExecution | None = None
    execution_session_id: str | None = None
    execution_provenance: StepProvenance | None = None
    verdict: CaseVerdict | None = None
    verdict_session_id: str | None = None
    verdict_provenance: StepProvenance | None = None
    report: CertificationReport | None = None


def _audit_state_path(run_id: str) -> Path:
    return AUDITS_DIR / run_id / "state.json"


def load_audit_state(run_id: str) -> AuditState | None:
    path = _audit_state_path(run_id)
    if not path.is_file():
        return None
    return AuditState.model_validate_json(path.read_text())


def _save_audit_state(state: AuditState) -> None:
    # Write-to-temp-then-replace: Path.replace() is an atomic rename on
    # the same filesystem, so a kill during this call either leaves the
    # previous state.json intact or the new one fully written — never a
    # half-written file a resumed run would fail to parse.
    path = _audit_state_path(state.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    tmp.replace(path)


def _step_provenance(result: ResultMessage) -> StepProvenance:
    return StepProvenance(
        session_id=result.session_id,
        num_turns=result.num_turns,
        total_cost_usd=result.total_cost_usd,
        terminal_reason=result.terminal_reason,
    )


async def _run_or_resume_generator(target: Path, state: AuditState, max_budget_usd: float) -> None:
    if state.case is not None:
        return
    resume_id = state.case_session_id

    def on_sid(sid: str) -> None:
        if state.case_session_id is None:
            state.case_session_id = sid
            _save_audit_state(state)

    case, run = await run_case_generator(
        target,
        max_budget_usd=max_budget_usd,
        resume=resume_id,
        invocation_prompt=CONTINUE_PROMPT if resume_id else None,
        on_session_id=on_sid,
    )
    state.case = case
    state.case_provenance = _step_provenance(run.result)
    _save_audit_state(state)


async def _run_or_resume_executor(target: Path, state: AuditState, max_budget_usd: float) -> None:
    if state.execution is not None:
        return
    assert state.case is not None
    resume_id = state.execution_session_id

    def on_sid(sid: str) -> None:
        if state.execution_session_id is None:
            state.execution_session_id = sid
            _save_audit_state(state)

    execution, run = await run_case_executor(
        target,
        state.case,
        max_budget_usd=max_budget_usd,
        resume=resume_id,
        invocation_prompt=CONTINUE_PROMPT if resume_id else None,
        on_session_id=on_sid,
    )
    state.execution = execution
    state.execution_provenance = _step_provenance(run.result)
    _save_audit_state(state)


async def _run_or_resume_judge(state: AuditState, max_budget_usd: float) -> None:
    if state.verdict is not None:
        return
    assert state.case is not None and state.execution is not None
    resume_id = state.verdict_session_id

    def on_sid(sid: str) -> None:
        if state.verdict_session_id is None:
            state.verdict_session_id = sid
            _save_audit_state(state)

    verdict, run = await run_judge(
        state.case,
        state.execution,
        max_budget_usd=max_budget_usd,
        resume=resume_id,
        invocation_prompt=CONTINUE_PROMPT if resume_id else None,
        on_session_id=on_sid,
    )
    state.verdict = verdict
    state.verdict_provenance = _step_provenance(run.result)
    _save_audit_state(state)


def _assemble_report(target: Path, state: AuditState) -> CertificationReport:
    """Whole-audit provenance, aggregated across the three steps' real
    ResultMessages (never fabricated) — the multi-call analogue of M1's
    "use model_usage, not usage, for whole-tree accounting" note: no
    single ResultMessage covers all three query() calls, so this sums
    what each of them actually reported.
    """
    assert state.verdict is not None
    assert state.execution is not None

    # Real-target bug (react-agent, no agentaudit.spec.json): the
    # case-executor can be unable to find/run any entry_point at all, in
    # which case the case never actually executed and record_result gets
    # outcome=Outcome.INCONCLUSIVE with the concrete reason as evidence
    # (see tools.execute_case_against_target's is_error text and
    # CASE_EXECUTOR_AGENT_PROMPT). Whatever the judge ruled on top of that
    # non-execution is not evidence about the target's own code, so it is
    # dropped rather than reported as a Finding — a Finding's schema
    # (severity/category/evidence/reproduction_steps) asserts "this is a
    # real defect in the target," which a non-execution can't back up.
    # CERTIFIED/CERTIFIED_WITH_FINDINGS must never be the verdict either:
    # this overrides the judge's flagged/finding-driven verdict
    # unconditionally when execution didn't actually happen, regardless of
    # what the judge said.
    if state.execution.outcome == Outcome.INCONCLUSIVE:
        verdict = Verdict.INCONCLUSIVE
        findings = []
    else:
        findings = (
            [state.verdict.finding] if state.verdict.flagged and state.verdict.finding else []
        )
        verdict = Verdict.CERTIFIED_WITH_FINDINGS if findings else Verdict.CERTIFIED

    steps = [
        p for p in (state.case_provenance, state.execution_provenance, state.verdict_provenance) if p
    ]
    costs = [p.total_cost_usd for p in steps if p.total_cost_usd is not None]
    return CertificationReport(
        target=TargetMetadata(path=str(target)),
        findings=findings,
        verdict=verdict,
        provenance=RunProvenance(
            session_id=state.verdict_provenance.session_id if state.verdict_provenance else "",
            num_turns=sum(p.num_turns for p in steps),
            total_cost_usd=sum(costs) if costs else None,
            terminal_reason=(
                state.verdict_provenance.terminal_reason if state.verdict_provenance else None
            ),
        ),
    )


async def run_audit(
    target: Path, run_id: str, case: Case | None = None, max_budget_usd: float = 0.50
) -> CertificationReport:
    """Run generator -> executor -> judge as one persisted pipeline.

    On a fresh run_id, this behaves like calling run_case_generator,
    run_case_executor, and run_judge in sequence. On a run_id whose
    AUDITS_DIR/<run_id>/state.json already has a step's final result
    on disk, that step is skipped (no LLM call); the one step that has a
    captured session_id but no final result — the step that was running
    when a prior process died — is resumed via `resume=` instead of
    restarted. See the module-level comment above this section for the
    full design.

    `case`, when given, skips the generator step outright, storing it in
    state immediately. Used by the CLI's --case-json flag and by
    checks/m7.py: the real case-generator's output category is
    non-deterministic, and the crash/resume test needs a reliable,
    reproducible step to kill mid-flight rather than whatever the
    generator happens to invent.
    """
    state = load_audit_state(run_id) or AuditState(run_id=run_id, target=str(target))
    if state.case is None and case is not None:
        state.case = case
        _save_audit_state(state)

    if state.case is None:
        await _run_or_resume_generator(target, state, max_budget_usd)
    if state.execution is None:
        await _run_or_resume_executor(target, state, max_budget_usd)
    if state.verdict is None:
        await _run_or_resume_judge(state, max_budget_usd)
    if state.report is None:
        state.report = _assemble_report(target, state)
        _save_audit_state(state)

    return state.report


async def run_judge_fork(
    judge_session_id: str, max_budget_usd: float = 0.50
) -> tuple[CaseVerdict, SubagentRun]:
    """Fork a completed judge session (fork_session=True) onto a brand-new
    session_id and ask it to re-examine the same case/evidence — still
    present in the forked session's inherited history, so it doesn't need
    to be passed again — and issue a fresh verdict. The original
    judge_session_id is left untouched.

    Because the severity-rubric skill is read fresh at invocation (it's
    not baked into the transcript being resumed), editing
    .claude/skills/severity-rubric/SKILL.md between the original judge run
    and this fork changes the fork's output. This is M7's "fork an audit
    to re-run a single branch without redoing the whole thing" — the
    branch here is judgement, and generator/executor are never touched.
    """
    return await run_judge(
        max_budget_usd=max_budget_usd,
        resume=judge_session_id,
        fork_session=True,
        invocation_prompt=JUDGE_REEXAMINE_PROMPT,
    )
