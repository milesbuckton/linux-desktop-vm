"""Orchestrator: build_and_provision, prefetch_images, simulate_distro, simulate_all."""
from __future__ import annotations
import shlex
import sys
import time
from pathlib import Path

from .constants import (
    OUT_ROOT, REPO, DISTRO_MIRROR, USERNAMES,
    BUILD_TIMEOUT_SEC, SSH_REACHABLE_TIMEOUT_SEC,
    CLOUD_INIT_WAIT_TIMEOUT_SEC, DISTRO_DEFAULTS,
)
from .executor import run_with_hard_timeout, run_to_file
from .ssh import (
    log_master, check_guest_dns, wait_ssh_reachable,
    ssh_wait_cloud_init, ssh_cmd, _check_success_marker, _capture_diagnostics,
)
from .lifecycle import shutdown_and_verify
from ..provider import find_running_ssh_port

PROVIDER = "qemu"


def _discover_ssh_port(target: Path, initial_delay: float) -> int | None:
    """Wait for qemu-system to spawn, then parse its SSH host-forward port.

    Shared by build_and_provision and simulate_distro (previously ~10
    duplicated lines each).
    """
    time.sleep(initial_delay)
    for _ in range(30):
        port = find_running_ssh_port(target)
        if port:
            return port
        time.sleep(10)
    return None


def build_and_provision(distro: str, gnome_ext_url: str = "") -> tuple[bool, bool]:
    """Returns (shutdown_ok, cloud_init_ok).

    shutdown_ok=False means the VM couldn't be confirmed dead (cascade risk).
    cloud_init_ok=False means cloud-init didn't complete successfully (fail-fast).
    Either is grounds to abort the whole fleet.
    """
    tag = f"{distro}/{PROVIDER}"
    target = OUT_ROOT / f"{distro}-{PROVIDER}"
    build_log = OUT_ROOT / f"{distro}-{PROVIDER}.build.log"
    wait_log = OUT_ROOT / f"{distro}-{PROVIDER}.wait.log"
    stop_log = OUT_ROOT / f"{distro}-{PROVIDER}.stop.log"
    username = USERNAMES[distro]
    build_timeout = BUILD_TIMEOUT_SEC
    wait_timeout = int(DISTRO_DEFAULTS.get(distro, {}).get("cloud_init_wait_timeout_sec") or CLOUD_INIT_WAIT_TIMEOUT_SEC)
    overall_start = time.time()

    ssh_key = target / "ssh_key"
    host: str = ""
    port: int = 0
    vm_started = False
    cloud_init_ok = False
    shutdown_ok = True

    class PhaseAbort(Exception):
        pass

    try:
        # ---- Phase 1: build + start ----
        log_master(f"{tag}: phase-1 (build) starting (timeout {build_timeout//60} min)")

        rc, el = run_to_file(
            [sys.executable, str(REPO / "setup_vm.py"),
             "--distro", distro,
             "--target-dir", str(target), "--keep-qcow2",
             "--gnome-ext-url", gnome_ext_url,
             "--start"] if gnome_ext_url else
            [sys.executable, str(REPO / "setup_vm.py"),
             "--distro", distro,
             "--target-dir", str(target), "--keep-qcow2",
             "--start"],
            build_log, build_timeout,
        )
        if rc == -1:
            log_master(f"{tag}: phase-1 TIMEOUT after {el/60:.1f} min")
            raise PhaseAbort
        if rc != 0:
            log_master(f"{tag}: phase-1 FAIL (exit={rc}) in {el/60:.1f} min")
            raise PhaseAbort
        log_master(f"{tag}: phase-1 ok in {el/60:.1f} min")
        vm_started = True

        # ---- Phase 2a: discover SSH endpoint ----
        if not ssh_key.exists():
            log_master(f"{tag}: phase-2 abort: ssh_key missing at {ssh_key}")
            raise PhaseAbort  # finally block will try to shutdown anyway

        log_master(f"{tag}: phase-2 discovering SSH endpoint ...")
        port = _discover_ssh_port(target, 30.0)
        if not port:
            log_master(f"{tag}: phase-2 abort: couldn't find qemu hostfwd port after 960s")
            raise PhaseAbort
        host = "127.0.0.1"
        log_master(f"{tag}: phase-2 SSH endpoint = {username}@{host}:{port}")

        reachable_host = wait_ssh_reachable(
            host, port, SSH_REACHABLE_TIMEOUT_SEC,
        )
        if reachable_host is None:
            log_master(f"{tag}: phase-2 abort: SSH never reachable on {host}:{port} in {SSH_REACHABLE_TIMEOUT_SEC//60} min")
            raise PhaseAbort
        host = reachable_host
        # Wait a bit for guest SSH to fully initialize before DNS check
        log_master(f"{tag}: phase-2 SSH reachable; waiting for guest services to stabilize ...")
        time.sleep(30)
        mirror = DISTRO_MIRROR.get(distro, "")
        if mirror:
            # Retry DNS check up to 3 times with increasing delays
            dns_ok = False
            for attempt in range(3):
                dns_ok = check_guest_dns(host, port, username, str(ssh_key), mirror)
                if dns_ok:
                    log_master(f"{tag}: guest DNS ok ({mirror})")
                    break
                if attempt < 2:
                    log_master(f"{tag}: WARN: guest DNS resolution failed for {mirror} (attempt {attempt + 1}/3), retrying in 60s...")
                    time.sleep(60)
            if not dns_ok:
                log_master(f"{tag}: WARN: guest DNS resolution failed for {mirror} after 3 attempts")
        log_master(f"{tag}: phase-2 SSH reachable; waiting for cloud-init (timeout {wait_timeout//60} min) ...")

        rc2, el2 = ssh_wait_cloud_init(host, port, username, ssh_key, wait_log, wait_timeout, target_dir=target)
        if rc2 == -1:
            log_master(f"{tag}: phase-2 cloud-init wait TIMEOUT (>{wait_timeout//3600}h) after {el2/60:.1f} min")
        elif rc2 == 0:
            log_master(f"{tag}: phase-2 cloud-init COMPLETE in {el2/60:.1f} min")
            cloud_init_ok = True
        else:
            log_master(f"{tag}: phase-2 cloud-init FAILED (probe rc={rc2}) in {el2/60:.1f} min")

    except PhaseAbort:
        pass
    finally:
        if vm_started:
            shutdown_ok = shutdown_and_verify(target, host, port, username, ssh_key, stop_log)
            total = (time.time() - overall_start) / 60
            log_master(f"{tag}: TOTAL {total:.1f} min  (shutdown_ok={shutdown_ok}, cloud_init_ok={cloud_init_ok})")

    if vm_started:
        return shutdown_ok, cloud_init_ok
    return True, False


