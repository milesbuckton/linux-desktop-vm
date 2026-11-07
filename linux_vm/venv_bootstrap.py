"""Re-exec a gate script into the repo's `.venv` — safely, exactly once.

Why this module exists
----------------------
The four fleet gate scripts (`scripts/lint-templates.py`,
`scripts/audit-packages.py`, `scripts/run-tests.py`,
`scripts/smoke-test-cli.py`) each need `jinja2`, `yaml`, `pycdlib`,
`certifi` and `pylint`. On this host the interpreter that is easiest to
type (`/opt/homebrew/bin/python3`, or `/usr/bin/python3`) carries NONE
of them — Homebrew's Python is PEP-668 managed and installs nothing
into site-packages by default. So each gate re-exec'd itself inside
`./.venv`, which is where the dependencies live.

Two ways that re-exec has historically gone wrong, both of which this
module exists to make impossible:

1. **The "am I already in the venv?" test was wrong.** It used to
   compare RESOLVED PATHS (`Path(venv_python).resolve() !=
   Path(sys.executable).resolve()`). A venv's `bin/python` is a
   SYMLINK to the base interpreter, so those two resolved paths are
   the SAME FILE whenever the venv was built from the interpreter that
   is running the script. The guard therefore no-op'd in exactly the
   case it existed for, and the gate died with
   `No module named 'jinja2'`. The correct test is
   `sys.prefix != sys.base_prefix` — i.e. "is this a virtual
   environment?" — which asks the interpreter instead of guessing from
   the filesystem.

2. **The re-exec could loop forever.** `sys.prefix == sys.base_prefix`
   is ALSO what a venv with a missing or unreadable `pyvenv.cfg`
   reports. In that case the guard re-fires after the re-exec and the
   process replaces itself indefinitely — a silent, output-free spin
   that only ends when something kills it (the fleet's gate timeout,
   or an operator's Ctrl-C). The environment sentinel below makes the
   re-exec happen AT MOST ONCE: the second pass detects the sentinel,
   reports the broken venv, and exits 2 with an actionable message.

This module also fixes a subtler problem: the gate scripts call
`_bootstrap_venv()` at MODULE level, so importing one of them (e.g. via
`runpy.run_path` from a test) under a non-venv interpreter would
`os.execv` and REPLACE THE CALLING PROCESS — the importing code would
never continue. `bootstrap_venv()` therefore refuses to re-exec when
the caller has not opted in explicitly via `allow_exec=True`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: Environment variable set before the re-exec. Its presence means
#: "this process is already the result of a bootstrap re-exec".
BOOTSTRAP_SENTINEL = "LINUX_VM_VENV_BOOTSTRAPPED"


def in_venv() -> bool:
    """True if the running interpreter is inside a virtual environment.

    This is the canonical test: CPython sets ``sys.prefix`` to the
    venv's own directory and ``sys.base_prefix`` to the base
    interpreter's, and they differ exactly when a venv is active.
    """
    return sys.prefix != sys.base_prefix


def venv_python(venv_dir: Path) -> Path | None:
    """Return the interpreter inside ``venv_dir``, or None if there is none."""
    for cand in ("bin/python", "bin/python3"):
        v = venv_dir / cand
        if v.exists():
            return v
    return None


def bootstrap_venv(
    repo: Path,
    script_path: Path,
    *,
    allow_exec: bool = True,
) -> Path | None:
    """Ensure this process runs under ``repo/.venv``.

    Returns the venv interpreter that was exec'd into (when one was),
    or ``None`` if no re-exec happened.

    ``allow_exec=False`` makes this function a NO-OP that only reports
    what it *would* do. Gate scripts call it at module level with
    ``allow_exec=True``; importers (tests, tooling) must use
    ``allow_exec=False`` so that importing a gate script can never
    replace the calling process.
    """
    if in_venv():
        return None

    if os.environ.get(BOOTSTRAP_SENTINEL):
        # We already re-exec'd once and are still not in a virtual
        # environment: the candidate venv is broken (missing or
        # unreadable pyvenv.cfg). Report it instead of looping.
        venv_dir = repo / ".venv"
        sys.stderr.write(
            f"ERROR: {venv_dir} does not look like a virtual environment "
            f"(sys.prefix == sys.base_prefix even after re-exec). "
            f"Recreate it: rm -rf {venv_dir} && python3 -m venv {venv_dir}\n")
        sys.exit(2)

    venv_python_path = venv_python(repo / ".venv")
    if venv_python_path is None:
        return None

    if not allow_exec:
        return venv_python_path

    os.environ[BOOTSTRAP_SENTINEL] = "1"
    os.execv(
        str(venv_python_path),
        [str(venv_python_path), str(script_path), *sys.argv[1:]],
    )
    return venv_python_path  # pragma: no cover -- execv never returns
