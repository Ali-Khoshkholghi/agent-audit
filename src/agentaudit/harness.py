"""All ClaudeAgentOptions construction lives here — nowhere else in this
codebase should instantiate ClaudeAgentOptions directly.
"""
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultError,
    ResultMessage,
    TextBlock,
    query,
)

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
        # A single-shot query() raises after a terminal error result
        # (error_max_turns, error_max_budget_usd, ...) instead of always
        # yielding it as a ResultMessage first. e.data is that result's raw
        # payload, so reconstruct the ResultMessage from it rather than
        # losing the outcome (and the telemetry record) to an unhandled
        # crash.
        if result is None:
            data = e.data
            result = ResultMessage(
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

    if result is None:
        raise RuntimeError("query() stream ended without a ResultMessage")

    return "\n".join(summary_parts), result
