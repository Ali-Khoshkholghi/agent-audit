# AgentAudit

A certification harness for LLM agents, built end-to-end on the [Claude Agent
SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python).

This project exists to learn the Agent SDK properly — not by reading the
docs, but by building something with real stakes on every surface: the query
loop, permissions, custom tools, structured output, isolated subagents,
sandboxed execution, session durability, and a containerized deployment. Each
milestone below exercises one part of the SDK and ends in a check script that
either passes or fails — no "it looked fine."

## What it does

AgentAudit points at a target agent repository, generates adversarial test
cases against it, executes those cases in a sandboxed environment, judges the
results, and emits a schema-valid `CertificationReport` — a verdict plus a
list of `Finding`s (severity, category, evidence, reproduction steps).

The pipeline runs as three isolated agents:

- **case-generator** — reads the target and proposes adversarial cases (Haiku)
- **case-executor** — runs each case through sandboxed tools (Haiku)
- **judge** — sees only results, never the target source or the generator's
  reasoning, and rules on each case (Opus)

## Architecture: the 8 milestones

| # | Milestone | SDK surface |
|---|---|---|
| M1 | The loop, observed | `query()`, `ResultMessage`, `model_usage`, cost/turn telemetry |
| M2 | Constrain the harness | `system_prompt`, `allowed_tools`/`disallowed_tools`, `max_turns`, `max_budget_usd` |
| M3 | Custom tools | `@tool`, `create_sdk_mcp_server()`, in-process MCP server |
| M4 | Structured output | `output_format={"type": "json_schema", ...}`, Pydantic → JSON Schema |
| M5 | Subagents | `AgentDefinition`, per-agent models/tools, isolation between generator and judge |
| M6 | Permissions and hooks | `can_use_tool`, `PreToolUse`/`PostToolUse` hooks, OS-level sandboxing (Seatbelt/bubblewrap) |
| M7 | Sessions, memory, packaging | `resume`, `fork_session`, skills, plugin packaging |
| M8 | Production | OTel export, containerization, GitHub Actions PR certification |

Full spec, acceptance criteria, and design notes for each milestone live in
[`PROJECT.md`](PROJECT.md).

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Auth is via a Claude subscription, not pay-per-token billing:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<token from `claude setup-token`>
```

**Do not set `ANTHROPIC_API_KEY`.** If present, the SDK silently prefers it
and switches to API billing instead of subscription usage.

For M8 (containerized runs, OTel export), you'll also need Docker and Docker
Compose — see `Dockerfile` and `docker-compose.m8.yml`.

## Running it

```bash
# Summarize a target agent repo's architecture (read-only, cost-bounded)
agentaudit inspect targets/simple-langgraph-agent

# Single read-only certification pass; exit code reflects the verdict
agentaudit certify targets/simple-langgraph-agent

# Full resumable generator -> executor -> judge pipeline
agentaudit audit targets/simple-langgraph-agent --run-id demo-1
```

`audit` persists state under `runs/`, so re-running with the same `--run-id`
resumes an interrupted pipeline instead of starting over.

## Running the checks

Each milestone has its own verification script — no pytest, just a script
that exits 0 or fails with a clear assertion:

```bash
python checks/m1.py   # query loop + telemetry
python checks/m2.py   # read-only, cost-bounded
python checks/m3.py   # custom MCP tool wiring
python checks/m4.py   # structured output parses into CertificationReport
python checks/m5.py   # subagent isolation, multi-model usage
python checks/m6.py   # sandboxing blocks network/filesystem escape
python checks/m7.py   # crash/resume, severity-rubric skill
python checks/m8.py   # containerized run, OTel traces, CI exit code
```

See [`PROJECT.md`](PROJECT.md) for what each check actually asserts and why.
