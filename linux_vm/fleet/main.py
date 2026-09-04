"""Fleet orchestrator entry point: argument parsing + pipeline orchestration."""
from __future__ import annotations
import sys
import time

from .constants import DISTROS, OUT_ROOT
from .ssh import log_master
from .lifecycle import preflight_cleanup
from .orchestrator import prefetch_images, simulate_all, build_and_provision
from ..download import start_gnome_ext_server, stop_gnome_ext_server
from pathlib import Path


def main() -> None:
    # Parse args:
    #   --distros ubuntu-lts,gentoo   build ONLY these distros, IN THE ORDER GIVEN
    #                                 (multiple distros run SEQUENTIALLY, one at a time)
    #   --prefetch-only               warm the shared image cache then exit
    #                                 (no VMs are built; useful before going
    #                                 offline or as a CI cache-warm step)
    #   --no-prefetch                 skip the implicit pre-download phase
    #                                 (default is ON: images downloaded
    #                                 before any VM starts so a flaky-
    #                                 network failure aborts the whole
    #                                 fleet upfront)
    #   --simulate-only               do prefetch + simulate then exit (no real build)
    #   --no-simulate                 skip the simulate phase (default is ON;
    #                                 simulate is a SOFT-fail dry-run that
    #                                 catches package conflicts in ~5 min/distro
    #                                 instead of 6h+)
    #   --no-preflight                skip pre-build cleanup (orphan VMs)
    #   --start-from <distro>          skip all distros before <distro> in the
    #                                 build order (useful for resuming an
    #                                 interrupted fleet build mid-run)
    # Default (no args) builds all distros in DISTROS order
    # with prefetch + simulate gates ON. Only one VM runs at a time.
    only_distros: list[str] = []
    prefetch_only = False
    skip_prefetch = False
    simulate_only = False
    skip_simulate = False
    skip_preflight = False
    start_from: str = ""
    argv = sys.argv[1:]
    while argv:
        a = argv.pop(0)
        if a == "--distros" and argv:
            only_distros = [s.strip() for s in argv.pop(0).split(",") if s.strip()]
        elif a.startswith("--distros="):
            only_distros = [s.strip() for s in a.split("=", 1)[1].split(",") if s.strip()]
        elif a == "--prefetch-only":
            prefetch_only = True
        elif a == "--no-prefetch":
            skip_prefetch = True
        elif a == "--simulate-only":
            simulate_only = True
        elif a == "--no-simulate":
            skip_simulate = True
        elif a == "--no-preflight":
            skip_preflight = True
        elif a == "--start-from" and argv:
            start_from = argv.pop(0)
        elif a.startswith("--start-from="):
            start_from = a.split("=", 1)[1]
        else:
            log_master(f"ERROR: unknown arg: {a!r}")
            log_master("Usage: build-fleet-sequential.py [--distros d1,d2,...] [--prefetch-only|--no-prefetch] [--simulate-only|--no-simulate] [--no-preflight] [--start-from distro]")
            sys.exit(2)

    overall_start = time.time()
    if only_distros:
        # Validate against known DISTROS so a typo doesn't silently no-op
        unknown = [d for d in only_distros if d not in DISTROS]
        if unknown:
            log_master(f"ERROR: --distros contains unknown distros: {unknown}")
            log_master(f"Known: {DISTROS}")
            sys.exit(2)
        distros_to_build = only_distros
    else:
        distros_to_build = list(DISTROS)
    if start_from:
        if start_from not in DISTROS:
            log_master(f"ERROR: --start-from unknown distro: {start_from!r}")
            log_master(f"Known: {DISTROS}")
            sys.exit(2)
        start_idx = DISTROS.index(start_from)
        distros_to_build = [d for d in distros_to_build if DISTROS.index(d) >= start_idx]
        if not distros_to_build:
            log_master(f"ERROR: --start-from {start_from!r} leaves no distros to build")
            sys.exit(2)
        log_master(f"=== --start-from {start_from}: skipping distros before {start_from} ===")
    total = len(distros_to_build)
    log_master(f"=== build-fleet-sequential: prefetch + simulate + verify + cache + continue-on-failure ===")
    log_master(f"=== distros (in order): {distros_to_build} ===")

    # Pre-download phase (default ON, skipped with --no-prefetch). Hard-fail:
    # if any prefetch fails (typically dead upstream URL, no network, firewall)
    # we abort BEFORE any VM build starts -- the whole point of prefetch is to
    # fail fast and avoid wasting 6 hours discovering a broken URL mid-fleet.
    if not skip_prefetch:
        prefetch_ok = prefetch_images(distros_to_build)
        if not prefetch_ok:
            log_master("!!! prefetch FAILED for one or more distros -- ABORTING fleet")
            log_master("    rerun with --no-prefetch to skip the gate, or fix the upstream")
            sys.exit(3)
    if prefetch_only:
        total_h = (time.time() - overall_start) / 3600
        log_master(f"=== prefetch-only mode: DONE in {total_h:.2f}h ===")
        return

    # Simulate phase (default ON, skipped with --no-simulate). HARD-fail:
    # report a per-distro pass/fail table at end of simulate,
    # and abort the fleet if any distro failed. User iterates the
    # simulate-fix loop until all pass before proceeding to real build.
    if not skip_simulate:
        sim_results = simulate_all(distros_to_build)
        # Print final summary table.
        log_master("")
        log_master("=== SIMULATE RESULTS ===")
        pass_count = 0
        total_sims = 0
        for distro in distros_to_build:
            total_sims += 1
            r = sim_results.get(distro, {"status": "?", "detail": ""})
            if r["status"] == "PASS":
                pass_count += 1
            log_master(f"  {distro:<22} {r['status']:<6} {r.get('detail', '')[:70]}")
        log_master(f"=== {pass_count}/{total_sims} distros passed simulate ===")
        log_master("")
        if pass_count < total_sims:
            log_master("!!! HARD-FAIL: one or more distros failed simulate -- aborting fleet")
            log_master("    inspect per-distro logs at:")
            log_master(f"    {OUT_ROOT / 'simulate' / 'logs'}/<distro>-qemu-simulate-wait.log")
            log_master("    fix the package lists or repo URLs, then retry")
            sys.exit(4)
        if simulate_only:
            total_h = (time.time() - overall_start) / 3600
            log_master(f"=== simulate-only mode: ALL PASSED in {total_h:.2f}h ===")
            return

    if skip_preflight:
        log_master("preflight: SKIPPED (--no-preflight) -- caller is responsible for VM cleanup")
    else:
        preflight_cleanup()

    # Host-served GNOME extension zips: pre-fetch once, serve for the whole
    # fleet so every distro installs the six extensions deterministically
    # (no flaky guest->GitHub fetch at build time). Passed to each build;
    # torn down in the finally block below.
    gnome_ext_proc = None
    gnome_ext_url = ""
    try:
        gnome_ext_proc, _gnome_ext_port, gnome_ext_url = start_gnome_ext_server(
            Path.home() / "VMs" / "cache"
        )
        log_master(f"=== GNOME extension server: {gnome_ext_url} (host-served) ===")
    except Exception as e:  # noqa: BLE001 - non-fatal; guest falls back to GitHub
        log_master(f"!!! GNOME extension server failed to start ({e}); guest will use GitHub")

    n = 0
    failed_distros = []
    for distro in distros_to_build:
        n += 1
        log_master(f"--- VM {n}/{total}: {distro}-qemu ---")
        shutdown_ok, ci_ok = build_and_provision(distro, gnome_ext_url=gnome_ext_url)
        if not shutdown_ok:
            log_master(f"!!! VM {n} ({distro}-qemu) shutdown verification failed -- marking as failed")
            failed_distros.append((distro, "shutdown verification"))
            continue
        if not ci_ok:
            log_master(f"!!! VM {n} ({distro}-qemu) cloud-init FAILED -- marking as failed")
            failed_distros.append((distro, "cloud-init"))
            continue
        log_master(f"  VM {n} ({distro}-qemu) SUCCESS")
    if gnome_ext_proc is not None:
        stop_gnome_ext_server(gnome_ext_proc)
        log_master("=== GNOME extension server stopped ===")
    total_h = (time.time() - overall_start) / 3600
    if failed_distros:
        log_master(f"=== SUMMARY: {len(distros_to_build) - len(failed_distros)}/{len(distros_to_build)} distros completed successfully, {len(failed_distros)} failed ===")
        log_master("  Failed distros:")
        for distro, reason in failed_distros:
            log_master(f"    {distro}-qemu: {reason}")
    else:
        log_master(f"=== DONE in {total_h:.2f}h ===")
    if failed_distros:
        log_master("!!! Some distros failed. To continue from the failed distro, run: python scripts/build-fleet-sequential.py --start-from <first-failed-distro>")
        sys.exit(1)
