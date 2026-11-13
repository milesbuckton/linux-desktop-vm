#!/usr/bin/env python3
"""End-to-end smoke test for setup_vm.py's CLI contract with the orchestrator.

Validates that:
  * --prefetch ACTUALLY downloads the image AND short-circuits (doesn't try
    to build a VM)
   * --simulate renders + builds the VM artifacts WITHOUT launching a
     qemu-system process (so smoke can run cheaply without leaking
     running VMs into the host)
  * Both modes produce exit code 0 on success

Picks ubuntu-lts so it's fast enough to run pre-commit (~2-3 min
on warm cache, ~5-10 min on cold).

ALSO checks the linux_vm.fleet package wiring (imports + REPO path +
entry-point contract) so a fleet-module refactor can't silently break the
fleet runtime path while py_compile, lint, and the single-VM smoke still
pass.

Run from repo root:
    python scripts/smoke-test-cli.py
"""
from __future__ import annotations
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP_VM = REPO / "setup_vm.py"

# Prefer the repo's .venv Python (3.15). Re-exec into the venv so this
# process AND every setup_vm.py subprocess run under 3.15 with the full
# dependency set (jinja2, yaml, pycdlib, certifi, paramiko, pylint).
#
# The reason is DEPENDENCIES, not syntax: every module in the repo uses
# `from __future__ import annotations`, so the 3.10+ union-type
# annotations are lazy and even the CommandLineTools 3.9 on this host
# parses and imports the whole package. But a PEP-668 managed Homebrew
# Python has NO site-packages deps at all (and cannot take them without
# --break-system-packages), so a smoke test launched as
# `python3 scripts/smoke-test-cli.py` on a PATH that puts
# /opt/homebrew/bin first dies with `No module named 'jinja2'` while a
# 3.9 with the deps in its user site-packages would have worked. The
# venv is the one interpreter that is guaranteed to be complete.
#
# The "am I already in the venv?" test is `sys.prefix != sys.base_prefix`
# -- i.e. "is this a virtual environment?". It is NOT a comparison of
# resolved paths: a venv's `bin/python` is a SYMLINK to the base
# interpreter, so `Path(v).resolve()` and `Path(sys.executable).resolve()`
# are the SAME file whenever the venv was built from the interpreter that is
# running this script. A path-comparison guard no-ops in exactly the case
# it exists for (running `/opt/homebrew/bin/python3` against a venv built
# from that same Homebrew Python), so the smoke test would run with no
# dependencies instead of silently using the venv.
#
# The shared helper (`linux_vm/venv_bootstrap.py`) owns that decision AND
# guarantees the re-exec happens at most once: a venv whose `pyvenv.cfg`
# is missing or unreadable also reports `sys.prefix == sys.base_prefix`,
# so a naive guard re-fires after the re-exec and the gate spins in a
# silent, output-free exec loop until something kills it.
sys.path.insert(0, str(REPO))

from linux_vm.venv_bootstrap import bootstrap_venv

# `allow_exec` is True ONLY when this script is the one being run: the
# call sits at module level, so an importer (a test) must not have its
# process replaced by the re-exec.
bootstrap_venv(REPO, Path(__file__), allow_exec=__name__ == "__main__")

# Cheapest distro to download for smoke-testing
SMOKE_DISTRO = "ubuntu-lts"


def run(cmd: list, label: str, timeout: int = 600) -> bool:
    print(f"\n=== {label} ===")
    print(f"    cmd: {' '.join(str(c) for c in cmd)}")
    try:
        rc = subprocess.call(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"    [FAIL] timed out after {timeout}s")
        return False
    if rc != 0:
        print(f"    [FAIL] exit code {rc}")
        return False
    print("    [ ok ] exit code 0")
    return True


def _check_dns() -> bool:
    """Verify host-side DNS can resolve every distro mirror in the fleet.

    Probes all DISTRO_MIRROR hosts (L7): a mirror DNS failure is exactly
    what the pre-flight check exists to catch.
    """
    import sys
    sys.path.insert(0, str(REPO))
    from linux_vm.fleet.constants import DISTRO_MIRROR

    ok = True
    for host in sorted(set(DISTRO_MIRROR.values())):
        try:
            socket.getaddrinfo(host, 80)
            print(f"    [ ok ] DNS: {host} resolves")
        except OSError as e:
            print(f"    [WARN] DNS: {host} failed ({e})")
            ok = False
    return ok


