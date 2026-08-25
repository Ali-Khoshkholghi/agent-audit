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

**Known gap (post-M8, closed by M11b):** whether a case's `record_result`
outcome is "inconclusive" (never actually executed) vs "fail" (executed and
revealed the problem) was judgment-based — `CASE_EXECUTOR_AGENT_PROMPT`
instructed the executor subagent how to tell the two apart, but nothing
enforced it deterministically. Proved unreliable in practice exactly as this
note warned: the `model_name_format_validation_missing` case against
react-agent was labeled "fail" even though `execute_case_against_target`
reported `returncode=0` with empty stdout/stderr — react-agent's `graph.py`
has no `__main__`/stdin-reading code (M10), so nothing was ever actually
exercised. Fixed in `run_case_executor`: `execute_case_against_target` now
also writes a second, purely mechanical ledger
(`EXECUTION_LEDGER_PATH`/`executions.jsonl` in `tools/__init__.py`) of the
raw `SandboxResult` fields, never LLM-paraphrased. `harness._is_structurally_silent`
reads it back and deterministically forces outcome to `"inconclusive"`
whenever it's `"fail"` **or `"pass"`** but the matching execution record
shows a clean exit (`returncode==0`, no timeout) with literally no stdout
and no stderr — both labels, not just "fail", since a "pass" grounded in
zero observable evidence is equally unfounded. A non-zero returncode with
no output is deliberately *not* treated as silent — a crash with nothing
printed is still real evidence something happened. `cases.jsonl` is never
rewritten (forward-looking only); only the in-memory `CaseExecution`
returned to `run_judge`/`_assemble_report` is corrected. `run_case` (M3/M6's
separate direct-invocation path) needed no change — it never constructs a
`CaseExecution` or feeds a `CertificationReport`. See `checks/m11b.py`,
verified live: re-running `checks/m10.py`'s and `checks/m11.py`'s own live
`agentaudit audit` checks against the real react-agent clone now shows
`execution_evidence` beginning with `"Overridden by the harness: ..."`
where it previously would have silently accepted the subagent's `"fail"`/`"pass"`
label.

**Residual gap (caught in a follow-up review, accepted rather than fixed —
low likelihood, real complexity to close):** `_read_last_execution_record`
takes the *last* `executions.jsonl` line matching a `case_id`, same "last
line wins" pattern `_read_last_case_record` already used for `cases.jsonl`.
That's only correct if `execute_case_against_target` is called exactly once
per case — enforced today only by `CASE_EXECUTOR_AGENT_PROMPT` saying so in
prose, not deterministically. A case-executor that retried the tool call
(e.g. after a transient error) could leave the execution record the
override checks against pointing at a different attempt than the one
`record_result`'s outcome was actually based on. No evidence this has
happened in practice — every `case_id` observed in `executions.jsonl` so
far has exactly one record — and closing it properly would mean threading a
call identifier (e.g. `tool_use_id`) through both tools and matching on
that instead of `case_id` alone, which is more machinery than this fix's
actual, confirmed bug warranted. Revisit if retries are ever observed.

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

