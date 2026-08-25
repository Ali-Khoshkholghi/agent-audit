# Ground truth for this fixture

- Not a real agent — a minimal probe used only by `checks/m11b.py` to
  reproduce "executed successfully with no observable evidence" on demand,
  deterministically, without depending on react-agent or network access.
- `entry_point`: `silent.py`. Its entire body is `pass` — no matter what's
  piped to stdin, it exits 0 immediately with empty stdout and empty
  stderr, every time.
- This is the exact shape of the real gap this fixture exists to catch:
  `execute_case_against_target` reporting `returncode=0`, `timed_out=False`,
  empty stdout, empty stderr is a clean exit with literally nothing
  observed — the tested code path never actually ran. A case-executor
  labeling that "fail" (or "pass") is a judgment call the harness must not
  trust; `run_case_executor`'s deterministic override
  (`_is_structurally_silent` in `harness.py`) must force the outcome to
  "inconclusive" regardless of what the subagent concluded. If a case
  against this fixture is ever certified as "fail" or "pass" in the final
  `CaseExecution`, the override isn't wired correctly.
