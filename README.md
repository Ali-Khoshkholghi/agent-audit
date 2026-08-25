# AgentAudit

A certification harness for LLM agents, built end-to-end on the [Claude Agent
SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python).

Each milestone in [`PROJECT.md`](PROJECT.md) exercises one part of the SDK and
ends in a check script that either passes or fails — no "it looked fine."

## Project status

**This is an active, ongoing project.**

**If you're interested in collaborating on this project in any capacity, please reach out via my [LinkedIn](https://www.linkedin.com/in/ali-khoshkholghi-phd-33584614b/).**

The original 8-milestone plan (M1–M8) is done, but running it against a real target
(`langchain-ai/react-agent`) kept surfacing gaps the plan hadn't
anticipated — entry-point discovery with no spec file, actually piping input
into a target's stdin, installing a target's own dependencies before it can
even be imported, a labeling bug that let an execution that never ran get
marked "fail" — and each of those became its own milestone (M9–M11b). That
pattern is expected to continue: more target frameworks, more dependency
managers, more edge cases in what "the target didn't actually run" looks
like. Treat everything below as the current state, not a destination.

## What it does

AgentAudit points at a target agent repository, generates adversarial test
cases against it, executes those cases in a sandboxed environment, judges
the results, and emits a schema-valid `CertificationReport` — a verdict
(`certified` / `certified_with_findings` / `not_certified` / `inconclusive`)
plus a list of `Finding`s (severity, category, evidence, reproduction
steps).

The pipeline runs as three isolated agents:

- **case-generator** — reads the target and proposes adversarial cases (Haiku)
- **case-executor** — runs each case through sandboxed tools (Haiku)
- **judge** — sees only results, never the target source or the generator's
  reasoning, and rules on each case (Opus)

## Setup