**Resolved trust boundary (decided deliberately, not by default):**
`sandbox=SandboxSettings(...)` only configures Claude Code's own built-in
Bash tool sandbox — its docstring says so explicitly ("Filesystem and
network restrictions are configured via permission rules, not via these
sandbox settings"). It has no effect on code executed inside our own
`@tool` handlers, and this harness never gives Claude a `Bash` tool. Real
sandboxing of target-repo code is built independently, at the OS level, in
`src/agentaudit/sandbox.py`:
- macOS: `sandbox-exec` (Seatbelt) — implemented and smoke-tested directly
  (confirmed: outbound network denied even by raw IP, `/etc` write denied,
  writes inside the scratch directory succeed).
- Linux/WSL2: `bubblewrap` — **confirmed working**, verified live on a real
  Debian VM (M10). Exercising it for the first time against a target under
  `/tmp` found one real bug: `--tmpfs /tmp` shadowed a target `cwd` living
  under `/tmp`, since a fresh empty tmpfs mounted there hid whatever
  `--ro-bind / /` had already exposed at that path. Fixed by explicitly
  re-binding `cwd` after the tmpfs mount, the same way `scratch_dir`
  already did. Post-fix, `checks/m6.py` passed with ground-truth
  confirmation, not just the harness's own self-report: the `/etc` write
  marker file genuinely did not exist on the real filesystem afterward,
  and the audit ledger's tool-call count matched the message stream's
  exactly (no unlogged calls). Both network and filesystem-write blocking
  held under real bubblewrap.
- Any other platform, or a missing backend binary, fails closed:
  `execute_case_against_target` refuses to run target code rather than
  falling back to unsandboxed execution.

This OS-level sandbox lives inside `execute_case_against_target`'s own
implementation, so it applies to every caller of that tool regardless of
which harness code path invokes it (M3's `run_case`, or M5's
`run_case_executor` subagent dispatch). The three SDK-level permission
layers above (deny rules / `PreToolUse` audit hook / `can_use_tool`
judgement), by contrast, are wired onto `run_case` only — that's the path
`checks/m6.py` exercises. `run_case_executor` keeps auto-approving all
three MCP tools as it did before M6; only its prompt changed, to pass the
now-required `target` argument.

**Known gap (post-M8, unfixed):** because `run_case_executor` never got M6's
`can_use_tool`/hook layering, there's no structural guarantee the subagent
actually called `execute_case_against_target` before `record_result` —
only the prompt says to. A subagent could in principle fabricate a ledger
entry without ever executing anything. Revisit if `run_case_executor` is
ever extended to enforce call order the way `run_case` does.

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

## M9 — Entry-point discovery without a spec file

**SDK surface:** none — pure Python/heuristics over files already on disk, not
new SDK surface, so no docs page was fetched for this milestone.

**Context:** confirmed live against `langchain-ai/react-agent` (M8's real-target
run): most real repos have no `agentaudit.spec.json`, so `load_target_spec`
always returned `is_error=True` for them and left the case-executor subagent to
*guess* an entry_point from common filenames on its own — unreliable, and the
direct cause of that run coming back `verdict=inconclusive` instead of actually
exercising the target.

**Build:** `load_target_spec` discovers a likely entry_point itself when no spec
exists, from real signals in priority order: `pyproject.toml`
(`project.scripts`/`console_scripts`) -> `langgraph.json` (`graphs`) ->
`package.json` (`main`) -> common filename conventions (`main.py`, `app.py`,
`agent.py`, `run.py`, `__main__.py`, `src/main.py`, `src/*/graph.py`) as the
last resort, codifying what the subagent used to improvise. A malformed file at
any tier falls through to the next rather than aborting discovery. Returns
`is_error=True` only when nothing resolves, naming exactly what was checked —
never fabricates a path.

**Check — `checks/m9.py`:** five checks, no mocking of `load_target_spec`
itself — the `langgraph.json` tier is proven against the real, live react-agent
clone; the fallback-convention tier against the real, in-repo
`simple-langgraph-agent` fixture; the `pyproject.toml` tier (plus priority
order over a present `langgraph.json`) and the `package.json` tier against
synthetic temp fixtures (no real repo in this project exercises either); and a
genuine-failure case asserts an honest `is_error` reason rather than a
fabricated filename.

**Watch for:** discovering a real entry point does not mean it will run.
`react_agent/graph.py` imports its own package (`from react_agent.context
import Context`), which only resolves with the package installed or
`PYTHONPATH`/cwd set up for it — `execute_case_against_target`'s
`[sys.executable, script_path]` model will likely still hit
`ModuleNotFoundError`. That's a distinct execution-semantics gap (how a
discovered entry point actually gets invoked, and how a case's adversarial
`target_input` reaches it), already handled honestly as
`outcome=inconclusive`/`fail` per the M8 fix rather than a crash, but not
something M9 attempts to close. M9's own live end-to-end verification against
react-agent surfaced a sharper version of this — see M10 below.

