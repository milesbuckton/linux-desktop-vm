"""Fleet orchestrator entry point: argument parsing + pipeline orchestration."""
from __future__ import annotations
import sys
import time

from .constants import CACHE_ROOT, DISTROS, OUT_ROOT, REPO, ensure_out_root
from .executor import run_with_hard_timeout
from .ssh import log_master
from .lifecycle import preflight_cleanup
from .orchestrator import prefetch_images, simulate_all, build_and_provision
from ..download import start_gnome_ext_server, stop_gnome_ext_server

LINT_TIMEOUT_SEC = 300  # lint is ~3 sec; 5 min cap so a hung render can't stall the fleet
AUDIT_TIMEOUT_SEC = 300  # audit is ~10 sec (static render); 5 min cap mirrors lint
PYLINT_TIMEOUT_SEC = 300  # pylint is ~4 sec over the whole tree; 5 min cap mirrors lint/audit
TESTS_TIMEOUT_SEC = 300  # unittest is ~3 sec; 5 min cap mirrors the other static gates
SMOKE_TIMEOUT_SEC = 3000  # smoke is ~2-3 min warm; each of its 3 tests caps itself at
                          # 900 s, so 50 min only fires if smoke's own timeouts break


def run_lint_gate() -> bool:
    """Gate 0: static template lint (render every distro x arch x mode).

    Hard-fail: a Jinja/YAML/marker regression aborts the fleet before any
    image is downloaded or VM booted. ~3 sec, no network, no VM.
    """
    lint_script = REPO / "scripts" / "lint-templates.py"
    log_master("=== Gate 0: lint (scripts/lint-templates.py) ===")
    rc, out, err = run_with_hard_timeout(
        [sys.executable, str(lint_script)], LINT_TIMEOUT_SEC)
    # Lint's own [ ok ]/[FAIL] lines are the useful part; stream them all
    # into the master log so a failure is diagnosable without a rerun.
    for line in (out or "").splitlines():
        log_master(f"  {line}")
    if rc != 0:
        for line in (err or "").splitlines():
            log_master(f"  ! {line}")
        log_master(f"!!! HARD-FAIL: lint gate (rc={rc}) -- aborting fleet before any build")
        log_master("    fix templates/ then rerun, or skip with --no-lint")
        return False
    log_master("=== Gate 0: lint PASSED ===")
    return True


def run_audit_gate() -> bool:
    """Gate 0b: cross-distro package parity audit (scripts/audit-packages.py).

    Hard-fail: a required app category (curated desktop/app set, see
    REQUIRED in the audit script) missing from either distro's explicit
    package list aborts the fleet before any image is downloaded or VM
    booted. Pure static render, ~10 sec, no network, no VM.
    """
    audit_script = REPO / "scripts" / "audit-packages.py"
    log_master("=== Gate 0b: audit (scripts/audit-packages.py) ===")
    rc, out, err = run_with_hard_timeout(
        [sys.executable, str(audit_script)], AUDIT_TIMEOUT_SEC)
    # The verdict lines (AUDIT-OK / [FAIL]) are the useful part; stream the
    # whole audit into the master log so a failure is diagnosable without a rerun.
    for line in (out or "").splitlines():
        log_master(f"  {line}")
    if rc != 0:
        for line in (err or "").splitlines():
            log_master(f"  ! {line}")
        log_master(f"!!! HARD-FAIL: audit gate (rc={rc}) -- aborting fleet before any build")
        log_master("    restore the missing package(s) or adjust REQUIRED in scripts/audit-packages.py, or skip with --no-audit")
        return False
    log_master("=== Gate 0b: audit PASSED ===")
    return True


