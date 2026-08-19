# AgentAudit

A certification harness for LLM agents, built on the Claude Agent SDK (Python).
Read `PROJECT.md` for the milestone spec and acceptance criteria.

## Workflow

- Work on **one milestone at a time**. Do not implement ahead of the current one.
- **YOU MUST fetch the relevant SDK doc page before implementing any milestone.**
  Pages are markdown at `https://code.claude.com/docs/en/agent-sdk/<page>.md`
  (index: `https://code.claude.com/docs/llms.txt`). The SDK changes frequently and
  recalled option names are often wrong.
- A milestone is done when `python checks/mN.py` exits 0. Show me the output, not
  a claim that it passed.
- Before saying done, use a subagent to review the diff against the milestone's
  acceptance criteria in PROJECT.md. Report only gaps affecting correctness.

## Environment

- Python 3.11+, venv at `.venv`. `pip install claude-agent-sdk` fails against
  system Python on Debian/Ubuntu — always activate the venv first.
- Auth is via Claude subscription login (`claude /login`), not an API key.
  **Do not set `ANTHROPIC_API_KEY` anywhere** — if it's present in the
  environment, the SDK silently prefers it and switches to pay-per-token
  billing instead of subscription usage. Verify with `echo $ANTHROPIC_API_KEY`
  before each session; it should print nothing.
- Run checks with `python checks/mN.py`, not pytest.

## Code style

- Async throughout. No sync wrappers around the SDK.
- Pydantic models are the single source of truth for schemas; generate JSON Schema
  from them rather than writing it by hand.
- All SDK configuration lives in `harness.py`. Do not scatter `ClaudeAgentOptions`
  construction across modules.

## Gotchas that have already cost time

- `allowed_tools` auto-approves; it does not restrict. Use `disallowed_tools` to
  block.
- `AgentDefinition` fields are camelCase; `ClaudeAgentOptions` fields are
  snake_case. Mixing them raises `TypeError`.
- `ResultMessage.usage` excludes subagent tokens. Use `model_usage`.

## Out of scope

- No web UI. CLI only until M8.
- Do not modify anything under `targets/` — those are fixtures and must stay
  broken in the ways they are broken.