def prefetch_images(distros: list[str]) -> bool:
    """Pre-download every distro's cloud image to the shared cache.

    Called before the per-VM build loop so:
      * a flaky-network failure aborts the WHOLE fleet up-front rather
        than the user discovering 4 hours in that distro #6's URL is dead
      * users can run with --prefetch-only to just warm the cache then exit

    Implementation: shell out to setup_vm.py --distro X
    --target-dir <tmp> --prefetch (the prefetch flag short-circuits after
    download_to_cache + verify; never actually builds a VM). The shared
    cache lives under ~/VMs/cache so subsequent VM builds hardlink
    from there.

    Returns True on full success, False if any prefetch failed (caller
    decides whether to abort the fleet).
    """
    log_master(f"=== prefetch: warming cache for {len(distros)} distro(s) ===")
    all_ok = True
    for i, distro in enumerate(distros, 1):
        prefetch_log = OUT_ROOT / f"{distro}.prefetch.log"
        # Use a throw-away target dir under the cache root so any
        # side-effect files (seed.iso etc.) don't pollute ~/VMs.
        cmd = [sys.executable, str(REPO / "setup_vm.py"),
               "--distro", distro,
               "--target-dir", str(OUT_ROOT / "cache" / "_prefetch_scratch" / distro),
               "--prefetch"]
        ok = False
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            log_master(f"--- prefetch {i}/{len(distros)}: {distro} (attempt {attempt}/{max_attempts}) ---")
            rc, elapsed = run_to_file(cmd, prefetch_log, timeout=2400)
            if rc == 0:
                log_master(f"--- prefetch ok ({distro}) in {elapsed/60:.1f} min")
                ok = True
                break
            log_master(f"!!! prefetch FAILED ({distro}, rc={rc}) -- attempt {attempt}/{max_attempts}")
            if attempt < max_attempts:
                wait = attempt * 10  # 10s, 20s backoff
                log_master(f"    retrying in {wait}s ...")
                time.sleep(wait)
        if not ok:
            all_ok = False
    return all_ok