def run_pylint_gate() -> bool:
    """Gate 0c: Python source lint (scripts/lint-python.py -> pylint).

    Hard-fail: a pylint finding in linux_vm/, scripts/ or tests/ aborts
    the fleet before any image is downloaded or VM booted. Whole tree
    (not a diff): a multi-hour build runs the committed code end to end,
    so everything checked in has to be clean, not just what a push
    touched. ~4 sec, no network, no VM.

    CI runs the same script with --changed-since so a push only pays for
    the files it changed; this gate always lints everything. A missing
    pylint is a setup error (rc=2), never a silent pass -- install it or
    skip with --no-pylint.
    """
    lint_script = REPO / "scripts" / "lint-python.py"
    log_master("=== Gate 0c: pylint (scripts/lint-python.py) ===")
    rc, out, err = run_with_hard_timeout(
        [sys.executable, str(lint_script)], PYLINT_TIMEOUT_SEC)
    # pylint's findings + the PYLINT-OK/FAIL verdict are the useful part;
    # stream them all into the master log so a failure is diagnosable
    # without a rerun.
    for line in (out or "").splitlines():
        log_master(f"  {line}")
    if rc != 0:
        for line in (err or "").splitlines():
            log_master(f"  ! {line}")
        log_master(f"!!! HARD-FAIL: pylint gate (rc={rc}) -- aborting fleet before any build")
        log_master("    fix the reported messages, then rerun, or skip with --no-pylint")
        return False
    log_master("=== Gate 0c: pylint PASSED ===")
    return True


def run_tests_gate() -> bool:
    """Gate 0d: unit-test suite (scripts/run-tests.py -> unittest).

    Hard-fail: the suite is the only thing that EXECUTES logic the other
    static gates merely render -- password quoting across numeric/bool/quote
    values, the dash-to-dock GNOME-version condition run against a
    pretty-printed metadata.json, and the fleet control-flow mappings. ~3 s,
    no network, no VM.

    It used to be referenced by template comments and AGENTS.md as if it were
    a guard while NO gate or CI job actually ran it, which is how a suite
    guarding exactly the classes the static gates cannot see could rot
    unnoticed. Now it runs on every fleet build and every push.
    """
    tests_script = REPO / "scripts" / "run-tests.py"
    log_master("=== Gate 0d: tests (scripts/run-tests.py) ===")
    rc, out, err = run_with_hard_timeout(
        [sys.executable, str(tests_script)], TESTS_TIMEOUT_SEC)
    for line in (out or "").splitlines():
        log_master(f"  {line}")
    if rc != 0:
        for line in (err or "").splitlines():
            log_master(f"  ! {line}")
        log_master(f"!!! HARD-FAIL: tests gate (rc={rc}) -- aborting fleet before any build")
        log_master("    fix the failing test(s), then rerun, or skip with --no-tests")
        return False
    log_master("=== Gate 0d: tests PASSED ===")
    return True


def run_smoke_gate() -> bool:
    """Gate 1: end-to-end CLI contract smoke test (scripts/smoke-test-cli.py).

    Hard-fail: an orchestrator <-> setup_vm.py argparse mismatch or a
    broken linux_vm.fleet wiring aborts the fleet before any image is
    downloaded or VM booted. ~2-3 min warm, no VM launched.
    """
    smoke_script = REPO / "scripts" / "smoke-test-cli.py"
    log_master("=== Gate 1: smoke (scripts/smoke-test-cli.py) ===")
    rc, out, err = run_with_hard_timeout(
        [sys.executable, str(smoke_script)], SMOKE_TIMEOUT_SEC)
    # Smoke's own [ ok ]/[FAIL] lines are the useful part; stream them all
    # into the master log so a failure is diagnosable without a rerun.
    for line in (out or "").splitlines():
        log_master(f"  {line}")
    if rc != 0:
        for line in (err or "").splitlines():
            log_master(f"  ! {line}")
        log_master(f"!!! HARD-FAIL: smoke gate (rc={rc}) -- aborting fleet before any build")
        log_master("    fix the CLI contract / fleet wiring, then rerun, or skip with --no-smoke")
        return False
    log_master("=== Gate 1: smoke PASSED ===")
    return True


