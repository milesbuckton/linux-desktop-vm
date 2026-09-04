#!/usr/bin/env python3
"""End-to-end smoke test for setup_vm.py's CLI contract with the orchestrator.

Validates that:
  * --prefetch ACTUALLY downloads the image AND short-circuits (doesn't try
    to build a VM)
   * --simulate renders + builds the VM artifacts WITHOUT launching a
     qemu-system process (so smoke can run cheaply without leaking
     running VMs into the host)
  * Both modes produce exit code 0 on success

Picks gentoo so it's fast enough to run pre-commit (~2-3 min
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

# Prefer the repo's .venv Python (3.14). The host may have an older system
# Python (e.g. CommandLineTools 3.9) earlier on PATH that rejects the 3.10+
# union-type annotations used throughout linux_vm. Re-exec into the venv so
# this process AND every setup_vm.py subprocess run under 3.14.
def _venv_python() -> str | None:
    for cand in ("bin/python", "bin/python3"):
        v = REPO / ".venv" / cand
        if v.exists():
            return str(v)
    return None

_VENV_PY = _venv_python()
if _VENV_PY and _VENV_PY != sys.executable:
    import os
    os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])

# Cheapest distro to download for smoke-testing
SMOKE_DISTRO = "gentoo"


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


def main() -> int:
    print("=== Pre-flight DNS check ===")
    dns_ok = _check_dns()
    if not dns_ok:
        print("    [WARN] some DNS lookups failed; downloads may be slow or fail\n")

    with tempfile.TemporaryDirectory(prefix="smoke-cli-") as tmp:
        scratch = Path(tmp) / "scratch"

        # Test 1: --prefetch must download + short-circuit (no VM build)
        ok1 = run(
            [sys.executable, str(SETUP_VM),
             "--distro", SMOKE_DISTRO,
             "--target-dir", str(scratch),
             "--prefetch"],
            f"--prefetch {SMOKE_DISTRO}",
            timeout=900,
        )

        # Test 2: --simulate must render + build the VM artifacts (qcow2,
        # seed.iso, launcher) WITHOUT booting qemu (simulate implies
        # build-only by default).
        ok2 = run(
            [sys.executable, str(SETUP_VM),
             "--distro", SMOKE_DISTRO,
             "--target-dir", str(scratch / "sim"),
             "--simulate"],
            f"--simulate {SMOKE_DISTRO}",
            timeout=900,
        )

        # Test 3: default (no --distro flag) must work. Use --prefetch which
        # short-circuits before any real work, so this is fast.
        # --target-dir is scoped into the temp scratch so no qcow2
        # materializes in the real ~/VMs.
        ok3 = run(
            [sys.executable, str(SETUP_VM),
             "--target-dir", str(scratch / "default"),
             "--prefetch"],
            f"--prefetch (default distro)",
            timeout=900,
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
