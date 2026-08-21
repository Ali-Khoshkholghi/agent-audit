"""Planted fixture for M10: proves target_input actually reaches a running
target's own logic via stdin, not just that AgentAudit's tooling claims to
have sent it.

Not a real agent — this is a minimal probe whose only job is to read
whatever's piped to its stdin and echo it back inside a stable, greppable
marker line, so callers don't have to depend on an LLM's paraphrase of the
result — same pattern as network-probe-target's SANDBOX_RESULT line for M6.
"""
import sys

received = sys.stdin.read()
print(f"STDIN_ECHO_START:{received}:STDIN_ECHO_END")
