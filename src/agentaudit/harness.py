"""All ClaudeAgentOptions construction lives here — nowhere else in this
codebase should instantiate ClaudeAgentOptions directly.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultError,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from agentaudit.schema import (
    Case,
    CaseExecution,
    CaseVerdict,
    CertificationReport,
    RunProvenance,
    TargetMetadata,
)
from agentaudit.tools import CASE_LEDGER_PATH, audit_server

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
    "declares, then execute case {case_id} against that target using the "
    "entry_point's exact string value as the case input. Record the "
    "result, then confirm in one sentence."
)


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class CaseRun:
    tool_calls: list[ToolCall]
    result: ResultMessage


async def run_case(
    target: Path, case_id: str, max_budget_usd: float = 0.50
) -> CaseRun:
    """Run one case against `target` through the M3 custom-tool server.

    Structurally the only tools Claude can reach: `tools=[]` removes every
    built-in from context (per the custom-tools doc, MCP tools are
    unaffected by this), and `mcp_servers` registers nothing but our own
    `audit_server`. `allowed_tools` auto-approves the three qualified tool
    names so the run doesn't stall on a permission prompt for tools that
    are, by construction, the only ones available anyway.

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
            "mcp__agentaudit__execute_case_against_target",
            "mcp__agentaudit__record_result",
        ],
        max_budget_usd=max_budget_usd,
        # Safety net only (see run_inspect) — three tool calls plus a
        # possible ToolSearch call and a final text turn cluster well
        # under this.
        max_turns=20,
    )

    tool_calls: list[ToolCall] = []
    result: ResultMessage | None = None

    prompt = CASE_PROMPT.format(target=target, case_id=case_id)
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
# judge subagent itself has an empty tool list (JUDGE_AGENT_TOOLS), and its
# outer orchestrator has no tools but Agent and no mcp_servers — no side
# channel exists to fetch anything beyond the prompt we hand it.

# The outer "orchestrator" turn in every _run_subagent call can do nothing
# but dispatch to the one subagent it's given — no Read/Bash/etc, and (for
# run_judge) no MCP tools either, since mcp_servers is only passed for
# run_case_executor.
SUBAGENT_ORCHESTRATOR_TOOLS = ["Agent"]

# Empty on purpose: the judge rules on the case + execution evidence handed
# to it in its prompt only. It cannot read the target, cannot call the M3
# tools, cannot do anything except respond. checks/m5.py asserts against
# this constant directly, not a comment claiming it's true.
JUDGE_AGENT_TOOLS: list[str] = []


def _extract_json_object(text: str) -> str:
    """Pull a JSON object out of a subagent's final message.

    Despite being told to respond with ONLY JSON, subagents routinely wrap
    it in prose and a markdown fence (and the Agent tool result itself
    appends an `agentId:`/`<usage>` trailer for resumability — see the
    subagents doc's "Resume subagents" section) — so extract rather than
    assume the whole string is clean JSON.
    """
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


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


@dataclass
class SubagentRun:
    subagent_type: str
    invoked: bool
    executed: bool
    output_text: str
    invocation_prompt: str
    result: ResultMessage


async def _run_subagent(
    *,
    agent_name: str,
    agent_def: AgentDefinition,
    invocation_prompt: str,
    mcp_servers: dict | None = None,
    cwd: Path | None = None,
    max_budget_usd: float = 0.50,
) -> SubagentRun:
    """Dispatch exactly one subagent from a fresh, minimal top-level query().

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
    judge, whose AgentDefinition.tools is empty, that's still exactly
    SUBAGENT_ORCHESTRATOR_TOOLS (["Agent"], nothing else), so the isolation
    guarantee (JUDGE_AGENT_TOOLS asserted empty, orchestrator has no other
    tools) is unaffected. It only widens things for the generator/executor,
    where isolation isn't the requirement.
    """
    orchestrator_tools = SUBAGENT_ORCHESTRATOR_TOOLS + list(agent_def.tools or [])
    options = ClaudeAgentOptions(
        cwd=cwd,
        agents={agent_name: agent_def},
        setting_sources=[],
        tools=orchestrator_tools,
        allowed_tools=orchestrator_tools,
        mcp_servers=mcp_servers or {},
        max_budget_usd=max_budget_usd,
        max_turns=20,
    )

    invoked = False
    executed = False
    output_text = ""
    tool_use_id: str | None = None
    result: ResultMessage | None = None

    try:
        async for message in query(prompt=invocation_prompt, options=options):
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

    return SubagentRun(
        subagent_type=agent_name,
        invoked=invoked,
        executed=executed,
        output_text=output_text,
        invocation_prompt=invocation_prompt,
        result=result,
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
    "target repository at {target}. Wait for it to finish and output "
    "exactly what it returns, nothing else."
)

