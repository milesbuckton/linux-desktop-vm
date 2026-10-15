#!/usr/bin/env python3
"""Unit-test gate (fleet Gate 0d; pre-commit; CI).

Runs the repo's unittest suite (``tests/test_logic.py``) and fails on any
error or failure. This exists because the suite is the only thing that
EXERCISES the logic the other gates only statically render:

  * ``TestPlainPasswordOnUserEntry`` renders the templates with numeric,
    bool and quote-bearing passwords and ``cloud-init schema``-shapes them --
    Gate 0/0b always render with ``PASSWORD="x"`` (``linux_vm/test_context.py``),
    so the quoting class is invisible to them.
  * ``TestDashToDockInstallGate`` EXECUTES the rendered shell condition
    against a pretty-printed ``metadata.json`` fixture. Gate 0 cannot catch
    that class of bug: the rendered script is syntactically valid shell, it
    just searches for the wrong thing.
  * the fleet-control-flow tests (status -> exit-code mapping, process-group
    teardown, shutdown grace) cover code no gate renders.

Scope: deliberately every test in ``tests/`` (whole suite, ~3 s), not a diff.
The fleet runs the whole thing before a multi-hour build, and CI runs it on
every push -- a filter that skipped tests would be a gate that quietly
verifies nothing, which is the failure mode that let this suite go unrun in
the first place.

A missing test directory is a hard failure, not a pass: an empty or deleted
suite must never look green.

Exit codes: 0 = all passed, 1 = failures/errors, 2 = setup failure (no
``tests/`` directory, no discoverable tests). No network, no VM.

Usage:
    python scripts/run-tests.py
    python scripts/run-tests.py -v          # verbose test names
"""
from __future__ import annotations
import argparse
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"


def _die(msg: str) -> None:
    print(f"[FAIL] {msg}")
    print("TESTS-FAIL: setup error -- " + msg)
    sys.exit(2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose test names (equivalent to unittest -v)")
    args = p.parse_args()

    if not TESTS_DIR.is_dir():
        _die(f"no tests directory at {TESTS_DIR}")

    print(f"=== unittest: discover -s {TESTS_DIR} ===")
    # tests/test_logic.py imports linux_vm.*, so the repo root has to be
    # importable. Keep the repo out of the repo root itself (sys.path[0] is
    # scripts/), and discover with tests/ as the top level -- tests/ has no
    # __init__.py, so making it a package just to satisfy the loader would be
    # churn.
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS_DIR))
    n_loaded = suite.countTestCases()
    if n_loaded == 0:
        # Unreachable via a present tests/ dir in practice, but an empty suite
        # is exactly the "verifies nothing" state this gate exists to prevent.
        _die(f"discovered 0 tests under {TESTS_DIR}")

    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1,
                                     stream=sys.stdout)
    result = runner.run(suite)
    if result.wasSuccessful():
        print(f"TESTS-OK: {n_loaded} tests passed")
        return 0
    print(f"TESTS-FAIL: {len(result.failures)} failure(s), "
          f"{len(result.errors)} error(s) out of {n_loaded} tests")
    return 1


if __name__ == "__main__":
    sys.exit(main())