Requires Python 3.11+. On Debian/Ubuntu, `pip install claude-agent-sdk`
against the system Python fails — always use a venv.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Auth is via a Claude subscription (Claude Pro/Max), not pay-per-token API
billing:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<token from `claude setup-token`>
```

**Do not set `ANTHROPIC_API_KEY`.** If it's present in the environment, the
SDK silently prefers it and switches to pay-per-token billing instead of
subscription usage — check `echo $ANTHROPIC_API_KEY` prints nothing before
running anything.

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
resumes an interrupted pipeline instead of starting over. All three commands
accept `--max-budget-usd` (default `0.50`) as a hard cost cap per run.

`certify` is the CI-facing entry point: it prints the machine-readable
`CertificationReport` JSON to stdout, a human-readable summary to stderr,
and its exit code reflects the verdict directly — `certified` /
`certified_with_findings` exit 0, `not_certified` / `inconclusive` exit 1,
so a CI job never passes silently just because no case actually ran.

## Architecture

**Subagent isolation.** The generator, executor, and judge run as separate
`AgentDefinition`s with different models and tool access. The isolation is
the point, not an implementation detail: the judge only ever sees case
results, never the target's source or the reasoning that produced a case,
so it can't rubber-stamp cases the generator wrote to be easy.

**Sandboxing.** `can_use_tool`, a `PreToolUse` audit hook, and declarative
deny rules gate everything the harness itself does. Actually running target
code is sandboxed independently at the OS level, in `sandbox.py`:
bubblewrap on Linux, `sandbox-exec` (Seatbelt) on macOS. Both deny outbound
network and filesystem writes outside a scratch directory; a platform with
neither backend available fails closed rather than falling back to
unsandboxed execution.

**Structured output.** `schema.py` holds the Pydantic models — `Finding`
and `CertificationReport` — that are the single source of truth. JSON
Schema is generated from them for the SDK's `output_format`, never
hand-written twice, and every case-executor run also writes a second,
purely mechanical ledger of raw execution results, so a labeling decision
can be checked against unparaphrased evidence (see Known limitations).

**Beyond M8:** getting a real, unmodified target repo (`react-agent`) to
actually run end to end needed three more capabilities that the original
plan didn't call out: discovering an entry point when there's no spec file
(M9), piping a case's input into that entry point's stdin instead of trying
to execute it directly (M10), and installing a target's own declared
dependencies into a disposable venv before importing it (M11). Full
detail, including real bugs found along the way, is in `PROJECT.md`.

## Known limitations

Stated plainly, not buried:

- **Dependency installation supports `pyproject.toml` only.** A target
  with no `pyproject.toml` is unaffected (execution just proceeds with the
  harness's own interpreter, as before M11) — but a target that declares
  dependencies via `requirements.txt` alone, or any other manifest format,
  isn't installed and will likely fail to import.
- **Network scoping during dependency install is pip-level, not
  OS-level.** This environment has no `iptables`/`nft` and no passwordless
  root, so there's no per-host firewall to build inside the sandbox's
  network namespace — bubblewrap's network control is all-or-nothing. The
  only boundary during `pip install` is `--index-url`/`--trusted-host`
  pinned to `pypi.org`; a malicious sdist's own build script could in
  principle still reach other hosts during its build step. Target
  *execution* itself stays fully network-denied throughout — this gap is
  scoped to the install step only, and read access during that step is
  narrowed to a curated allowlist rather than the whole filesystem, which
  bounds the damage even with network open.
- **Fail-vs-inconclusive labeling had a real edge case, now mitigated.**
  Whether a case's outcome is "fail" (executed, revealed the problem) or
  "inconclusive" (never actually exercised) was originally judgment-based —
  the executor subagent was told how to tell the two apart in its prompt,
  but nothing enforced it. That proved unreliable in practice: a case
  against `react-agent` was labeled "fail" even though the target's
  `graph.py` has no stdin-reading code at all, so nothing was ever
  exercised. M11b closed this with a deterministic, non-LLM check: a
  mechanical execution ledger records the raw sandbox result for every run,
  and the harness overrides *any* outcome — "fail" or "pass" — back to
  "inconclusive" whenever the matching record shows a clean exit with no
  stdout and no stderr. A residual, lower-likelihood gap remains (documented
  in `PROJECT.md`'s M5 section) if a case is ever executed more than once
  and only the last attempt is checked against.
- **A real pass/fail verdict against `react-agent`'s actual planted flaw is
  structurally unreachable in this environment.** `react_agent/graph.py`
  has no `__main__` block or any stdin-reading code at all, so even with
  entry-point discovery and stdin wiring both correct (M9/M10), there's
  nothing in the target that can observably consume what's piped to it —
  and even a target that did read stdin would still need a live model API
  call to produce a real result, which the sandbox deliberately denies
  during execution. `inconclusive` is the expected, correct outcome there;
  the milestones above closed the *wiring* gaps (entry-point discovery,
  stdin, dependencies) without claiming to close that one, which is a
  property of the target and this environment, not a bug in the harness.

## Running the checks

Each milestone has its own verification script — no pytest, just a script
that exits 0 or fails with a clear assertion:

```bash
python checks/m1.py    # query loop + telemetry
python checks/m2.py    # read-only, cost-bounded
python checks/m3.py    # custom MCP tool wiring
python checks/m4.py    # structured output parses into CertificationReport
python checks/m5.py    # subagent isolation, multi-model usage
python checks/m6.py    # sandboxing blocks network/filesystem escape
python checks/m7.py    # crash/resume, severity-rubric skill
python checks/m8.py    # containerized run, OTel traces, CI exit code
python checks/m9.py    # entry-point discovery without a spec file
python checks/m10.py   # target_input wired into execution via stdin
python checks/m11.py   # per-target ephemeral dependency installation
python checks/m11b.py  # deterministic fail-vs-inconclusive override
```

See [`PROJECT.md`](PROJECT.md) for what each check actually asserts, why,
and the real bugs each milestone turned up along the way.