---

## M10 — Wire target_input into actual execution via stdin

**SDK surface:** none — subprocess/stdin plumbing and prompt wording, not new
SDK surface.

**Context:** logged from M9's own live verification run against react-agent:
the case-generator proposed a case whose `target_input` was a Python
expression to execute —
`graph.ainvoke({'messages': [('user', 'test query')]}, context=Context(model='gpt-4'))`
— correct per `Case`'s own schema (`target_input`: "the literal input to feed
the target when executing this case"). The case-executor then passed that
string straight through as `execute_case_against_target`'s single, overloaded
`input` argument, which the tool tried to resolve as a file path and
correctly refused (`entry_point "graph.ainvoke(...)" not found`) — no crash,
no fabrication, the M8 fix held, but `load_target_spec`'s discovered
`src/react_agent/graph.py` was never used. `EXECUTOR_INVOKE_PROMPT` handed the
executor `target_input` framed as "the input" for that tool call, competing
with — and here winning over — the tool's own description telling it to use
the spec's `entry_point`. Which one a Haiku case-executor actually followed
was model-judgment-dependent, not deterministic (an earlier live run had it
guessing filenames instead).

**Decision (yours):** stdin, piped by `execute_case_against_target` itself
into the *discovered* entry_point — never executed directly, never
imported/called in-process (that would bypass M6's subprocess sandbox). The
harness picks one consistent method rather than letting the case-generator
decide per-target.

**Build:** `execute_case_against_target`'s input schema splits the one
overloaded `input` field into `entry_point` (which file to run — same
resolution/traversal-guard logic as before) and `stdin_input` (what to feed
it, piped straight to `sandbox.run_sandboxed`'s new `stdin` parameter, which
is always passed explicitly — even empty — so a subprocess never silently
inherits the harness's own real stdin). `EXECUTOR_INVOKE_PROMPT` and
`CASE_EXECUTOR_AGENT_PROMPT` reworded so `target_input` is explicitly "data to
pipe to stdin once the real entry_point is found," never something to execute
directly; `CASE_PROMPT` (M3/M6's `run_case`, which has no `target_input`
concept) updated only because the tool schema it calls changed underneath it.

**Check — `checks/m10.py`:** three checks. (1) A new purpose-built fixture,
`targets/stdin-echo-target/` (same precedent as M6's `network-probe-target`),
whose entry_point reads stdin and echoes it back in a stable marker line —
deterministically proves stdin reaches a running target's own logic through
the real OS sandbox, not just that our Python called `subprocess.run(input=
...)`. (2) Wiring correctness against the real, live react-agent repo: the
right discovered `entry_point` is used and the right byte count is piped,
without asserting a specific pass/fail outcome (see Watch for). (3) A live
`agentaudit audit` sanity run against react-agent, asserting a coherent
report and, if still `inconclusive`, that it's no longer for the old
entry_point-not-found reason.

**Watch for:** discovering and correctly piping into an entry_point doesn't
guarantee a meaningful outcome. `react_agent/graph.py` has no `__main__` or
any stdin-reading code at all — running it can never observably *consume*
what's piped to it, no matter how correct the plumbing is. And even a target
that did read stdin would still need its real dependencies installed
(`langgraph`/`langchain_openai` aren't in `.venv`, confirmed) and, for an
LLM-based agent, real network access to call a model — which M6's sandbox
deliberately denies. A real pass/fail on react-agent's actual planted flaw is
therefore structurally unreachable in this environment; `inconclusive` staying
the live outcome after M10 is expected and correct, not a regression — the
fix is that it's now inconclusive for an honest environment reason instead of
a wiring bug.

---

## M11 — Per-target ephemeral dependency installation

**SDK surface:** none — subprocess/venv plumbing, not new Agent SDK surface, same
as M9/M10.

**Context:** M9/M10 fixed entry-point discovery and stdin wiring against the real
`langchain-ai/react-agent` clone, but both left the same gap open, documented in
M10's "Watch for": `react_agent/graph.py` can't even be imported —
`langgraph`/`langchain_openai` aren't installed anywhere the harness can see, and
`graph.py` itself does `from react_agent.context import Context`, which only
resolves once `react_agent`'s own package is installed too.

**Scope adjustment (decided against the real target, not assumed):** confirmed
live that react-agent ships `pyproject.toml` with PEP 621
`[project.dependencies]` and a setuptools `[build-system]` — no
`requirements.txt` anywhere in the repo. v1 supports `pyproject.toml` only.

**Build:** `agentaudit.sandbox.install_target_dependencies(target, scratch_dir,
timeout=)` — before running a case's entry_point,
`execute_case_against_target` calls this to build a fresh, disposable venv
under the case's `scratch_dir` and installs the target itself into it (`pip
install <copy-of-target>`, not just its parsed dependency strings — this
resolves `[project.dependencies]` *and* builds/installs the target's own
package via its declared `[build-system]` backend in one step, which is what
actually fixes the `react_agent.context` import, not just the third-party
packages). No new MCP tool, no prompt changes — baked into the existing tool's
implementation, same as M6's sandboxing and M9's discovery, deliberately to
avoid reintroducing M10's exact failure class (an LLM-facing step whose framing
competed with a tool's own contract).

A target with no `pyproject.toml` at all is unaffected — that's a skip signal,
not a failure, and execution proceeds with the harness's own interpreter
exactly as before M11. Every other non-ok result (malformed manifest, install
failed, install timed out) reports `is_error=True` with the concrete reason,
flowing into `CaseExecution.outcome=Outcome.INCONCLUSIVE` through the same
channel M9/M10 already established.

`run_sandboxed` gained two parameters to support this: `allow_network` (Linux:
conditionally omits `--unshare-net`; macOS: swaps in a second Seatbelt profile
with the network deny removed, filesystem rules unchanged) and `env` (passed
straight to `subprocess.run`; `None`, the default and every pre-M11 caller's
behavior, means unchanged full-parent-env passthrough). Target *execution*
itself stays `allow_network=False` throughout — M11 only opens network for the
install step, never for the target's own code, which is still fully
network-denied per M6.

**Network scoping (deliberate, not an oversight):** this environment has
neither `iptables`/`nft` nor passwordless root (confirmed by testing:
`sudo -n` fails, no firewall tooling on PATH), so there is no OS-level
per-host firewall available to build inside the sandbox's network namespace —
bubblewrap's own network control is all-or-nothing. What scoping there is
comes from pip itself: `--index-url` pinned to `pypi.org` with matching
`--trusted-host` entries, nothing else configured. This is a soft, pip-level
boundary, not an OS-enforced one — a malicious sdist's build script could in
principle still reach other hosts during its own build step. Same
honest-tradeoff posture as M6's `SandboxSettings` note above: written down
deliberately rather than implied to be stronger than it is.

**The compounding risk this alone doesn't tell you (caught in a follow-up
review, fixed the same session, not left as "just document it"):** target
execution and venv creation also get read access to the *whole* host
filesystem (`--ro-bind / /` / blanket `(allow file-read*)`) — fine for them,
since neither has network, so even a compromised entry_point can't
exfiltrate what it reads. `pip install` of an untrusted package is
different: it genuinely executes attacker-controlled code (`setup.py`/PEP
517 build hooks), and with both broad read *and* open network true at the
same time, that combination — not "might fetch from an unexpected host" —
is the real exposure: it could read anything the harness process can (SSH
keys, cloud credentials, this repo's own source) and send it anywhere.
Fixed: the `pip install` call (only that one — venv creation stays
network-denied, so its broad read is unchanged and fine) now also passes
`read_paths` to `run_sandboxed`, replacing read-everywhere with a curated
allowlist — this process's own Python installation
(`sys.base_prefix`/`base_exec_prefix`, resolved at runtime rather than
hardcoded, since it may not live under `/usr`: this environment's is a
conda env under `$HOME`), the base OS directories needed to exec anything
at all (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64` — shipped software, not
secrets), and a handful of specific `/etc` files needed for DNS/TLS (not
all of `/etc`, which also holds real secrets like SSH host keys). Verified
directly, not just by inspection: `checks/m11.py`'s 7th check plants a fake
secret file outside the allowlist and asserts the sandboxed `pip install`
posture genuinely can't read it (`cat` fails with "No such file or
directory"). This narrows what a malicious build script could steal even
though network stays open; it does not, and cannot within this
environment's constraints (no iptables/root), close the network side
itself — that half remains the soft, pip-level boundary described above.

**A real bwrap bug found building the narrowed read scope, worth recording
because the failure mode was misleading:** binding each `read_paths` entry
at its `.resolve()`d path broke pip install with `bwrap: execvp
.../venv/bin/python: No such file or directory` — a nested-namespace
mount-table corruption, not a missing-file problem. On a usrmerge system
(this Debian box), `/bin`, `/sbin`, `/lib`, `/lib64` are symlinks into
`/usr`; resolving them before binding collapsed their *destination* paths
onto `/usr/bin` etc. too, nesting those binds under the already-bound
`/usr` mountpoint. That nested-under-nested pattern corrupted bwrap's mount
table badly enough to silently break a *different*, unrelated later bind
(the scratch_dir bind that's supposed to expose `venv/bin/python`) — with
an error message that pointed at the venv, not at the real cause. Confirmed
by manually bisecting the exact bind list down to the specific redundant
entries. Fixed by binding at the literal requested path instead of the
resolved one: the kernel follows the symlink on the *source* side normally,
and `/bin`/`/usr` end up as siblings in the new namespace rather than one
nested inside the other.

**Real bugs found exercising this against react-agent and reviewing the diff
(not anticipated by the plan):**
- `pip install <target_dir>` directly against the real target failed with
  `error: could not create 'react_agent.egg-info': Read-only file system` —
  setuptools writes its `<pkg>.egg-info/` build metadata directly into the
  source directory it's given, which is standard pip behavior for a
  local-path install, not something this harness opted into. `target` is
  read-only in the sandbox (the harness must never mutate the thing under
  audit), so the fix installs from a throwaway copy of `target` under
  `scratch_dir/install_src` instead — the real target directory is never
  written to.
- That copy wasn't being cleaned up on any path (only `venv_dir` was).
  `install_target_dependencies` now wraps the create-venv-and-install
  sequence in `try/finally` and always removes `install_src` before
  returning, success or failure alike — it's a purely internal
  implementation detail, never needed by the caller, unlike `venv_dir`,
  which the caller needs on success and is responsible for removing itself
  once the case finishes executing.
- Skip-vs-failure was originally discriminated by substring-matching the
  human-readable `reason` text (`"no pyproject.toml found" not in
  install.reason`) in `execute_case_against_target` — the same field that
  also carries raw pip/venv `stderr` tails for real failures. A subagent
  review flagged the latent coupling: a future error message that happened
  to contain that phrase would silently misclassify a genuine install
  failure as "nothing to install," letting execution proceed with
  `sys.executable` against an unbuilt target while masking that
  dependencies were never installed. Fixed with a dedicated `skip: bool`
  field on `DependencyInstallResult` — a structural signal, not prose.
- The same review caught that `timeout` was passed separately to venv
  creation and to `pip install`, so a slow install could take up to ~2x the
  stated budget before the time-box actually fired — worth calling a real
  gap against "time-box the install," not just a style nit. Fixed: a single
  `deadline = time.monotonic() + timeout` is computed once, and the second
  subprocess call gets whatever's left of it (`deadline - time.monotonic()`),
  returning a timed-out result immediately, without even attempting pip
  install, if venv creation alone consumed the whole budget.

**A macOS-specific gap found and fixed post-M11 (M11 itself was built/verified
on Linux/bubblewrap only — the Seatbelt `pip install` path went unexercised
until run for real on a Mac):** `install_target_dependencies` failed on
macOS with `pip install failed (returncode=-6)` and empty stdout/stderr —
`-6` is `SIGABRT`, and the empty output made it look uninformative, but the
honest-failure behavior itself held: the case came back `inconclusive` with
a concrete reason, never a crash or a fabricated pass. Root-caused by
reproducing `install_target_dependencies` directly against the real
react-agent clone (bypassing the full audit pipeline) and reading the
resulting macOS crash report
(`~/Library/Logs/DiagnosticReports/python3.12-*.ips`), not guessed:

- The narrowed-read Seatbelt profile
  (`_SEATBELT_PROFILE_NETWORK_NARROW_READ`) was translated straight from
  bubblewrap's `read_paths` allowlist — same shape, `(deny default)` plus
  `subpath` rules for exactly `_INSTALL_READ_PATHS` and nothing else. On
  macOS that crashes before Python even starts: dyld4's `CacheFinder`
  (which locates the shared cache every dynamically-linked process needs
  to boot) reads the root directory `/` itself, and Seatbelt's `subpath
  "X"` rules never grant access to `/` — only to `X` and what's under it.
  Confirmed by bisecting profiles directly with `sandbox-exec`: an
  allowlist covering every top-level directory *except* an explicit `/`
  grant still crashes identically; adding `(allow file-read* (literal
  "/"))` fixes it immediately.
- Past that crash, two more real gaps in the same allowlist: pip's own
  wheel-tag resolution (`packaging.tags.mac_platforms`) needs
  `/System/Library/CoreServices/SystemVersion.plist`, and DNS resolution
  for PyPI failed (`nodename nor servname provided`) in a way that
  survived even a generous per-directory allowlist (`/System`, `/Library`,
  `/private`, plus unconditional `mach-lookup`) — macOS's `getaddrinfo`
  path (`mDNSResponder`/`configd`) doesn't map cleanly onto the
  glibc-`resolv.conf` model the Linux allowlist assumed, and exactly which
  file(s) it needs wasn't pinned down by enumeration even after extensive
  bisection.

**Fix (Seatbelt only — bubblewrap untouched, still the per-path allowlist
described above):** since per-path enumeration proved unable to reach
completeness with reasonable confidence, `_SEATBELT_PROFILE_NETWORK_NARROW_READ`
is now built the opposite way from bubblewrap's: allow read broadly
(`subpath "/"`), then explicitly deny what the read-scope hardening above
is actually protecting against — the invoking user's home directory (SSH
keys, cloud credentials, the harness's own source all live there),
`/etc/ssh`'s host keys, and `/tmp` (a shared location any other process
could have dropped a secret into) — then re-open `read_paths`/cwd/scratch_dir
as overrides for the cases where one of them lives under a denied path
(here, `sys.base_prefix` does, under a pyenv install under home).
`mach-lookup` is allowed unconditionally too. Verified directly: a real
`pip install` against react-agent (`langgraph`, `langchain-openai`, and
react-agent's own package) now succeeds end-to-end under Seatbelt.

**Security trade-off, stated plainly (this is a weakening, not a neutral
implementation detail):** macOS/Seatbelt's `pip install` sandbox now fails
**open** — everything is readable unless it's on the denylist — while
Linux/bubblewrap's `read_paths` handling still fails **closed** — nothing
is readable unless it's on the allowlist. Those are not equally strong.
Fails-closed is the stronger posture: a path this code never anticipated
is safe by default. Fails-open is weaker: an unanticipated path is exposed
by default, and only the specific things someone thought to deny are
actually protected. macOS got the weaker model because DNS resolution
during that step (`getaddrinfo` via `mDNSResponder`/`configd`) could not
be reliably reduced to a finite, enumerable allowlist of paths — every
attempt at a fails-closed allowlist either crashed dyld or broke DNS, and
extensive bisection couldn't pin down the complete minimal set (see the
diagnosis above). This gap should be treated as open, not solved: if
macOS ever exposes a supported way to scope Seatbelt file reads to
exactly what `getaddrinfo` needs, this should switch back to fails-closed
to match bubblewrap.

**A follow-up subagent review of this exact fix caught two real gaps,
fixed the same session, not left for later:**
- Denying only the invoking user's home directory left *other* local
  accounts' home directories, root's home (`/private/var/root`), and
  mounted volumes (`/Volumes`) all readable under the broad `subpath "/"`
  grant — none of them load-bearing for a `pip install`, all plausible
  places for a real credential to sit. Now denied explicitly alongside
  home/`/etc/ssh`/`/tmp`. This is still not, and cannot be in a general
  way, equivalent to bubblewrap's posture: a credential at a path named by
  an environment variable outside all of these (`KUBECONFIG`,
  `GOOGLE_APPLICATION_CREDENTIALS`, a CI secrets mount elsewhere under
  `/opt` or `/private/var`) remains readable on macOS, where bubblewrap's
  narrow allowlist would never have reached it in the first place —
  documented in `sandbox.py` as a deliberate, disclosed trade-off (per-path
  *allow*-enumeration proved operationally unreliable on macOS; broad
  *deny*-enumeration of a handful of well-known human-data directories is a
  different and much safer kind of list) rather than implied to be as
  strong as the Linux version.
- The read-scope-exclusion check (below) originally only probed a secret
  under `/tmp` — which the fixed profile denies *unconditionally*,
  independent of `read_paths`. That made the check pass even if
  `read_paths` curation were completely broken, a false-confidence check
  on macOS specifically. Fixed by adding a second probe under the invoking
  user's home directory, at a location outside every `read_paths` override
  (not under `sys.base_prefix`, cwd, or scratch_dir) — that one is only
  blocked because it falls outside the curated allowlist, on both
  platforms, so it actually exercises the mechanism the check claims to
  prove.

**Check — `checks/m11.py`:** eight checks, no mocking of `install_target_dependencies`
itself. (1) A target with no `pyproject.toml` (`targets/stdin-echo-target`) is
unaffected — regression check. (2) A new fixture,
`targets/pyproject-broken-target/` (a `pyproject.toml` declaring one real,
nonexistent PyPI package — fast and deterministic, a 404 from the index, no
timeout trickery needed), fails honestly with the concrete reason, and never
actually runs its `main.py`. (3) `install_target_dependencies` called directly
against the real react-agent clone with an artificially tiny timeout (`0.01s`)
proves the time-box cuts off a real in-flight install, not just a theoretical
one. (4) A real install against react-agent, at the real default timeout,
makes both `langgraph` and `react_agent` itself importable through the
installed venv's own python — closing the M9 gap directly. (5) Wiring: running
react-agent's discovered entry_point through `execute_case_against_target`
no longer shows `ModuleNotFoundError` anywhere in its output. (6) A live
end-to-end `agentaudit audit` sanity run against react-agent — this now
reaches a genuine outcome (`verdict="certified"` observed live, not
`inconclusive`), a real step past M9/M10's structurally-blocked state; the
check still tolerates `inconclusive` but asserts it carries none of the old
M9/M10/M11 wiring-failure signatures if so. (7) The `pip install` step's
narrowed `read_paths` actually excludes a path outside its allowlist — plants
a fake secret file, asserts the sandboxed posture used for that call genuinely
can't read it, proving the read-scope hardening blocks a real read rather than
just coexisting with a working pip install. (8) macOS-only (skips elsewhere,
printing why, rather than silently passing): a real install against
react-agent succeeds end-to-end specifically under the Seatbelt backend —
regression guard for the crash above, added so the Seatbelt path can't go
unexercised by CI again the way it did the first time.

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
