# Decisions — injected-clock

### design r1

- R-1 accept — Required-parameter migration omits two dependent test suites; scope expanded to name all four direct callers (`test_codex_credentials.py`'s seven `codex_poll()` calls + `mod.NOW_EPOCH`-derived `jwt_exp` stub, and `test_entry_point.py`'s zero-arg poll stubs vs a `main()` that now passes `now`), with the migration for each spelled out in decision 5.

### design r2

- approve — 0 findings. R-1 re-verified resolved against the edited design.md; the fix introduced no new critical or high issue. Gate passed on verdict, not on waiver.
