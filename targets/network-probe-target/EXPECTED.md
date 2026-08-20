# Ground truth for this fixture

- Not a real agent — a minimal probe used only by `checks/m6.py` to verify
  sandboxed execution.
- `entry_point`: `probe.py`. Running it attempts one outbound HTTP call and
  one write to `/etc/agentaudit-sandbox-test-marker`.
- Both attempts must be blocked when executed through
  `agentaudit.sandbox.run_sandboxed()`. Neither succeeding is the pass
  condition — this fixture exists to catch sandboxing regressions, not to
  be "certified."
