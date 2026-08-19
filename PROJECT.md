# AgentAudit — a certification harness for LLM agents

A tool that points at an agent repository, generates adversarial cases, runs them
against the target, judges the results, and emits a schema-valid certification
report. Built on the Claude Agent SDK (Python).

The project exists to teach the Agent SDK end to end. Every milestone exercises a
specific part of the SDK and ends in a check that either passes or fails.

---

## Ground rules

1. **One milestone per session.** `/clear` between them.
2. **No milestone is done until its check script exits 0.** "It ran and looked
   fine" is not done.
3. **Fetch the docs before implementing.** The SDK moves fast; recalled option
   names are frequently stale. Every page has a markdown variant, e.g.
   `https://code.claude.com/docs/en/agent-sdk/hooks.md`. Full index at
   `https://code.claude.com/docs/llms.txt`.
4. **Plan mode first** for anything touching more than one file.
5. **Adversarial review before commit**: a subagent reviews the diff against this
   file's acceptance criteria and reports only correctness gaps.

---

## Layout

```
agent-audit/
  src/agentaudit/
    __init__.py
    cli.py              # entrypoint
    harness.py          # SDK options assembly, the run loop
    tools/              # @tool definitions -> in-process MCP server
    agents/             # AgentDefinition subagents
    hooks/              # PreToolUse / PostToolUse handlers
    schema.py           # Pydantic models for findings + report
    telemetry.py        # cost, turns, OTel
  targets/              # sample agent repos to audit (fixtures)
  checks/               # per-milestone verification scripts
  .claude/
    CLAUDE.md
    skills/
    settings.json
  PROJECT.md
```

---

## M1 — The loop, observed

**SDK surface:** `query()` vs `ClaudeSDKClient`, message stream, `AssistantMessage`,
`ResultMessage`, `terminal_reason`, `total_cost_usd`, `model_usage`.

**Build:** a CLI `agentaudit inspect <repo-path>` that runs one agent turn over a
target repository and prints a prose summary of its agent architecture — what
framework, what tools, where the entrypoint is.

Instrument from the first line: every run appends a JSONL record with
`session_id`, turn count, per-model token usage, estimated cost, and
`terminal_reason`.

**Docs:** `agent-sdk/quickstart.md`, `agent-sdk/agent-loop.md`,
`agent-sdk/python.md`, `agent-sdk/cost-tracking.md`

**Check — `checks/m1.py`:**
- Runs `inspect` against `targets/simple-langgraph-agent/`.
- Asserts a JSONL record was written with a non-null `total_cost_usd` and
  `terminal_reason == "completed"`.
- Asserts the run used fewer than N turns.

**Watch for:** `ResultMessage.usage` covers only the top-level loop and excludes
subagents. Use `model_usage` for whole-tree accounting. Note now, matters at M5.

---

## M2 — Constrain the harness

**SDK surface:** `system_prompt` (custom string vs `{"type": "preset", "preset":
"claude_code"}` with `append`), `setting_sources`, `allowed_tools`,
`disallowed_tools`, `cwd`, `add_dirs`, `max_turns`, `max_budget_usd`,
`permission_mode="plan"`.

**Build:** make `inspect` provably read-only and cost-bounded. Decide explicitly
whether you inherit Claude Code's system prompt or write your own — write down the
reasoning in a comment, because it's the single biggest behavioural lever in the
SDK.

**Docs:** `agent-sdk/permissions.md`,
`agent-sdk/modifying-system-prompts.md`, `agent-sdk/claude-code-features.md`

**Check — `checks/m2.py`:**
- Plant a target repo containing a file the agent would plausibly want to "fix".
- Run `inspect`; assert the target directory's git status is clean afterwards.
- Assert a run with `max_budget_usd` set to an absurdly low value terminates with
  `subtype == "error_max_budget_usd"`.

**Trap:** `allowed_tools` auto-approves without prompting — it does **not**
restrict Claude to only those tools. Unlisted tools fall through to
`permission_mode` and `can_use_tool`. Blocking requires `disallowed_tools`. A bare
name like `"Bash"` removes the tool entirely; a scoped rule like `"Bash(rm *)"`
leaves it available and denies matching calls in every mode.

---

## M3 — Custom tools

**SDK surface:** `@tool` decorator, `create_sdk_mcp_server()`, `mcp_servers=`,
tool naming `mcp__<server>__<tool>`, `ToolAnnotations`.

**Build:** an in-process MCP server exposing:
- `load_target_spec(path)` — read the target's declared capabilities
- `execute_case_against_target(case_id, input)` — run one case, capture output
- `record_result(case_id, outcome, evidence)` — append to the run ledger

`execute_case_against_target` is the dangerous one. Stub the execution for now;
M6 sandboxes it.

**Docs:** `agent-sdk/custom-tools.md`, `agent-sdk/mcp.md`

**Check — `checks/m3.py`:** a scripted run where the only path to the answer is
through your tools; assert all three were called, in order, with valid arguments.

---

## M4 — Structured output

**SDK surface:** `output_format={"type": "json_schema", "schema": ...}`,
`ResultMessage.structured_output`, `error_max_structured_output_retries`.