def simulate_distro(distro: str) -> dict:
    """Boot a simulate-mode VM for one distro, capture SIMULATE-OK/FAIL.

    Strategy (iteration 2 -- real boot + marker grep):
      1. Spawn setup_vm.py --simulate, which renders user-data with
         simulate_only=True, builds disk+seed.iso+launcher, AND launches
          the VM (build-only by default; --start must be passed explicitly).
      2. Discover the SSH endpoint:
            - QEMU: parse hostfwd port from the running qemu-system cmdline
      3. wait_ssh_reachable + ssh_wait_cloud_init, exactly like a real
         build but with a tighter timeout (simulate has no install step).
      4. SSH in and grep /var/log/cloud-init-output.log for SIMULATE-OK
         (returns PASS) or SIMULATE-FAIL (returns FAIL with detail).
      5. shutdown_and_verify to leave no orphan vm process.

    Returns dict: {status: PASS/FAIL/ERROR, detail: str, log_path: Path,
                   elapsed_min: float}.
    PASS  = SIMULATE-OK marker found.
    FAIL  = VM booted but the dry-run resolver reported a problem (the
            useful signal -- a package conflict caught BEFORE the 6h
            real-build burns).
    ERROR = infrastructure issue (SSH never came up, vm died, etc.)
            -- not a template bug, retry-worthy.
    """
    target = OUT_ROOT / "simulate" / f"{distro}-{PROVIDER}"
    target.mkdir(parents=True, exist_ok=True)
    sim_log_dir = OUT_ROOT / "simulate" / "logs"
    sim_log_dir.mkdir(parents=True, exist_ok=True)
    build_log = sim_log_dir / f"{distro}-{PROVIDER}-simulate-build.log"
    wait_log = sim_log_dir / f"{distro}-{PROVIDER}-simulate-wait.log"
    stop_log = sim_log_dir / f"{distro}-{PROVIDER}-simulate-stop.log"
    username = USERNAMES[distro]
    ssh_key = target / "ssh_key"
    host: str = ""
    port: int = 0
    vm_started = False
    start = time.time()

    class _SimAbort(Exception):
        pass

    log_master(f"--- simulate {distro}/{PROVIDER}: building simulate-mode VM")

    try:
        # Phase 1: build + start (setup_vm.py --simulate now launches the VM)
        rc, el = run_to_file(
            [sys.executable, str(REPO / "setup_vm.py"),
             "--distro", distro,
             "--target-dir", str(target), "--simulate", "--keep-qcow2",
             "--start"],
            build_log, timeout=1800,  # 30 min: simulate disk-build only (no install)
        )
        if rc != 0:
            return {
                "status": "ERROR",
                "detail": f"setup_vm.py --simulate exit {rc} after {el/60:.1f} min -- see {build_log}",
                "log_path": build_log,
                "elapsed_min": (time.time() - start) / 60,
            }
        vm_started = True

        # Phase 2a: discover SSH endpoint
        port = _discover_ssh_port(target, 90.0)
        if not port:
            raise _SimAbort("couldn't find qemu hostfwd port in 960s")
        host = "127.0.0.1"
        log_master(f"--- simulate {distro}/{PROVIDER}: SSH endpoint = {username}@{host}:{port}")

        # Phase 2b: wait SSH
        reachable_host = wait_ssh_reachable(
            host, port, SSH_REACHABLE_TIMEOUT_SEC,
        )
        if reachable_host is None:
            raise _SimAbort(f"SSH never reachable on {host}:{port} in {SSH_REACHABLE_TIMEOUT_SEC//60} min")
        host = reachable_host
        mirror = DISTRO_MIRROR.get(distro, "")
        if mirror:
            dns_ok = check_guest_dns(host, port, username, str(ssh_key), mirror)
            if not dns_ok:
                log_master(f"  {distro}/{PROVIDER}: WARN: guest DNS resolution failed for {mirror}")

        # Phase 2c: wait cloud-init done (much shorter timeout -- no install)
        # Simulate runcmd is a single dry-run command; even slow distros
        # should be done in <10 min. Cap at 30 min to absorb image-extract,
        # boot, DHCP, and the dry-run itself.
        rc2, el2 = ssh_wait_cloud_init(host, port, username, ssh_key, wait_log, 1800, marker="SIMULATE-OK:", target_dir=target)
        if rc2 == -1:
            raise _SimAbort(f"cloud-init wait TIMEOUT after {el2/60:.1f} min")
        # We don't fail on rc2 != 0 here: cloud-init may flag the
        # simulate-runcmd's intentional non-zero exit (when SIMULATE-FAIL
        # fires) as a config error. The actual signal is the marker grep.

        # Phase 3: grep for the SIMULATE-OK marker.
        # Each per-distro template emits its own SIMULATE-OK string with
        # a distro-specific suffix (e.g. "SIMULATE-OK: package resolver
        # passed for full target list", "SIMULATE-OK: apt resolver
        # passed for full target list"). We match the PREFIX so any
        # variant counts. Same for SIMULATE-FAIL.
        sim_ok_marker = "SIMULATE-OK:"
        sim_fail_marker = "SIMULATE-FAIL:"
        # SSH may be flaky (a loaded guest can starve sshd).
        # Retry the marker check up to 5 times with 30s delays to ride
        # out transient sshd unavailability.
        ok = False
        for _marker_attempt in range(5):
            ok = _check_success_marker(host, port, username, ssh_key, wait_log, marker=sim_ok_marker, target_dir=target)
            if ok:
                break
            if _marker_attempt < 4:
                time.sleep(30)
        if ok:
            # Pull the full SIMULATE-OK line for the detail field so the
            # results table shows which resolver passed.
            full_detail = "SIMULATE-OK"
            try:
                _, stdout, _ = run_with_hard_timeout(
                    ssh_cmd(
                        host, port, username, ssh_key,
                        f"sudo -n grep -F {shlex.quote(sim_ok_marker)} /var/log/cloud-init-output.log 2>&1 | head -1 || true",
                    ),
                    timeout_sec=30,
                )
                line = (stdout or "").strip().splitlines()
                if line and sim_ok_marker in line[0]:
                    full_detail = line[0].strip()
            except Exception:
                pass
            elapsed = (time.time() - start) / 60
            return {
                "status": "PASS",
                "detail": f"{full_detail} ({elapsed:.1f} min total)",
                "log_path": wait_log,
                "elapsed_min": elapsed,
            }
        # Marker not OK: try to pull the SIMULATE-FAIL line for a useful detail
        try:
            _, stdout, _ = run_with_hard_timeout(
                ssh_cmd(
                    host, port, username, ssh_key,
                    f"sudo -n grep -F {shlex.quote(sim_fail_marker)} /var/log/cloud-init-output.log 2>&1 | head -5 || echo NO_FAIL_MARKER",
                ),
                timeout_sec=30,
            )
            fail_line = (stdout or "").strip().splitlines()
            fail_detail = next((l for l in fail_line if sim_fail_marker in l), "no SIMULATE-OK and no SIMULATE-FAIL in cloud-init-output.log")
        except Exception as e:
            fail_detail = f"marker grep error: {type(e).__name__}: {e}"
        # Capture full diagnostic for forensics
        _capture_diagnostics(host, port, username, ssh_key, wait_log, "simulate phase")
        elapsed = (time.time() - start) / 60
        return {
            "status": "FAIL",
            "detail": fail_detail,
            "log_path": wait_log,
            "elapsed_min": elapsed,
        }

    except _SimAbort as exc:
        elapsed = (time.time() - start) / 60
        return {
            "status": "ERROR",
            "detail": f"{exc} (after {elapsed:.1f} min) -- see {build_log}",
            "log_path": build_log,
            "elapsed_min": elapsed,
        }
    finally:
        if vm_started:
            try:
                shutdown_and_verify(target, host, port, username, ssh_key, stop_log)
            except Exception as e:
                log_master(f"--- simulate {distro}/{PROVIDER}: shutdown error: {type(e).__name__}: {e}")


def simulate_all(distros: list[str]) -> dict:
    """Run the simulate phase for every distro.

    Returns results dict: { distro: result_dict }.
    Never exits early: the FULL pass/fail table is collected and printed
    by the caller (main), which then makes the hard-fail decision --
    matching the documented "soft gate, table at end" contract.
    """
    log_master(f"=== simulate: dry-run package resolver for {len(distros)} distro(s) ===")
    results: dict = {}
    idx = 0
    for distro in distros:
        idx += 1
        log_master(f"--- simulate {idx}/{len(distros)}: {distro}/{PROVIDER} ---")
        r = simulate_distro(distro)
        results[distro] = r
        log_master(f"--- simulate {distro}/{PROVIDER}: {r['status']} -- {r['detail']}")
        if r["status"] != "PASS":
            log_master(f"!!! simulate FAIL: {distro}/{PROVIDER} -- inspect {OUT_ROOT / 'simulate' / 'logs' / f'{distro}-{PROVIDER}-simulate-wait.log'}")
    return results
