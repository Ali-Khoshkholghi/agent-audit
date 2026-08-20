"""M8 check: containerized end-to-end run + OTel export + CI wiring.

Three independent parts, matching PROJECT.md's M8 check text ("end-to-end
run inside the container against a fixture repo, asserting traces reach a
local collector and the exit code reflects the verdict") plus the
GitHub Actions wiring PROJECT.md's "Build" section asks for:

Part A (containerized run) builds the harness image and a local OTel
collector via docker-compose.m8.yml, runs `agentaudit certify` against the
bundled targets/simple-langgraph-agent fixture *inside* the container, and
asserts the container's exit code matches the verdict actually printed in
its stdout JSON — using cli._verdict_exit_code itself, so this check can
never silently drift from the policy it's verifying.

Part B (traces reach a local collector) reads back the OTel collector's
file-exporter output (bind-mounted to runs/otel-m8/traces.json) and asserts
it contains a span from *this specific run* — matched on the run's real
session_id from the certify output, not just "the file is non-empty" —
proving telemetry reached the collector rather than merely that some prior
run's data is still on disk.

Part C (GitHub Actions wiring) statically validates
.github/workflows/certify.yml: it triggers on pull_request, has
pull-requests: write permission, posts via `gh pr comment`, authenticates
with CLAUDE_CODE_OAUTH_TOKEN, and never references ANTHROPIC_API_KEY (see
CLAUDE.md's "never set ANTHROPIC_API_KEY" rule) or a hand-written PR-comment
JSON parse (it must dispatch through scripts/format_pr_comment.py, which
imports the same agentaudit.schema.CertificationReport this check uses).
This part does not require Docker and does not actually invoke GitHub
Actions — no local runner is available for that.

Run with: python checks/m8.py
"""
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit.cli import _verdict_exit_code  # noqa: E402
from agentaudit.schema import CertificationReport  # noqa: E402

COMPOSE_FILE = ROOT / "docker-compose.m8.yml"
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "certify.yml"
TRACE_DIR = ROOT / "runs" / "otel-m8"
TRACE_FILE = TRACE_DIR / "traces.json"
FIXTURE_TARGET = "targets/simple-langgraph-agent"

# docker compose (v2, a `docker` subcommand) vs the standalone docker-compose
# binary — prefer v2, fall back if only the legacy binary is on PATH.
def _compose_base() -> list[str]:
    if shutil.which("docker") is not None:
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose") is not None:
        return ["docker-compose"]
    raise RuntimeError(
        "no working docker compose found (tried `docker compose` and "
        "`docker-compose`) — Docker/Colima must be installed and running "
        "for this check; see PROJECT.md's M8 Build section"
    )


def _compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    cmd = _compose_base() + ["-f", str(COMPOSE_FILE)] + list(args)
    return subprocess.run(cmd, cwd=ROOT, **kwargs)


def _wait_for_collector(host: str = "127.0.0.1", port: int = 4318, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.5)
    raise AssertionError(f"otel-collector never accepted connections on {host}:{port}")