**Build:** `schema.py` with Pydantic models — `Finding` (severity, category,
evidence, reproduction steps, confidence) and `CertificationReport` (target
metadata, findings, verdict, run provenance). Generate JSON Schema from Pydantic;
don't hand-write it twice.

**Docs:** `agent-sdk/structured-outputs.md`

**Check — `checks/m4.py`:** run against a target with a known planted flaw; assert
the result parses into `CertificationReport` and contains at least one `Finding`
with the expected category.

---

## M5 — Subagents

**SDK surface:** `agents={...}`, `AgentDefinition`, per-agent `model`, `tools`,
`disallowedTools`, `maxTurns`, `background`.

**Build:** three subagents.
- `case-generator` — reads the target, proposes adversarial cases. Haiku.
- `case-executor` — runs cases via the M3 tools. Haiku.
- `judge` — reads results only, never the target source, and rules on each case.
  Opus. Isolation is the point: the judge must not see the reasoning that
  produced the case.

**Docs:** `agent-sdk/subagents.md`

**Check — `checks/m5.py`:**
- Assert all three ran (parse the message stream for subagent activity).
- Assert `model_usage` shows more than one model.
- Assert judge verdicts are reproducible across two runs on a fixed case set
  (allow a tolerance; record the variance — this number is itself a finding about
  your own harness).

**Trap:** `AgentDefinition` uses camelCase (`disallowedTools`, `maxTurns`,
`permissionMode`) while `ClaudeAgentOptions` uses snake_case. It's a dataclass, so
snake_case raises `TypeError` at construction.

---

## M6 — Permissions and hooks

**SDK surface:** `can_use_tool` callback, `PermissionResultAllow` /
`PermissionResultDeny`, `hooks={HookEvent: [HookMatcher(...)]}`, `PreToolUse`,
`PostToolUse`, `permission_mode` values, `sandbox=SandboxSettings(...)`.

**Build:** three layers, in this order:
1. Declarative deny rules — the hard stops.
2. A `PreToolUse` hook that logs **every** tool call to the audit ledger.
3. `can_use_tool` for the judgement calls that need context.

Then sandbox target execution: no network, no writes outside a scratch directory.

**Docs:** `agent-sdk/hooks.md`, `agent-sdk/permissions.md`,
`agent-sdk/secure-deployment.md`, `sandboxing.md`

**Check — `checks/m6.py`:**
- A target repo that attempts an outbound HTTP call and a write to `/etc` — assert
  both are blocked and both appear in the ledger with a denial reason.
- Assert the ledger's tool-call count matches the message stream's tool-call count
  exactly. Any gap means something ran unlogged.

**The distinction that matters:** `can_use_tool` fires *only* when the permission
flow falls through to a prompt. Calls auto-approved by `allowed_tools`, an allow
rule, or the permission mode never reach it. To gate every call, you need the
`PreToolUse` hook. Get this wrong and your audit trail has holes.

---

## M7 — Sessions, memory, packaging

**SDK surface:** `resume`, `fork_session`, `resume_session_at`, `session_store`,
`enable_file_checkpointing` + `rewind_files()`, `skills=`, `plugins=`,
`setting_sources=["project"]` for CLAUDE.md loading.

**Build:**
- Long audits survive a restart: persist `session_id`, resume on relaunch.
- Fork an audit to re-run a single branch without redoing the whole thing.
- Move the severity rubric out of code into `.claude/skills/severity-rubric/SKILL.md`
  so it's editable without a deploy.
- Package the whole harness as a plugin.

**Docs:** `agent-sdk/sessions.md`, `agent-sdk/session-storage.md`,
`agent-sdk/skills.md`, `agent-sdk/plugins.md`, `agent-sdk/file-checkpointing.md`

**Check — `checks/m7.py`:** start an audit, kill the process mid-run, relaunch,
assert it resumes and completes with a coherent report. Then edit the rubric file
and assert the verdict changes accordingly.

---

## M8 — Production

**SDK surface:** OpenTelemetry export, hosting architecture (subprocess model,
session persistence, multi-tenant isolation), non-interactive CI invocation.

**Build:** containerize it, export traces and cost metrics, and wire it into GitHub
Actions so it comments a certification verdict on PRs in a target repo.

**Docs:** `agent-sdk/observability.md`, `agent-sdk/hosting.md`,
`agent-sdk/secure-deployment.md`, `github-actions.md`

**Check — `checks/m8.py`:** end-to-end run inside the container against a fixture
repo, asserting traces reach a local collector and the exit code reflects the
verdict.

---

## Open design questions

Decide these deliberately rather than by default. Worth thinking through before
the milestone that forces the answer.

- **M2:** Claude Code preset system prompt, or fully custom? The preset carries a
  lot of coding-agent behaviour you may not want in an auditor.
- **M4:** does a finding include a suggested fix? Auditors that propose fixes get
  read differently from auditors that only report.
- **M5:** how does the judge avoid rewarding cases the generator wrote to be
  easy? This is the real research question in the project.
- **M6:** what's the trust boundary? Auditing an untrusted repo is meaningfully
  different from auditing your own.
