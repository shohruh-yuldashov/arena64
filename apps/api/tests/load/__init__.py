"""The load and performance harness — A64-028.5.

Not collected by `pytest`: nothing here is named `test_*` except
`test_harness.py`, which tests the harness itself rather than the platform.
Timing assertions do not belong in the unit suite (§50), so the scenarios
are run deliberately:

    uv run python -m tests.load run --scenario P01 --against http://127.0.0.1:8101

See `docs/05-operations/load-testing.md` for the environment a number is
only meaningful inside.
"""