def check_containerized_run_and_traces() -> None:
    if TRACE_DIR.exists():
        shutil.rmtree(TRACE_DIR)
    TRACE_DIR.mkdir(parents=True)
    # otel-collector-contrib's image runs as a non-root UID with no shell
    # (a distroless nonroot base) that never matches the host user who
    # owns this bind-mounted dir — the default 0o755 has no write bit for
    # "other", so the file exporter fails with "permission denied" before
    # the OTLP receivers even start listening. World-writable is fine:
    # this is disposable local scratch data for the check, torn down by
    # the `finally` block below regardless of outcome.
    TRACE_DIR.chmod(0o777)

    print("building images...")
    build = _compose("build", capture_output=True, text=True)
    assert build.returncode == 0, f"docker compose build failed:\n{build.stdout}\n{build.stderr}"

    _compose("up", "-d", "otel-collector", capture_output=True, text=True, check=False)
    try:
        _wait_for_collector()
        print("OK: otel-collector accepting connections")

        print(f"running `agentaudit certify {FIXTURE_TARGET}` inside the container...")
        run = _compose(
            # -T: no pseudo-TTY — this runs headless and captures stdout
            # for JSON parsing, so a TTY's control sequences would corrupt it.
            "run", "--rm", "-T", "agentaudit",
            "certify", FIXTURE_TARGET, "--max-budget-usd", "0.50",
            capture_output=True, text=True, timeout=180,
        )

        # certify prints the CertificationReport JSON on stdout and nothing
        # else (see cli._cmd_certify) — diagnostics go to stderr.
        report = CertificationReport.model_validate_json(run.stdout)
        expected_exit_code = _verdict_exit_code(report.verdict)
        assert run.returncode == expected_exit_code, (
            f"container exit code ({run.returncode}) does not reflect the printed "
            f"verdict ({report.verdict.value!r} -> expected {expected_exit_code}):\n"
            f"stderr:\n{run.stderr}"
        )
        print(
            f"OK: exit code {run.returncode} matches verdict={report.verdict.value!r} "
            f"({len(report.findings)} finding(s))"
        )

        assert report.provenance is not None and report.provenance.session_id, (
            "certify's report has no provenance.session_id to match against the trace export"
        )
        session_id = report.provenance.session_id

        # Give the collector a moment to receive the run's final export
        # batch over the network before we read its output back.
        time.sleep(3)
        assert TRACE_FILE.is_file(), f"no trace file exported to {TRACE_FILE}"
        trace_text = TRACE_FILE.read_text()
        assert trace_text.strip(), "trace file exists but is empty — nothing was exported"
        assert "claude_code.interaction" in trace_text, (
            "no claude_code.interaction span found in the exported traces — "
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA may not have taken effect"
        )
        assert session_id in trace_text, (
            f"exported traces don't mention this run's session_id ({session_id}) — "
            "can't confirm the traces are from this run rather than stale data"
        )
        print(f"OK: traces for session={session_id[:8]}... reached the local collector")

        # PROJECT.md's M8 Build text asks for "traces and cost metrics" —
        # the file exporter is shared across pipelines (see
        # otel-collector-config.yaml), so cost/token counters land in the
        # same file as the trace/log data checked above.
        assert '"name":"claude_code.cost.usage"' in trace_text, (
            "no claude_code.cost.usage metric found in the export — "
            "the collector's metrics pipeline may not be wired up"
        )
        print("OK: cost metrics reached the local collector")
    finally:
        _compose("down", "-v", capture_output=True, text=True, check=False)


def check_github_actions_wiring() -> None:
    import yaml

    assert WORKFLOW_FILE.is_file(), f"missing {WORKFLOW_FILE}"
    raw = WORKFLOW_FILE.read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in raw, "certify.yml never references CLAUDE_CODE_OAUTH_TOKEN"

    doc = yaml.safe_load(raw)

    # Structural check, not a raw substring search — the file's own
    # explanatory comments legitimately mention ANTHROPIC_API_KEY to say
    # it's *not* used, which a plain `"ANTHROPIC_API_KEY" not in raw`
    # assertion would misfire on. What actually matters is that no step's
    # `env:` block sets it.
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []):
            assert "ANTHROPIC_API_KEY" not in (step.get("env") or {}), (
                "certify.yml sets ANTHROPIC_API_KEY in a step's env — CLAUDE.md "
                "requires CLAUDE_CODE_OAUTH_TOKEN only"
            )
    # PyYAML 1.1 parses the unquoted `on:` key as the boolean True.
    triggers = doc.get("on", doc.get(True))
    assert triggers is not None and "pull_request" in triggers, (
        "certify.yml does not trigger on pull_request"
    )

    perms = doc.get("permissions", {})
    assert perms.get("pull-requests") == "write", (
        f"certify.yml permissions missing pull-requests: write, got {perms!r}"
    )

    jobs = doc.get("jobs", {})
    assert "certify" in jobs, "certify.yml has no `certify` job"
    steps = jobs["certify"].get("steps", [])
    run_bodies = [s.get("run", "") for s in steps]

    assert any("agentaudit certify" in b for b in run_bodies), (
        "certify.yml never runs `agentaudit certify`"
    )
    assert any("gh pr comment" in b for b in run_bodies), (
        "certify.yml never posts a PR comment via `gh pr comment`"
    )
    assert any("format_pr_comment.py" in b for b in run_bodies), (
        "certify.yml doesn't build the comment via scripts/format_pr_comment.py "
        "(the CertificationReport-schema-backed formatter)"
    )
    assert any(
        s.get("if", "").strip() and "exit_code" in s.get("if", "") for s in steps
    ), "certify.yml has no step gating the job on the certify step's exit_code"

    print("OK: .github/workflows/certify.yml triggers on PRs, authenticates via "
          "CLAUDE_CODE_OAUTH_TOKEN, and comments via scripts/format_pr_comment.py")


def main() -> int:
    check_containerized_run_and_traces()
    check_github_actions_wiring()
    return 0


if __name__ == "__main__":
    sys.exit(main())