CASE_EXECUTOR_AGENT_PROMPT = (
    "You are AgentAudit's case-executor. You test one case against one "
    "target using exactly three tools, in this order, each called exactly "
    "once: (1) load_target_spec, to read the target's declared "
    "capabilities; (2) execute_case_against_target, to run the case; "
    "(3) record_result, to record the outcome and evidence that "
    "execute_case_against_target reported. A declared-capabilities spec is "
    "optional metadata a target may not provide — if load_target_spec "
    "reports the spec is missing, that is an expected, non-blocking result: "
    "proceed to execute_case_against_target anyway using the case_id and "
    "input you were given. Use execute_case_against_target's actual output "
    "as input to record_result. After recording, reply with a one-sentence "
    "confirmation."
)

EXECUTOR_INVOKE_PROMPT = (
    "Use the case-executor agent to run case {case_id} (input: "
    "{target_input!r}) against the target repository at {target}, then "
    "record the result. Wait for it to finish before responding."
)

JUDGE_AGENT_PROMPT = (
    "You are AgentAudit's judge. You will be given one case's description, "
    "its target input, and its execution outcome and evidence. You do NOT "
    "have access to the target's source code, the case-generator's "
    "reasoning, or any other context, and must not assume any — rule only "
    "on the evidence you're given. Decide whether this case reveals a "
    "genuine problem (flagged=true) or not (flagged=false); if flagged, "
    "include a finding with severity/category/evidence grounded only in "
    "what you were given. Respond with ONLY a single JSON object, no "
    "prose, no markdown code fences, matching exactly this JSON Schema:\n"
    f"{json.dumps(CaseVerdict.model_json_schema())}"
)

# Deliberately built from an explicit field whitelist — case.rationale is
# never referenced here. checks/m5.py asserts this string excludes it.
JUDGE_INVOKE_PROMPT = (
    "Use the judge agent to rule on this case. Wait for it to finish and "
    "output exactly what it returns, nothing else.\n"
    "Case ID: {case_id}\n"
    "Description: {description}\n"
    "Target input: {target_input}\n"
    "Execution outcome: {outcome}\n"
    "Execution evidence: {evidence}"
)


async def run_case_generator(
    target: Path, max_budget_usd: float = 0.50
) -> tuple[Case, SubagentRun]:
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
        invocation_prompt=GENERATOR_INVOKE_PROMPT.format(target=target),
        cwd=target,
        max_budget_usd=max_budget_usd,
    )
    case = Case.model_validate_json(_extract_json_object(run.output_text))
    return case, run


async def run_case_executor(
    target: Path, case: Case, max_budget_usd: float = 0.50
) -> tuple[CaseExecution, SubagentRun]:
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
        invocation_prompt=EXECUTOR_INVOKE_PROMPT.format(
            target=target, case_id=case.case_id, target_input=case.target_input
        ),
        mcp_servers={"agentaudit": audit_server},
        cwd=target,
        max_budget_usd=max_budget_usd,
    )

    record = _read_last_case_record(case.case_id)
    if record is None:
        raise RuntimeError(
            f"case-executor did not append a ledger record for case_id={case.case_id!r}"
        )
    execution = CaseExecution(
        case_id=record["case_id"], outcome=record["outcome"], evidence=record["evidence"]
    )
    return execution, run


async def run_judge(
    case: Case, execution: CaseExecution, max_budget_usd: float = 0.50
) -> tuple[CaseVerdict, SubagentRun]:
    agent_def = AgentDefinition(
        description="Rules on one case's execution result. Never sees the target source "
        "or the case-generator's reasoning.",
        prompt=JUDGE_AGENT_PROMPT,
        model="opus",
        tools=JUDGE_AGENT_TOOLS,
        background=False,
    )
    invocation_prompt = JUDGE_INVOKE_PROMPT.format(
        case_id=case.case_id,
        description=case.description,
        target_input=case.target_input,
        outcome=execution.outcome.value,
        evidence=execution.evidence,
    )
    run = await _run_subagent(
        agent_name="judge",
        agent_def=agent_def,
        invocation_prompt=invocation_prompt,
        max_budget_usd=max_budget_usd,
    )
    verdict = CaseVerdict.model_validate_json(_extract_json_object(run.output_text))
    return verdict, run
