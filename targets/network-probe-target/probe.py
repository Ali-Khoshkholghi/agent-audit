"""Planted fixture for M6: attempts one outbound HTTP call and one write
outside the sandbox's scratch directory, then reports what happened.

Not a real agent — this is a minimal probe whose only job is to prove
AgentAudit's sandboxing actually blocks both classes of escape when this
script is executed as a case's entry_point. Both attempts are expected to
fail when run through agentaudit.sandbox.run_sandboxed(); if either
succeeds, that's a sandboxing bug, not a finding about this fixture.

Emits one final, stable, greppable line so callers don't have to depend on
an LLM's paraphrase of the result:
  SANDBOX_RESULT: network=<blocked|allowed> filesystem=<blocked|allowed>
"""
import pathlib
import urllib.request

MARKER_PATH = "/etc/agentaudit-sandbox-test-marker"

try:
    urllib.request.urlopen("http://example.com", timeout=3)
    network = "allowed"
    network_detail = "request succeeded"
except Exception as e:
    network = "blocked"
    network_detail = f"{type(e).__name__}: {e}"

try:
    pathlib.Path(MARKER_PATH).write_text("pwned")
    filesystem = "allowed"
    filesystem_detail = f"wrote {MARKER_PATH}"
except Exception as e:
    filesystem = "blocked"
    filesystem_detail = f"{type(e).__name__}: {e}"

print(f"network attempt: {network_detail}")
print(f"filesystem attempt: {filesystem_detail}")
print(f"SANDBOX_RESULT: network={network} filesystem={filesystem}")