def main() -> None:
    # Parse args:
    #   --distros ubuntu-lts,gentoo   build ONLY these distros, IN THE ORDER GIVEN
    #                                 (multiple distros run SEQUENTIALLY, one at a time)
    #   --no-lint                     skip Gate 0 (static template lint; default ON)
    #   --no-audit                    skip Gate 0b (cross-distro package audit; default ON)
    #   --no-pylint                   skip Gate 0c (pylint over all tracked .py; default ON)
    #   --no-tests                    skip Gate 0d (unittest suite; default ON)
    #   --no-smoke                    skip Gate 1 (CLI contract smoke test; default ON)
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
    # with lint + audit + pylint + smoke + prefetch + simulate gates ON. Only one VM runs at a time.
    import argparse
    p = argparse.ArgumentParser(
        prog="build-fleet-sequential.py",
        description=(
            "Sequential fleet build of unattended Linux GNOME-on-Wayland VMs. "
            "Default (no args) builds all supported distros with lint, audit, "
            "pylint, smoke, prefetch + simulate gates ON; only one VM runs at a time."
        ),
    )
    p.add_argument(
        "--distros", default=None,
        help="Comma-separated list of distros to build, in order (default: all supported)",
    )
    p.add_argument("--no-lint", action="store_true",
                   help="Skip Gate 0 (static template lint)")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip Gate 0b (cross-distro package parity audit)")
    p.add_argument("--no-pylint", action="store_true",
                   help="Skip Gate 0c (pylint over every tracked .py)")
    p.add_argument("--no-tests", action="store_true",
                   help="Skip Gate 0d (the unittest suite in tests/)")
    p.add_argument("--no-smoke", action="store_true",
                   help="Skip Gate 1 (CLI contract smoke test)")
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
    # `--distros` is validated BEFORE `ensure_out_root()` and every gate, so a
    # value that matches nothing costs seconds rather than an hour. This used
    # to build BOTH distros: `--distros ","` split to an empty list, which the
    # `if args.distros else []` fallback below could not tell apart from the
    # flag being absent, so the fleet fell through to the full multi-hour real
    # build loop -- the same "silently does the expensive thing" class of
    # footgun as the contradictory-flag pair rejected just below.
    only_distros = ([s.strip() for s in args.distros.split(",") if s.strip()]
                    if args.distros else [])
    # Validate the *parsed* selector, not the raw string: `--distros ","`
    # strips to the truthy "," yet selects no distro, which used to fall
    # through to the `if args.distros else []` empty list and silently build
    # BOTH distros -- indistinguishable from the flag being absent.
    if args.distros is not None and not only_distros:
        log_master(
            f"ERROR: --distros {args.distros!r} matches no distro. Pass a "
            f"comma-separated list from {list(DISTROS)}, or omit the flag to "
            f"build all of them."
        )
        sys.exit(2)
    if only_distros:
        # Validate against known DISTROS so a typo doesn't silently no-op.
        unknown = [d for d in only_distros if d not in DISTROS]
        if unknown:
            log_master(f"ERROR: --distros contains unknown distros: {unknown}")
            log_master(f"Known: {DISTROS}")
            sys.exit(2)
    skip_lint = args.no_lint
    skip_audit = args.no_audit
    skip_pylint = args.no_pylint
    skip_tests = args.no_tests
    skip_smoke = args.no_smoke
    prefetch_only = args.prefetch_only
    skip_prefetch = args.no_prefetch
    simulate_only = args.simulate_only
    skip_simulate = args.no_simulate
    skip_preflight = args.no_preflight
    start_from = args.start_from

    # Reject contradictory flag pairs BEFORE anything runs. Each of these used
    # to resolve the wrong way and silently do the expensive thing:
    #   --simulate-only --no-simulate  ->  simulate was skipped AND the early
    #       `return` (which lived INSIDE the `if not skip_simulate:` block)
    #       never ran, so the fleet fell through to the FULL multi-hour real
    #       build loop. That is the single worst footgun in the CLI: "I only
    #       wanted the dry run" booted two real VMs.
    #   --prefetch-only --no-prefetch  ->  the warm-up was skipped, nothing
    #       else was meant to run, and we returned instantly claiming DONE.
    #   --distros and --start-from that leave nothing to build -> checked
    #       further down (needs the parsed list).
    for only_flag, skip_flag, enabled, skipped in (
        ("--simulate-only", "--no-simulate", simulate_only, skip_simulate),
        ("--prefetch-only", "--no-prefetch", prefetch_only, skip_prefetch),
    ):
        if enabled and skipped:
            log_master(
                f"ERROR: {only_flag} conflicts with {skip_flag}: {only_flag} "
                f"asks for exactly the phase {skip_flag} disables, so there is "
                f"nothing left to do. (Without this check the fleet fell "
                f"through to the real build loop.)"
            )
            sys.exit(2)

    # ~/VMs (where the master log and every build land) is created here, not
    # at constants-import time -- importing linux_vm.fleet must not have a
    # filesystem side effect.
    ensure_out_root()

    overall_start = time.time()
    if only_distros:
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
    log_master(f"=== build-fleet-sequential: lint + audit + pylint + tests + smoke + prefetch + simulate + verify + cache + continue-on-failure ===")
    log_master(f"=== distros (in order): {distros_to_build} ===")

    # Gate 0 -- static template lint (default ON, skipped with --no-lint).
    # Cheapest gate and the only one that catches Jinja/YAML/marker
    # regressions, so it runs before we spend anything on downloads.
    if not skip_lint:
        if not run_lint_gate():
            sys.exit(5)

    # Gate 0b -- cross-distro package parity audit (default ON, skipped
    # with --no-audit). Static; fails if a required app category dropped
    # out of either template's package list -- the cheap pre-download
    # equivalent of the verify-block that otherwise only fires hours into
    # a real build.
    if not skip_audit:
        if not run_audit_gate():
            sys.exit(7)

    # Gate 0c -- Python source lint (default ON, skipped with
    # --no-pylint). Static, whole-tree: a Python error in linux_vm/ or
    # scripts/ would otherwise only surface minutes-to-hours into the
    # run (or worse, never, if it sits on an error path).
    if not skip_pylint:
        if not run_pylint_gate():
            sys.exit(8)

    # Gate 0d -- unit-test suite (default ON, skipped with --no-tests).
    # Static-ish (~3 s) and the only gate that EXECUTES logic rather than
    # rendering it: password quoting for numeric/bool/quote-bearing values,
    # the dash-to-dock GNOME-version condition against a pretty-printed
    # metadata.json, and the fleet control-flow mappings.
    if not skip_tests:
        if not run_tests_gate():
            sys.exit(9)

    # Gate 1 -- CLI contract smoke test (default ON, skipped with
    # --no-smoke). Catches orchestrator <-> setup_vm.py argparse
    # mismatches and broken fleet wiring before we spend anything on
    # downloads; runs ubuntu-lts --prefetch/--simulate without a VM.
    if not skip_smoke:
        if not run_smoke_gate():
            sys.exit(6)

    # Orphan-VM cleanup runs HERE, before prefetch and before the ~15 min
    # simulate gate -- not just before the real build loop. An orphan left by
    # an earlier interrupted run (a force-killed fleet, a killed QEMU) keeps
    # its vCPUs, RAM and hostfwd ports busy for the whole run; on a busy host
    # that is the CPU-starvation condition that soft-locks the guest kernel
    # and turns a 15-min simulate into a timeout. Killed upfront, it costs 3 s.
    # Skipped in prefetch-only / simulate-only mode: those modes exist to be
    # cheap and non-destructive, and must not take out a VM the operator has
    # deliberately booted by hand.
    if skip_preflight:
        log_master("preflight: SKIPPED (--no-preflight) -- caller is responsible for VM cleanup")
    elif prefetch_only or simulate_only:
        log_master("preflight: SKIPPED (prefetch-only/simulate-only mode leaves running VMs alone)")
    else:
        preflight_cleanup()

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

    # Host-served GNOME extension zips: pre-fetch once, serve for the whole
    # fleet so every distro installs dash-to-dock deterministically
    # (no flaky guest->GitHub fetch at build time). Passed to each build;
    # torn down in the finally block below.
    gnome_ext_proc = None
    gnome_ext_url = ""
    try:
        # CACHE_ROOT, not a second hand-written ~/VMs/cache literal: setup_vm.py
        # warms the extension zips into default_cache_dir(), so if these two
        # ever disagree the fleet serves a directory the prefetch never wrote.
        gnome_ext_proc, _gnome_ext_port, gnome_ext_url = start_gnome_ext_server(
            CACHE_ROOT
        )
        log_master(f"=== GNOME extension server: {gnome_ext_url} (host-served) ===")
    except Exception as e:  # noqa: BLE001 - non-fatal; guest falls back to GitHub
        log_master(f"!!! GNOME extension server failed to start ({e}); guest will use GitHub")

    n = 0
    failed_distros = []
    try:
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
    finally:
        # In a finally, not after the loop: the server is spawned detached
        # (start_new_session=True) specifically to survive process exit, so
        # an aborted build (KeyboardInterrupt, unexpected exception) would
        # otherwise leak an orphaned http.server on loopback.
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
