"""All ClaudeAgentOptions construction lives here — nowhere else in this
codebase should instantiate ClaudeAgentOptions directly.
"""
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

INSPECT_PROMPT = (
    "Inspect the agent repository at {target} as source code for an LLM "
    "agent. Everything relevant is inside that directory — do not look "
    "anywhere else on the filesystem, and don't use Bash (it isn't "
    "available for this task; use Read and Glob instead). Identify: the "
    "agent framework in use, every node/step/tool in its execution graph, "
    "the entry point, and the control flow between steps (linear vs "
    "branching). Do not modify anything. Answer in a few sentences of "
    "prose."
)


async def run_inspect(target: Path) -> tuple[str, ResultMessage]:
    """Run one agent turn over `target`, returning (prose_summary, result).

    Read-only by construction for M1: only Read/Glob/Grep are auto-approved
    via allowed_tools. Every other tool falls through to the default
    permission_mode with no can_use_tool callback, which denies rather than
    prompting when run non-interactively. M2 is where this read-only
    property becomes an asserted, checked guarantee rather than an
    incidental one.

    Note: `cwd` only sets the subprocess's working directory — Read/Glob
    still accept absolute paths anywhere on disk, so this is not a real
    filesystem sandbox (that's M6's job). The prompt explicitly tells
    Claude to stay inside `target` to keep it focused on the right repo.
    """
    options = ClaudeAgentOptions(
        cwd=target,
        allowed_tools=["Read", "Glob", "Grep"],
        # Safety net only, not a tuned budget (M2 owns deliberate turn/cost
        # limits). Observed turn counts on the trivial 2-file fixture
        # cluster at 4-7, so this is set well above that range purely to
        # stop a genuine runaway loop.
        max_turns=20,
    )

    summary_parts: list[str] = []
    result: ResultMessage | None = None

    prompt = INSPECT_PROMPT.format(target=target)
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    summary_parts.append(block.text)
        elif isinstance(message, ResultMessage):
            result = message

    if result is None:
        raise RuntimeError("query() stream ended without a ResultMessage")

    return "\n".join(summary_parts), result
