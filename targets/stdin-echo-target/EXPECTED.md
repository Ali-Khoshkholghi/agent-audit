# Ground truth for this fixture

- Not a real agent — a minimal probe used only by `checks/m10.py` to verify
  that `execute_case_against_target`'s `stdin_input` actually reaches a
  running target's own logic via stdin, not just that AgentAudit's tooling
  claims to have sent it.
- `entry_point`: `echo_stdin.py`. It reads all of stdin and prints it back
  inside a stable `STDIN_ECHO_START:...:STDIN_ECHO_END` marker line.
- The pass condition is that marker line containing exactly what was piped
  in — this fixture exists to prove the stdin mechanism end-to-end through
  the real OS sandbox, not to be "certified."