def _check_fleet_wiring() -> bool:
    """Verify the linux_vm.fleet package wiring survived refactors.

    Catches the class of bug where moving/nesting the fleet modules
    silently breaks the fleet runtime path while py_compile, lint, and
    the single-VM smoke still pass -- e.g. REPO (fleet.constants)
    resolving one level too deep after the module was nested, so
    subprocess calls target a non-existent linux_vm/setup_vm.py.
    """
    ok = True

    print("\n=== fleet package wiring ===")
    check_code = (
        "import sys; sys.path.insert(0, %r); "
        "from linux_vm.fleet.constants import REPO; "
        "from linux_vm.fleet import main; "
        "assert (REPO / 'setup_vm.py').exists(), f'REPO wrong: {REPO}'; "
        "assert callable(main), 'linux_vm.fleet.main not callable'"
    ) % str(REPO)
    try:
        subprocess.check_call([sys.executable, "-c", check_code], cwd=str(REPO), timeout=60)
        print("    [ ok ] REPO resolves to repo root; linux_vm.fleet imports OK")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"    [FAIL] fleet wiring broken: {e}")
        ok = False

    print("\n=== fleet entry point (unknown arg -> usage rc=2) ===")
    try:
        rc = subprocess.call(
            [sys.executable, str(REPO / "scripts" / "build-fleet-sequential.py"),
             "--smoke-bogus"],
            cwd=str(REPO), timeout=60,
        )
        if rc == 2:
            print("    [ ok ] fleet entry point import + arg-parse contract OK (rc=2)")
        else:
            print(f"    [FAIL] fleet entry point returned rc={rc}, expected 2")
            ok = False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"    [FAIL] fleet entry point error: {e}")
        ok = False

    return ok


def _qemu_pids_under(path: Path) -> list[int]:
    """QEMU processes whose cmdline references `path`.

    Scoped to the scratch dir on purpose: the operator may legitimately
    have their own VM running, and failing smoke because of it would make
    the gate unusable. Anything a --simulate leak would spawn carries the
    scratch target dir in its cmdline, so scoping loses no signal.
    """
    sys.path.insert(0, str(REPO))
    from linux_vm.provider import list_running_qemu_pids
    try:
        return list_running_qemu_pids(str(path))
    except OSError as e:
        print(f"    [WARN] could not enumerate qemu-system ({e})")
        return []


def _run_and_assert_no_qemu(cmd: list, label: str, scratch: Path,
                            timeout: int = 900) -> bool:
    """Run a command, then ASSERT it left no qemu-system behind.

    The old test only asserted `exit code 0`, while its own docstring
    promised it verified `--simulate` launches no qemu-system. A leaked
    VM is exactly the failure mode that starves the host and breaks every
    later build, and exit 0 says nothing about it.
    """
    ok = run(cmd, label, timeout=timeout)
    leaked = _qemu_pids_under(scratch)
    if leaked:
        print(f"    [FAIL] {label} left qemu-system running: PIDs {leaked}")
        for pid in leaked:
            subprocess.call(["kill", "-9", str(pid)])
        return False
    print(f"    [ ok ] {label} launched no qemu-system")
    return ok


def main() -> int:
    print("=== Pre-flight DNS check ===")
    dns_ok = _check_dns()
    if not dns_ok:
        print("    [WARN] some DNS lookups failed; downloads may be slow or fail\n")

    with tempfile.TemporaryDirectory(prefix="smoke-cli-") as tmp:
        scratch = Path(tmp) / "scratch"

        # Test 1: --prefetch must download + short-circuit (no VM build)
        ok1 = _run_and_assert_no_qemu(
            [sys.executable, str(SETUP_VM),
             "--distro", SMOKE_DISTRO,
             "--target-dir", str(scratch),
             "--prefetch"],
            f"--prefetch {SMOKE_DISTRO}",
            scratch,
        )

        # Test 2: --simulate must render + build the VM artifacts (qcow2,
        # seed.iso, launcher) WITHOUT booting qemu (simulate implies
        # build-only by default).
        ok2 = _run_and_assert_no_qemu(
            [sys.executable, str(SETUP_VM),
             "--distro", SMOKE_DISTRO,
             "--target-dir", str(scratch / "sim"),
             "--simulate"],
            f"--simulate {SMOKE_DISTRO}",
            scratch / "sim",
        )

        # Test 3: default (no --distro flag) must work. Use --prefetch which
        # short-circuits before any real work, so this is fast.
        # --target-dir is scoped into the temp scratch so no qcow2
        # materializes in the real ~/VMs.
        ok3 = _run_and_assert_no_qemu(
            [sys.executable, str(SETUP_VM),
             "--target-dir", str(scratch / "default"),
             "--prefetch"],
            f"--prefetch (default distro)",
            scratch / "default",
        )

    print()
    ok4 = _check_fleet_wiring()

    print()
    if ok1 and ok2 and ok3 and ok4:
        print("[ ok ] all smoke tests passed")
        return 0
    print(f"[FAIL] prefetch={ok1} simulate={ok2} default={ok3} fleet-wiring={ok4}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
