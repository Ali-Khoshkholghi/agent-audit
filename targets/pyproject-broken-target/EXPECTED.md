# Ground truth for this fixture

- Not a real agent — a minimal probe used only by `checks/m11.py` to verify
  the M11 dependency-install step's failure path.
- `pyproject.toml` declares one dependency,
  `this-package-definitely-does-not-exist-9x7z`, which does not exist on
  PyPI — real, deterministic, fast (a 404 from the index, not a slow
  resolver search), and needs no timeout trickery to fail.
- `main.py` must never actually run: `install_target_dependencies` (and
  therefore `execute_case_against_target`) must report `is_error=True` with
  a concrete reason naming the failed install, before execution is ever
  attempted. If this fixture's entry_point output ever shows up in a
  result, dependency installation is silently swallowing a real failure
  instead of reporting it honestly — the M11 regression this fixture exists
  to catch.
