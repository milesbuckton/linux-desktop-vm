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
    import argparse
    p = argparse.ArgumentParser(
        prog="build-fleet-sequential.py",
        description=(
            "Sequential fleet build of unattended Linux GNOME-on-Wayland VMs. "
            "Default (no args) builds all supported distros with prefetch + "
            "simulate gates ON; only one VM runs at a time."
        ),
    )
    p.add_argument(
        "--distros", default=None,
        help="Comma-separated list of distros to build, in order (default: all supported)",
    )
    p.add_argument("--prefetch-only", action="store_true",
                   help="Warm the shared image cache then exit (no VMs built)")
    p.add_argument("--no-prefetch", action="store_true",
                   help="Skip the implicit pre-download phase")
    p.add_argument("--simulate-only", action="store_true",
                   help="Do prefetch + simulate then exit (no real build)")
    p.add_argument("--no-simulate", action="store_true",
                   help="Skip the simulate phase")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip pre-build cleanup (orphan VMs)")
    p.add_argument("--start-from", default="",
                   help="Skip all distros before <distro> in the build order")
    args = p.parse_args()
    only_distros = ([s.strip() for s in args.distros.split(",") if s.strip()]
                    if args.distros else [])
    prefetch_only = args.prefetch_only
    skip_prefetch = args.no_prefetch
    simulate_only = args.simulate_only
    skip_simulate = args.no_simulate
    skip_preflight = args.no_preflight
    start_from = args.start_from

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
        # When --distros is set, only its members that come after start_from
        # are valid; without --distros we slice from start_from in DISTROS order.
        if only_distros:
            distros_to_build = [d for d in only_distros if d in DISTROS
                                and DISTROS.index(d) >= start_idx]
        else:
            distros_to_build = list(DISTROS)[start_idx:]
        if not distros_to_build:
            log_master(
                f"ERROR: --start-from {start_from!r} combined with "
                f"--distros {only_distros!r} leaves nothing to build. "
                f"Either drop --start-from or include {start_from!r} (or a later distro) in --distros."
            )
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
            log_master(f"    {OUT_ROOT / 'simulate' / 'logs'}/<distro>-simulate-wait.log")
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
    # fleet so every distro installs dash-to-dock deterministically
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
        log_master(f"--- VM {n}/{total}: {distro} ---")
        shutdown_ok, ci_ok = build_and_provision(distro, gnome_ext_url=gnome_ext_url)
        if not shutdown_ok:
            log_master(f"!!! VM {n} ({distro}) shutdown verification failed -- marking as failed")
            failed_distros.append((distro, "shutdown verification"))
            continue
        if not ci_ok:
            log_master(f"!!! VM {n} ({distro}) cloud-init FAILED -- marking as failed")
            failed_distros.append((distro, "cloud-init"))
            continue
        log_master(f"  VM {n} ({distro}) SUCCESS")
    if gnome_ext_proc is not None:
        stop_gnome_ext_server(gnome_ext_proc)
        log_master("=== GNOME extension server stopped ===")
    total_h = (time.time() - overall_start) / 3600
    if failed_distros:
        log_master(f"=== SUMMARY: {len(distros_to_build) - len(failed_distros)}/{len(distros_to_build)} distros completed successfully, {len(failed_distros)} failed ===")
        log_master("  Failed distros:")
        for distro, reason in failed_distros:
            log_master(f"    {distro}: {reason}")
    else:
        log_master(f"=== DONE in {total_h:.2f}h ===")
    if failed_distros:
        log_master("!!! Some distros failed. To continue from the failed distro, run: python scripts/build-fleet-sequential.py --start-from <first-failed-distro>")
        sys.exit(1)
