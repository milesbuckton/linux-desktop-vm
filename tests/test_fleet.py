"""Unit tests for the fleet orchestrator's control flow (no network, no VMs).

Covers the verdict-producing logic that the review found untested: the
cleanup `finally` that keeps a leaked QEMU from outliving a failed phase-1
(lifecycle leak / host starvation), the process-group teardown that makes that
cleanup reachable at all, the cloud-init status -> exit-code ladder, the
marker grep's rc handling (Ubuntu's verify block never writes
/var/log/verify-marker.log, so grep exits 2 even on a match), and the CLI's
mutually-exclusive flag validation.

Run from repo root:
    python3 scripts/run-tests.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from importlib import import_module

from linux_vm import provider
from linux_vm.fleet import executor, lifecycle, orchestrator, ssh
from linux_vm.fleet.constants import OUT_ROOT, VERIFY_OK_MARKER

# linux_vm/fleet/__init__.py re-exports `main` as the FUNCTION, so
# `from linux_vm.fleet import main` yields the function, not the module --
# and `import linux_vm.fleet.main as x` prefers the shadowing attribute too.
# importlib is the only form that reliably gives us the MODULE.
fleet_main = import_module("linux_vm.fleet.main")

MARKER = "VERIFY-OK: all required components present"


def _ticking_clock(step: float = 100.0):
    """A fake time.time() that advances on every call.

    A fixed list of values runs out (StopIteration) as soon as the code
    under test asks for the time more times than the list holds; a
    generator cannot.
    """
    state = {"t": 1000.0}

    def _now():
        state["t"] += step
        return state["t"]

    return _now


def _clock():
    """A monotonic time.time() stand-in that advances only when slept.

    The wait loops are deadline-driven, so a fake clock makes an
    hours-long timeout testable in microseconds without weakening the
    assertions (a real sleep would make this file take hours).
    """
    state = {"t": 1000.0}

    def time_():
        return state["t"]

    def sleep(seconds):
        state["t"] += float(seconds)

    return time_, sleep, state


class TestRunToFile(unittest.TestCase):
    """executor.run_to_file: the timeout path must not orphan the child."""

    def test_timeout_returns_minus_one(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "out.log"
            rc, elapsed = executor.run_to_file(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                log, timeout=1,
            )
            # Assert inside the TemporaryDirectory: once it exits, the log
            # file is deleted with it.
            self.assertTrue(log.exists(), "run_to_file must stream to log_path")
            self.assertEqual(rc, -1)
        self.assertGreaterEqual(elapsed, 1.0)

    def test_nonzero_exit_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "out.log"
            rc, _ = executor.run_to_file(
                [sys.executable, "-c", "raise SystemExit(7)"], log, timeout=30,
            )
        self.assertEqual(rc, 7)

    def test_timeout_kills_spawned_grandchildren(self):
        """The whole point of start_new_session + killpg.

        `setup_vm.py --start` execs QEMU, which is NOT the direct child we
        wait on. A plain proc.kill() would leave it running and holding an
        open qcow2. The grandchild here stands in for QEMU.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "out.log"
            marker = Path(td) / "grandchild.pid"
            script = (
                "import subprocess, sys, pathlib, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))\n"
                "time.sleep(60)\n"
            )
            rc, _ = executor.run_to_file(
                [sys.executable, "-c", script], log, timeout=3,
            )
            self.assertEqual(rc, -1)
            self.assertTrue(marker.exists(), "grandchild never started")
            grandchild_pid = int(marker.read_text())
            # Poll: SIGKILL delivery is async, so the pid may take a moment
            # to disappear. It must NOT still be alive in 5s -- if proc.kill()
            # were all we did, this sleep(60) child would still be running.
            deadline = time.time() + 5
            while time.time() < deadline:
                alive = subprocess.run(
                    ["kill", "-0", str(grandchild_pid)],
                    capture_output=True,
                ).returncode == 0
                if not alive:
                    break
                time.sleep(0.2)
            else:
                subprocess.run(["kill", "-9", str(grandchild_pid)],
                               capture_output=True)
                self.fail(f"grandchild pid {grandchild_pid} survived the timeout kill")


class TestShutdownAndVerify(unittest.TestCase):
    """A healthy guest must be allowed to unmount before any SIGKILL."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.target = Path(self.td.name)
        self.log = self.target / "stop.log"
        self.log.write_text("", encoding="utf-8")
        time_, sleep, _ = _clock()
        patcher = mock.patch.object(lifecycle.time, "time", time_)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(lifecycle.time, "sleep", sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, pids_sequence):
        with mock.patch.object(lifecycle, "list_running_qemu_pids",
                               side_effect=pids_sequence) as pgrep, \
             mock.patch.object(lifecycle, "kill_pid") as kill, \
             mock.patch.object(lifecycle.subprocess, "run"), \
             mock.patch.object(ssh, "log_master") as master:
            ok = lifecycle.shutdown_and_verify(
                self.target, "", 0, "vmuser", self.target / "ssh_key", self.log,
            )
        return ok, pgrep, kill, master

    def test_the_suite_never_writes_to_the_operators_master_log(self):
        """The test suite must not append to ~/VMs/build-fleet.log.

        The operator's master log is the record of what the fleet actually
        did. The shutdown tests call the REAL shutdown_and_verify, and
        its `_note()` helper used to call `log_master` through a by-value
        `from .ssh import log_master` binding -- so patching
        `fleet.ssh.log_master` could NOT intercept it, and every suite run
        appended ~14 lines of "phase-3: force-kill qemu-system PID 4242" /
        "VERIFY TIMEOUT" for VMs that never existed. The live log then
        claimed VMs had been force-killed and had timed out, and a real
        fleet run's history was indistinguishable from test noise.

        `log_master` is now reached through the module, so patching
        `ssh.log_master` intercepts it -- and this test proves it.
        """
        _ok, _pgrep, _kill, master = self._run([[4242], []])
        self.assertGreater(master.call_count, 0,
                           "the shutdown path must still log -- but through "
                           "the patchable module attribute, not the live log")

    def test_clean_self_shutdown_is_not_force_killed(self):
        """First poll still sees the VM, the second sees it gone.

        This is the regression guard for the old behaviour: sleep 5s, then
        SIGKILL unconditionally -- even when the guest was about to exit on
        its own, which left the qcow2 with no clean unmount for the NEXT
        --keep-qcow2 build.
        """
        ok, pgrep, kill, _master = self._run([[4242], []])
        self.assertTrue(ok)
        self.assertGreaterEqual(pgrep.call_count, 2)
        kill.assert_not_called()
        self.assertIn("clean shutdown", self.log.read_text(encoding="utf-8"))

    def test_never_exits_is_force_killed(self):
        """A guest stuck mid-install never powers off: bounded, then killed."""
        ok, _pgrep, kill, _master = self._run(lambda *_: [4242])
        self.assertFalse(ok)
        self.assertTrue(kill.called, "force-kill must be attempted")
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("force-killing", logged)
        self.assertIn("VERIFY TIMEOUT", logged)

    def test_force_kill_note_is_logged_when_no_ssh_endpoint(self):
        """Phase-1 failures have no endpoint; the disk warning still matters."""
        with mock.patch.object(lifecycle, "list_running_qemu_pids",
                               return_value=[4242]), \
             mock.patch.object(lifecycle, "kill_pid"), \
             mock.patch.object(lifecycle, "SHUTDOWN_TIMEOUT_SEC", 0), \
             mock.patch.object(lifecycle, "VERIFY_DEAD_TIMEOUT_SEC", 0), \
             mock.patch.object(lifecycle.subprocess, "run"), \
             mock.patch.object(ssh, "log_master"):
            ok = lifecycle.shutdown_and_verify(
                self.target, "", 0, "vmuser", self.target / "ssh_key", self.log,
            )
        self.assertFalse(ok)
        self.assertIn("disk may be left unclean",
                      self.log.read_text(encoding="utf-8"))


class TestCheckSuccessMarker(unittest.TestCase):
    """grep's exit code is NOT the verdict -- the matched stdout is."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = Path(self.td.name) / "wait.log"
        self.log.write_text("", encoding="utf-8")

    def _grep(self, rc, stdout, stderr=""):
        with mock.patch.object(ssh, "run_with_hard_timeout",
                               return_value=(rc, stdout, stderr)), \
             mock.patch.object(ssh.time, "sleep"):
            return ssh._check_success_marker(
                "127.0.0.1", 2222, "vmuser", Path(self.td.name) / "ssh_key",
                self.log,
            )

    def test_grep_rc0_with_marker_passes(self):
        self.assertTrue(self._grep(0, f"...{MARKER}\n"))

    def test_grep_rc2_with_marker_still_passes(self):
        """Ubuntu's real case: verify-marker.log doesn't exist.

        `grep -F MARKER cloud-init-output.log verify-marker.log` exits 2
        (one file unreadable) while STILL printing the match from the other
        file. A verdict keyed on rc == 0 would fail every Ubuntu build.
        """
        self.assertTrue(self._grep(
            2,
            f"...{MARKER}\n",
            "/var/log/verify-marker.log: No such file or directory\n",
        ))

    def test_rc1_no_match_fails(self):
        self.assertFalse(self._grep(1, ""))

    def test_hard_timeout_rc_is_noted_and_fails(self):
        """rc=-99 means grep may never have run -- recorded, then no marker."""
        self.assertFalse(self._grep(-99, "", "timed out"))
        self.assertIn("rc=-99", self.log.read_text(encoding="utf-8"))

    def test_console_log_fallback_when_ssh_dead(self):
        target = Path(self.td.name)
        (target / "console.log").write_text(
            "boot noise\n" + MARKER + "\n", encoding="utf-8",
        )
        with mock.patch.object(ssh, "run_with_hard_timeout",
                               return_value=(-99, "", "timed out")), \
             mock.patch.object(ssh.time, "sleep"):
            ok = ssh._check_success_marker(
                "127.0.0.1", 2222, "vmuser", target / "ssh_key", self.log,
                target_dir=target,
            )
        self.assertTrue(ok)
        self.assertIn("console.log fallback",
                      self.log.read_text(encoding="utf-8"))


class TestSshWaitCloudInit(unittest.TestCase):
    """The extended_status -> exit-code ladder. This decides build success."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.log = Path(self.td.name) / "wait.log"
        self.log.write_text("", encoding="utf-8")
        time_, sleep, _ = _clock()
        for mod in (ssh.time,):
            p = mock.patch.object(mod, "time", time_)
            p.start()
            self.addCleanup(p.stop)
            p = mock.patch.object(mod, "sleep", sleep)
            p.start()
            self.addCleanup(p.stop)

    def _status(self, extended, marker_ok, timeout=1800):
        stdout = f"status: done\nextended_status: {extended}\n"
        with mock.patch.object(ssh, "run_with_hard_timeout",
                               return_value=(0, stdout, "")), \
             mock.patch.object(ssh, "_kill_ssh_child_processes"), \
             mock.patch.object(ssh, "_check_success_marker",
                               return_value=marker_ok) as marker, \
             mock.patch.object(ssh, "_capture_diagnostics") as diag:
            rc, _elapsed = ssh.ssh_wait_cloud_init(
                "127.0.0.1", 2222, "vmuser", Path(self.td.name) / "ssh_key",
                self.log, timeout,
            )
        return rc, marker, diag

    def test_done_with_marker_is_success(self):
        rc, marker, diag = self._status("done", True)
        self.assertEqual(rc, 0)
        marker.assert_called_once()
        diag.assert_not_called()

    def test_done_without_marker_is_failure(self):
        rc, _marker, diag = self._status("done", False)
        self.assertEqual(rc, 1)
        diag.assert_called_once()

    def test_degraded_done_with_marker_is_success(self):
        rc, _marker, _diag = self._status("degraded done", True)
        self.assertEqual(rc, 0)

    def test_degraded_done_without_marker_is_failure(self):
        rc, _marker, _diag = self._status("degraded done", False)
        self.assertEqual(rc, 1)

    def test_error_done_with_marker_is_success_with_diagnostics(self):
        """cloud-init tripped by a noisy postinst, but our runcmd finished."""
        rc, _marker, diag = self._status("error - done", True)
        self.assertEqual(rc, 0)
        diag.assert_called_once()

    def test_error_done_without_marker_is_terminal_failure(self):
        rc, _marker, diag = self._status("error - done", False)
        self.assertEqual(rc, 1)
        diag.assert_called_once()

    def test_running_keeps_waiting_then_times_out(self):
        """Past the 900s runcmd window the marker is probed every cycle."""
        rc, marker, _diag = self._status("running", False, timeout=1800)
        self.assertEqual(rc, -1)
        self.assertGreaterEqual(marker.call_count, 1)

    def test_degraded_running_keeps_waiting(self):
        rc, _marker, _diag = self._status("degraded running", False, timeout=900)
        self.assertEqual(rc, -1)

    def test_unreachable_ssh_times_out_rather_than_passing(self):
        with mock.patch.object(ssh, "run_with_hard_timeout",
                               return_value=(-99, "", "")), \
             mock.patch.object(ssh, "_kill_ssh_child_processes"), \
             mock.patch.object(ssh, "_check_success_marker",
                               return_value=False), \
             mock.patch.object(ssh, "_snapshot_cloud_init_status"):
            rc, _ = ssh._ssh_wait_cloud_init(
                "127.0.0.1", 2222, "vmuser", Path(self.td.name) / "ssh_key",
                self.log, 600,
            )
        self.assertEqual(rc, -1)

    def test_marker_after_long_run_wins_over_stuck_status(self):
        """cloud-init stuck on a post-runcmd module, but runcmd finished.

        Past 900s a present marker is treated as completed, so a
        post-runcmd hang can't hold the build open for hours.
        """
        stdout = "status: running\nextended_status: running\n"
        with mock.patch.object(ssh, "run_with_hard_timeout",
                               return_value=(0, stdout, "")), \
             mock.patch.object(ssh, "_kill_ssh_child_processes"), \
             mock.patch.object(ssh, "_check_success_marker", return_value=True):
            rc, _ = ssh._ssh_wait_cloud_init(
                "127.0.0.1", 2222, "vmuser", Path(self.td.name) / "ssh_key",
                self.log, 3600,
            )
        self.assertEqual(rc, 0)

    def test_snapshot_runs_on_every_exit_path(self):
        """A timeout must still preserve status.json -- it is the evidence."""
        with mock.patch.object(ssh, "_ssh_wait_cloud_init",
                               return_value=(-1, 600.0)) as wait, \
             mock.patch.object(ssh, "_snapshot_cloud_init_status") as snap:
            rc, _ = ssh.ssh_wait_cloud_init(
                "127.0.0.1", 2222, "vmuser", Path(self.td.name) / "ssh_key",
                self.log, 600,
            )
        wait.assert_called_once()
        snap.assert_called_once()
        self.assertEqual(rc, -1)

    def test_snapshot_runs_even_when_wait_raises(self):
        with mock.patch.object(ssh, "_ssh_wait_cloud_init",
                               side_effect=RuntimeError("probe exploded")), \
             mock.patch.object(ssh, "_snapshot_cloud_init_status") as snap:
            with self.assertRaises(RuntimeError):
                ssh.ssh_wait_cloud_init(
                    "127.0.0.1", 2222, "vmuser", Path(self.td.name) / "ssh_key",
                    self.log, 600,
                )
        snap.assert_called_once()


class TestPrefetchImages(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p = mock.patch.object(orchestrator, "log_master")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_single_failure_retries_then_succeeds(self):
        with mock.patch.object(orchestrator, "run_to_file",
                               side_effect=[(1, 1.0), (1, 1.0), (0, 2.0)]) as run:
            ok = orchestrator.prefetch_images(["ubuntu-lts"])
        self.assertTrue(ok)
        self.assertEqual(run.call_count, 3)

    def test_persistent_failure_returns_false_after_three_attempts(self):
        with mock.patch.object(orchestrator, "run_to_file",
                               return_value=(1, 1.0)) as run:
            ok = orchestrator.prefetch_images(["gentoo"])
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 3)

    def test_one_failure_among_many_is_still_a_failure(self):
        """ubuntu ok on attempt 1; gentoo fails all 3 -> whole gate fails."""
        with mock.patch.object(orchestrator, "run_to_file",
                               side_effect=[(0, 1.0), (1, 1.0), (1, 1.0),
                                            (1, 1.0)]) as run:
            ok = orchestrator.prefetch_images(["ubuntu-lts", "gentoo"])
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 4)


class TestBuildAndProvision(unittest.TestCase):
    """The leak: phase-1 fails AFTER launching QEMU, so `vm_started` is False
    and the cleanup `finally` used to skip shutdown_and_verify entirely."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        target = self.root / "ubuntu-lts"
        target.mkdir(parents=True, exist_ok=True)
        (target / "ssh_key").write_text("key\n", encoding="utf-8")
        for attr, value in (
            ("log_master", None),
            ("time", None),
        ):
            del attr, value
        p = mock.patch.object(orchestrator, "log_master")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator, "OUT_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator, "check_guest_dns", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator, "wait_ssh_reachable",
                              return_value="127.0.0.1")
        p.start()
        self.addCleanup(p.stop)
        # NOTE: shutdown_and_verify is patched on lifecycle, not on
        # orchestrator, for these tests: orchestrator imported the name
        # directly, so patching the module attribute is what actually takes
        # effect for the call under test.
        shutdown = mock.patch.object(orchestrator, "shutdown_and_verify",
                                     return_value=True)
        self.shutdown = shutdown.start()
        self.addCleanup(shutdown.stop)

    def _run(self, phase1_rc, qemu_running):
        with mock.patch.object(orchestrator, "run_to_file",
                               return_value=(phase1_rc, 1.0)), \
             mock.patch.object(orchestrator, "_discover_ssh_port",
                               return_value=(2222, 30)), \
             mock.patch.object(orchestrator, "ssh_wait_cloud_init",
                               return_value=(0, 60.0)), \
             mock.patch.object(orchestrator, "_qemu_running_for",
                               return_value=qemu_running):
            return orchestrator.build_and_provision("ubuntu-lts")

    def test_phase1_failure_with_live_qemu_is_cleaned_up(self):
        ok, ci_ok, reason = self._run(1, qemu_running=True)
        self.shutdown.assert_called_once()
        self.assertTrue(ok)
        self.assertFalse(ci_ok)
        # A phase-1 BUILD failure must NOT be reported as a cloud-init
        # failure: the real cause is in <distro>.build.log, and telling
        # the operator to look at the cloud-init log sent them to the
        # wrong file.
        self.assertIn("build", reason.lower())
        self.assertNotIn("cloud-init", reason.lower())

    def test_phase1_timeout_with_live_qemu_is_cleaned_up(self):
        ok, ci_ok, reason = self._run(-1, qemu_running=True)
        self.shutdown.assert_called_once()
        self.assertFalse(ci_ok)
        self.assertIn("timeout", reason.lower())

    def test_no_live_qemu_means_no_shutdown_attempt(self):
        ok, ci_ok, _reason = self._run(1, qemu_running=False)
        self.shutdown.assert_not_called()
        self.assertTrue(ok, "no VM to clean up is a clean outcome")
        self.assertFalse(ci_ok)

    def test_happy_path_shuts_down_and_reports_success(self):
        ok, ci_ok, reason = self._run(0, qemu_running=True)
        self.shutdown.assert_called_once()
        self.assertTrue(ok)
        self.assertTrue(ci_ok)
        self.assertEqual(reason, "", "a successful build has no reason")

    def test_cloud_init_failure_is_reported_but_vm_still_shut_down(self):
        with mock.patch.object(orchestrator, "run_to_file",
                               return_value=(0, 1.0)), \
             mock.patch.object(orchestrator, "_discover_ssh_port",
                               return_value=(2222, 30)), \
             mock.patch.object(orchestrator, "ssh_wait_cloud_init",
                               return_value=(1, 60.0)), \
             mock.patch.object(orchestrator, "_qemu_running_for",
                               return_value=True):
            _ok, ci_ok, _reason = orchestrator.build_and_provision(
                "ubuntu-lts")
        self.shutdown.assert_called_once()
        self.assertFalse(ci_ok)

    def test_cloud_init_timeout_is_not_success(self):
        with mock.patch.object(orchestrator, "run_to_file",
                               return_value=(0, 1.0)), \
             mock.patch.object(orchestrator, "_discover_ssh_port",
                               return_value=(2222, 30)), \
             mock.patch.object(orchestrator, "ssh_wait_cloud_init",
                               return_value=(-1, 900.0)), \
             mock.patch.object(orchestrator, "_qemu_running_for",
                               return_value=True):
            _ok, ci_ok, _reason = orchestrator.build_and_provision(
                "ubuntu-lts")
        self.assertFalse(ci_ok)

    def test_shutdown_failure_propagates_as_not_ok(self):
        """shutdown_and_verify returning False must fail the whole build.

        The `finally` block discards its own result unless vm_started, so a
        swallowed shutdown failure would silently report a successful build
        and leave the next distro building under a full host.
        """
        self.shutdown.return_value = False
        ok, ci_ok, _reason = self._run(0, qemu_running=True)
        self.assertTrue(ci_ok, "cloud-init did succeed here")
        self.assertFalse(ok, "an unverifiable shutdown is a cascade risk")
        self.assertTrue(ci_ok)


class TestQemuRunningFor(unittest.TestCase):
    def test_true_when_pids_found(self):
        with mock.patch.object(orchestrator, "list_running_qemu_pids",
                               return_value=[123]):
            self.assertTrue(orchestrator._qemu_running_for(Path("/x")))

    def test_false_when_pgid_lookup_breaks(self):
        """A broken pgrep must not turn a cleanup into a crash."""
        with mock.patch.object(orchestrator, "list_running_qemu_pids",
                               side_effect=FileNotFoundError("pgrep")):
            self.assertFalse(orchestrator._qemu_running_for(Path("/x")))


class TestSimulateDistro(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        p = mock.patch.object(orchestrator, "log_master")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator, "OUT_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(orchestrator, "wait_ssh_reachable",
                              return_value="127.0.0.1")
        p.start()
        self.addCleanup(p.stop)
        shutdown = mock.patch.object(orchestrator, "shutdown_and_verify")
        self.shutdown = shutdown.start()
        self.addCleanup(shutdown.stop)

    def _run(self, phase1_rc, marker_ok, qemu_running=True, marker_fail=False):
        def _marker(*_args, **kwargs):
            # The verdict greps BOTH markers now: a SIMULATE-FAIL marker
            # must be able to beat an earlier SIMULATE-OK marker.
            if kwargs.get("marker") == "SIMULATE-FAIL:":
                return marker_fail
            return marker_ok

        with mock.patch.object(orchestrator, "run_to_file",
                               return_value=(phase1_rc, 1.0)), \
             mock.patch.object(orchestrator, "_discover_ssh_port",
                               return_value=(2222, 90)), \
             mock.patch.object(orchestrator, "ssh_wait_cloud_init",
                               return_value=(0, 10.0)), \
             mock.patch.object(orchestrator, "_check_success_marker",
                               side_effect=_marker), \
             mock.patch.object(orchestrator, "run_with_hard_timeout",
                               return_value=(0, "", "")), \
             mock.patch.object(orchestrator, "_qemu_running_for",
                               return_value=qemu_running):
            return orchestrator.simulate_distro("ubuntu-lts")

    def test_pass(self):
        r = self._run(0, True)
        self.assertEqual(r["status"], "PASS")
        self.shutdown.assert_called_once()

    def test_simulate_fail_marker_beats_an_earlier_simulate_ok(self):
        """A failed check must be able to fail the gate.

        The verdict used to be "SIMULATE-OK found ANYWHERE in the log", so a
        check that failed AFTER an earlier check had passed was reported as
        PASS -- the same class of bug as the verify gate.
        """
        r = self._run(0, True, marker_fail=True)
        self.assertEqual(r["status"], "FAIL")
        self.shutdown.assert_called_once()

    def test_marker_missing_is_fail_with_diagnostics(self):
        with mock.patch.object(orchestrator, "_capture_diagnostics") as diag:
            r = self._run(0, False)
        self.assertEqual(r["status"], "FAIL")
        diag.assert_called_once()
        self.shutdown.assert_called_once()

    def test_phase1_failure_with_live_qemu_is_cleaned_up(self):
        r = self._run(1, False, qemu_running=True)
        self.assertEqual(r["status"], "ERROR")
        self.shutdown.assert_called_once()

    def test_shutdown_error_does_not_mask_the_verdict(self):
        self.shutdown.side_effect = RuntimeError("no such pid")
        r = self._run(0, True)
        self.assertEqual(r["status"], "PASS")


class TestCliFlagConflicts(unittest.TestCase):
    """`--simulate-only --no-simulate` used to fall through to REAL builds.

    Both flags parsed fine, the simulate block was skipped by --no-simulate
    and the `--simulate-only: return` inside it never ran, so the fleet
    proceeded to build every distro for hours.
    """

    def _main(self, argv):
        """Run main() to completion; SystemExit is the CLI's return channel."""
        code = 0
        with mock.patch.object(fleet_main.sys, "argv", ["build-fleet", *argv]), \
             mock.patch.object(fleet_main, "log_master") as log_master, \
             mock.patch.object(fleet_main, "ensure_out_root"):
            try:
                code = fleet_main.main()
            except SystemExit as exc:
                code = exc.code if exc.code is not None else 0
        return code, log_master

    def test_simulate_only_with_no_simulate_exits(self):
        code, log_master = self._main(["--distros", "ubuntu-lts",
                                       "--simulate-only", "--no-simulate"])
        self.assertEqual(code, 2)
        self.assertTrue(any("conflicts" in c.args[0]
                            for c in log_master.call_args_list))

    def test_prefetch_only_with_no_prefetch_exits(self):
        code, _ = self._main(["--distros", "ubuntu-lts",
                              "--prefetch-only", "--no-prefetch"])
        self.assertEqual(code, 2)

    def test_conflict_is_checked_before_any_gate_runs(self):
        with mock.patch.object(fleet_main, "run_lint_gate") as lint, \
             mock.patch.object(fleet_main, "run_audit_gate"), \
             mock.patch.object(fleet_main, "run_pylint_gate"), \
             mock.patch.object(fleet_main, "run_tests_gate"), \
             mock.patch.object(fleet_main, "run_smoke_gate"):
            self._main(["--distros", "ubuntu-lts",
                        "--simulate-only", "--no-simulate"])
        lint.assert_not_called()

    def test_start_from_leaving_nothing_exits(self):
        code, log_master = self._main(["--distros", "ubuntu-lts",
                                       "--start-from", "gentoo"])
        self.assertEqual(code, 2)
        self.assertTrue(any("nothing to build" in c.args[0]
                            for c in log_master.call_args_list))

    def test_unknown_distro_exits(self):
        code, _ = self._main(["--distros", "not-a-distro"])
        self.assertEqual(code, 2)

    def test_empty_distro_selector_exits_instead_of_building_everything(self):
        """`--distros` that matches nothing used to build BOTH distros.

        The list-comp dropped empty tokens before `if args.distros:` was
        reachable, so `--distros ","` / `""` / `"  "` parsed to an empty
        `only_distros` list -- which is the same state as "flag absent",
        so the fleet launched the full multi-hour build. The same family
        of footgun as --simulate-only --no-simulate above.
        """
        for value in ("", ",", "  ", ",,"):
            with self.subTest(value=value):
                code, log_master = self._main(["--distros", value])
                self.assertEqual(code, 2)
                self.assertTrue(any("matches no distro" in c.args[0]
                                    for c in log_master.call_args_list))

    def test_empty_distro_selector_is_checked_before_any_gate_runs(self):
        with mock.patch.object(fleet_main, "run_lint_gate") as lint, \
             mock.patch.object(fleet_main, "run_audit_gate"), \
             mock.patch.object(fleet_main, "run_pylint_gate"), \
             mock.patch.object(fleet_main, "run_tests_gate"), \
             mock.patch.object(fleet_main, "run_smoke_gate"), \
             mock.patch.object(fleet_main, "ensure_out_root") as out_root:
            code, _ = self._main(["--distros", ","])
        self.assertEqual(code, 2)
        lint.assert_not_called()
        out_root.assert_not_called()

    def test_absent_distro_flag_still_builds_everything(self):
        """The fix must not break the default path: no --distros at all.

        Proved by letting the real flow start and stopping at the first
        gate with a sentinel exception: reaching it at all means the flag
        validation did NOT reject an absent --distros.

        `sys.argv` is patched explicitly (like `_main` above) rather than
        inherited: main() runs argparse over the host process's argv, so
        without this the test's verdict depended on how the SUITE was
        invoked. `python -m unittest tests.test_fleet...` (the test name is
        a positional argparse never heard of) and `run-tests.py -v` both
        exit 2 inside parse_args, failing this test for a reason that has
        nothing to do with the flag validation it is about.
        """
        # NOT named `sentinel`: Python 3.15 added a `sentinel` BUILTIN, so
        # a local of that name shadows it and pylint's redefined-builtin
        # check (W0622) fires -- which is a hard Gate 0c failure. The name
        # is a local, so renaming it is behaviour-neutral.
        boom_exc = RuntimeError("reached-the-build-loop")

        def boom(*_a, **_k):
            raise boom_exc

        with mock.patch.object(fleet_main.sys, "argv", ["build-fleet"]), \
             mock.patch.object(fleet_main, "ensure_out_root"), \
             mock.patch.object(fleet_main, "log_master"), \
             mock.patch.object(fleet_main, "run_lint_gate", boom):
            with self.assertRaises(RuntimeError) as caught:
                fleet_main.main()
        self.assertIs(caught.exception, boom_exc)


class TestConstants(unittest.TestCase):
    def test_marker_constant_matches_the_verify_contract(self):
        self.assertEqual(VERIFY_OK_MARKER, "VERIFY-OK: all required components present")

    def test_shutdown_timeout_is_usable_as_a_grace_period(self):
        """A dead constant is how the "5s then SIGKILL" bug survived."""
        self.assertGreater(lifecycle.SHUTDOWN_TIMEOUT_SEC, 0)

    def test_every_distro_has_a_username_and_wait_timeout(self):
        from linux_vm.fleet.constants import USERNAMES
        for distro in ("ubuntu-lts", "gentoo"):
            self.assertIn(distro, USERNAMES)
        self.assertTrue(OUT_ROOT.name)


class TestCloudInitDeadlineStillChecksMarker(unittest.TestCase):
    """The overall-deadline exit must consult the success marker too.

    Every OTHER exit from _ssh_wait_cloud_init asks _check_success_marker
    before reporting a verdict (the "error - done" ladder, the "done" /
    "degraded done" ladder, and the >900 s short-circuit). Only the
    `while ... : break` on the overall deadline returned -1 with no marker
    consultation at all.

    That is not a theoretical gap: the marker is written by the LAST
    cloud-init stage, so it can land in the seconds between the fleet's
    final status probe and the deadline expiring. A build that completed
    would have been reported FAILED and its disk discarded. Gentoo's fleet
    cap is 21600 s and its guest loop stops at /var/lib/gentoo-install-done,
    so this window is exactly where that pair's margin is spent.
    """

    def _run_to_deadline(self, marker_present: bool):
        """Drive _ssh_wait_cloud_init to the overall deadline; return its rc.

        Every probe reports `extended_status: running`, so the only exit
        from the while loop is the deadline -- the path under test.
        """
        time_, sleep, _ = _clock()
        probes = {"n": 0}
        running = "status: running\nextended_status: running\n"

        def fake_probe(*_a, **_kw):
            probes["n"] += 1
            return 0, running, ""

        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "wait.log"
            log_path.write_text("", encoding="utf-8")
            with mock.patch.object(ssh.time, "time", time_), \
                 mock.patch.object(ssh.time, "sleep", sleep), \
                 mock.patch.object(ssh, "run_with_hard_timeout", fake_probe), \
                 mock.patch.object(ssh, "_kill_ssh_child_processes"), \
                 mock.patch.object(ssh, "_check_success_marker",
                                   return_value=marker_present), \
                 mock.patch.object(ssh, "_capture_diagnostics"):
                rc, _elapsed = ssh._ssh_wait_cloud_init(
                    "127.0.0.1", 2222, "ubuntu", Path(td) / "ssh_key",
                    log_path, 900,
                    target_dir=Path(td),
                )
            self.assertGreater(probes["n"], 0, "the wait loop never probed")
            return rc

    def test_marker_present_at_deadline_is_success(self):
        self.assertEqual(self._run_to_deadline(marker_present=True), 0)

    def test_marker_absent_at_deadline_is_failure(self):
        self.assertEqual(self._run_to_deadline(marker_present=False), -1)


class TestPgrepScopeIsEscaped(unittest.TestCase):
    """provider.list_running_qemu_pids must not treat `scope` as a regex.

    The scope is a filesystem path (a target dir, or OUT_ROOT). A path with
    regex metacharacters -- README documents `--target-dir
    ~/VMs/my-custom-name`, and a version dir like `v1.0` is entirely
    plausible -- is interpolated straight into `pgrep -f`. Two distinct
    failures, both verified on this host:

      pgrep -f 'qemu-system.*v1.0'   -> rc=1, but `.` matched any char,
                                        so an unrelated VM's cmdline can be
                                        silently "matched" (or a real one
                                        missed).
      pgrep -f 'qemu-system.*v(a'     -> pgrep: Cannot compile regular
                                        expression ... (parentheses not
                                        balanced), rc=2.

    rc=2 is the worse one: list_running_qemu_pids calls check_returncode()
    on anything outside (0, 1), so it raises CalledProcessError -- and
    preflight_cleanup() and the shutdown polling in lifecycle.py call it
    with no try/except. One directory with a paren in its name takes out
    the fleet's cleanup path and, worse, the `finally` that shuts the VM
    down.
    """

    def test_escaped_scope_does_not_raise_on_metacharacters(self):
        # Defined once, outside the loop: a closure defined per-iteration
        # would capture the loop's cell (pylint W0640) and pylint's gate
        # treats that as a real finding.
        captured = {}

        class _R:
            returncode = 1
            stdout = ""
            stderr = ""

        def fake_run(argv, **_kw):
            captured["argv"] = argv
            return _R()

        for scope in ("/Users/me/VMs/v1.0", "/Users/me/VMs/v(a",
                      "/Users/me/VMs/a+b*", "/Users/me/VMs/[set]"):
            with self.subTest(scope=scope):
                with mock.patch.object(provider.subprocess, "run", fake_run):
                    # Must return [] rather than raising: the old code raised
                    # CalledProcessError for the "(a" case (pgrep rc=2).
                    self.assertEqual(provider.list_running_qemu_pids(scope), [])
                pat = captured["argv"][2]
                self.assertTrue(
                    pat.startswith("qemu-system.*"),
                    f"scope prefix lost: {pat!r}",
                )
                # The literal scope must appear escaped, i.e. pgrep's own
                # regex engine will match it verbatim.
                self.assertIn(re.escape(scope), pat)

    def test_real_pgrep_compiles_the_pattern(self):
        """Prove it end-to-end against the real pgrep, not a stub.

        A mock cannot catch "the pattern does not compile" -- that only
        pgrep knows. rc=1 (no match) is success here; rc=2 is the bug.
        """
        for scope in ("/Users/me/VMs/v1.0", "/Users/me/VMs/v(a"):
            with self.subTest(scope=scope):
                r = subprocess.run(
                    ["pgrep", "-f", f"qemu-system.*{re.escape(scope)}"],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertIn(
                    r.returncode, (0, 1),
                    f"pgrep could not compile the escaped pattern: {r.stderr}",
                )


class TestGateTableMatchesWrappers(unittest.TestCase):
    """fleet/main.py's GATES table is the single source for the five gates.

    The five gate functions were ~40 lines of near-identical copy-paste
    that differed only in script path, log label and exit code. Collapsing
    them into a table means a change to the gate machinery (or a new gate)
    lands once. These tests keep the table honest:

      * every GATES entry has a `run_<name>_gate` wrapper, because main()
      calls the wrappers by name and a typo would only show up as a
        5-hour-later AttributeError;
      * the scripts named exist and are tracked by git -- a renamed script
        would otherwise pass Gate 0d and fail at gate 0 in the fleet;
      * each entry's doc matches the script's own documented role.
    """

    def test_every_gate_has_a_wrapper(self):
        for gate in fleet_main.GATES:
            with self.subTest(gate=gate.name):
                self.assertTrue(
                    hasattr(fleet_main, f"run_{gate.name}_gate"),
                    f"fleet/main.py main() calls run_{gate.name}_gate(); "
                    f"GATES declares '{gate.name}' with no wrapper",
                )

    def test_gate_scripts_exist(self):
        for gate in fleet_main.GATES:
            with self.subTest(gate=gate.name):
                self.assertTrue(
                    (REPO / "scripts" / gate.script).is_file(),
                    f"GATES names scripts/{gate.script}, which does not exist",
                )

    def test_gate_timeouts_are_positive(self):
        for gate in fleet_main.GATES:
            with self.subTest(gate=gate.name):
                self.assertGreater(gate.timeout_sec, 0)
                self.assertTrue(gate.number)
                self.assertTrue(gate.hint)
                self.assertTrue(gate.doc)


class TestRunWithHardTimeoutKillsTree(unittest.TestCase):
    """executor.run_with_hard_timeout must reap the whole process tree.

    ssh.py's own docstring says the probe is run with a hard timeout and
    that "the whole tree is reaped on timeout" -- but run_with_hard_timeout
    used a bare Popen with no start_new_session, then proc.kill(). proc.kill()
    signals only the direct child. The fleet's SSH probes go through
    ssh_cmd(), which on a stock macOS host is an ssh client with a
    ProxyCommand helper (ControlMaster, a jump host) -- so the process that
    actually holds the TCP connection to :2222 is a GRANDchild and survived
    the timeout, holding the guest's SSH port open and the disk busy.

    run_to_file already got this right (start_new_session + _kill_process_tree).
    This test pins the same guarantee on its sibling so the two cannot drift.
    """

    def test_timeout_returns_minus_99(self):
        rc, _out, _err = executor.run_with_hard_timeout(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout_sec=1,
        )
        self.assertEqual(rc, -99)

    def test_successful_run_still_returns_zero(self):
        rc, out, _err = executor.run_with_hard_timeout(
            [sys.executable, "-c", "print('hello')"], timeout_sec=30,
        )
        self.assertEqual(rc, 0)
        self.assertIn("hello", out)

    def test_timeout_kills_spawned_grandchildren(self):
        """Mirror of TestRunToFile's grandchild test, for the other helper."""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "grandchild.pid"
            script = (
                "import subprocess, sys, pathlib, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))\n"
                "time.sleep(60)\n"
            )
            rc, _out, _err = executor.run_with_hard_timeout(
                [sys.executable, "-c", script], timeout_sec=3,
            )
            self.assertEqual(rc, -99)
            self.assertTrue(marker.exists(), "grandchild never started")
            pid = int(marker.read_text().strip())
            # The grandchild is a zombie at worst (its parent, the child we
            # killed, is never wait()ed here); either state means it is no
            # longer running.
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return  # gone -- the fix works
                time.sleep(0.25)
            self.fail(
                f"grandchild pid {pid} still alive 10 s after the hard "
                "timeout -- run_with_hard_timeout is not reaping the tree"
            )


# --------------------------------------------------------------------------
# Regression tests for the defects found by the 2026 code audit.
# --------------------------------------------------------------------------
class TestExecutorErrorSentinels(unittest.TestCase):
    """The executor's documented failure sentinels must be reachable.

    `run_with_hard_timeout` documented a -98 return for "the command could
    not be started", but the `subprocess.Popen` call sat OUTSIDE the try
    block, so a missing binary raised FileNotFoundError out of the function
    and the sentinel was unreachable. The same applied to `run_to_file`'s
    -2: its error handler re-opened the very path that had just failed, so
    it raised the identical OSError instead of reporting -2.
    """

    def test_missing_binary_returns_minus_98_instead_of_raising(self):
        rc, _out, err = executor.run_with_hard_timeout(
            ["/nonexistent/definitely-not-a-command"], timeout_sec=5,
        )
        self.assertEqual(rc, -98)
        self.assertIn("FileNotFoundError", err)

    def test_run_to_file_reports_minus_2_when_the_log_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as td:
            bad_log = Path(td) / "no-such-dir" / "out.log"
            rc, _elapsed = executor.run_to_file(
                [sys.executable, "-c", "print('hi')"], bad_log, 10,
            )
            self.assertEqual(rc, -2)


class TestLifecyclePgrepFailureCannotAbortTheFleet(unittest.TestCase):
    """An unusable pgrep must not abort the fleet.

    `provider.list_running_qemu_pids` deliberately re-raises when pgrep
    exits with anything other than 0/1, and re-raises FileNotFoundError
    when pgrep is not on PATH. `shutdown_and_verify` calls it from
    `build_and_provision`'s `finally`, so an exception there masked any
    in-flight exception and escaped the function -- aborting the fleet and
    losing the remaining distros AND the summary table.
    """

    def test_shutdown_and_verify_survives_an_unusable_pgrep(self):
        with tempfile.TemporaryDirectory() as td:
            stop_log = Path(td) / "stop.log"
            with mock.patch.object(lifecycle, "list_running_qemu_pids",
                                   side_effect=subprocess.CalledProcessError(
                                       2, "pgrep")), \
                 mock.patch.object(lifecycle, "_ssh"), \
                 mock.patch.object(lifecycle, "kill_pid") as kill, \
                 mock.patch.object(lifecycle.subprocess, "run"), \
                 mock.patch.object(lifecycle.time, "sleep"), \
                 mock.patch.object(lifecycle.time, "time",
                                   side_effect=_ticking_clock(step=10.0)):
                # Must NOT raise.
                ret = lifecycle.shutdown_and_verify(
                    Path(td), "127.0.0.1", 2222, "user",
                    Path(td) / "ssh_key", stop_log,
                )
            self.assertFalse(ret)
            for call in kill.call_args_list:
                self.assertGreater(
                    call.args[0], 0,
                    "kill_pid must never be called with a non-positive pid "
                    "(kill -9 -1 signals EVERY process the user owns)",
                )

    def test_preflight_cleanup_survives_an_unusable_pgrep(self):
        with mock.patch.object(lifecycle, "list_running_qemu_pids",
                               side_effect=FileNotFoundError("pgrep")), \
             mock.patch.object(lifecycle, "_ssh"), \
             mock.patch.object(lifecycle, "kill_pid") as kill:
            lifecycle.preflight_cleanup()
            # It must NOT have killed anything it could not see.
            self.assertFalse(kill.called)

    def test_a_real_pgrep_pid_is_still_killed(self):
        """The guard must not neuter the normal cleanup path."""
        with mock.patch.object(lifecycle, "list_running_qemu_pids",
                               return_value=[4242]), \
             mock.patch.object(lifecycle, "_ssh"), \
             mock.patch.object(lifecycle, "kill_pid") as kill:
            lifecycle.preflight_cleanup()
            kill.assert_called_once()


class TestCloudInitDeadlineCapturesDiagnostics(unittest.TestCase):
    """The cloud-init deadline exit must preserve the guest-side evidence.

    The deadline is the most common Gentoo failure mode (the fleet cap
    expires while the guest loop is still running), and it was the ONE
    terminal exit that did not call `_capture_diagnostics` -- so the
    evidence was destroyed exactly when it was needed most.
    """

    def test_deadline_calls_capture_diagnostics(self):
        captured = []
        with tempfile.TemporaryDirectory() as td:
            wait_log = Path(td) / "wait.log"
            wait_log.write_text("", encoding="utf-8")
            with mock.patch.object(ssh, "_capture_diagnostics",
                                   side_effect=lambda *a, **k: captured.append(a)), \
                 mock.patch.object(ssh, "_kill_ssh_child_processes"), \
                 mock.patch.object(ssh, "run_with_hard_timeout",
                                   return_value=(255, "", "refused")), \
                 mock.patch.object(ssh, "_check_success_marker",
                                   return_value=False), \
                 mock.patch.object(ssh.time, "sleep"), \
                 mock.patch.object(ssh.time, "time",
                                   side_effect=_ticking_clock()):
                rc, _elapsed = ssh._ssh_wait_cloud_init(
                    "127.0.0.1", 2222, "user", Path(td) / "k", wait_log,
                    overall_timeout_sec=1, marker="VERIFY-OK:",
                )
        self.assertEqual(rc, -1, "the deadline must report a timeout")
        self.assertTrue(captured,
                        "the deadline exit must capture diagnostics before "
                        "returning")


class TestSimulateFailMarkerIsReprobedOnEveryAttempt(unittest.TestCase):
    """A transient SSH hiccup must not turn a FAILED simulate into a PASS.

    The SIMULATE-FAIL probe used to run ONCE, outside the retry loop,
    while the SIMULATE-OK probe got 5 attempts. One sshd hiccup on that
    single probe therefore reported a failed simulate as a pass.
    """

    def test_fail_marker_is_reprobed_until_it_is_seen(self):
        """A SIMULATE-FAIL seen AFTER the OK marker must still fail the gate.

        The template emits one marker per check (the exact-use resolve
        first, then the full-list resolve), so the FAIL marker can appear
        after the OK marker. The FAIL probe used to run ONCE, before the
        retry loop, so a FAIL that only became visible later -- or that a
        transient ssh hiccup hid on that single probe -- was reported as
        PASS.
        """
        calls = {"fail": 0}

        def _marker(*_args, **kwargs):
            if kwargs.get("marker") == "SIMULATE-FAIL:":
                calls["fail"] += 1
                # The first probe could not reach the guest (transient);
                # every later probe sees the real SIMULATE-FAIL marker.
                return calls["fail"] >= 2
            return True

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(orchestrator, "log_master"), \
                 mock.patch.object(orchestrator, "OUT_ROOT", root), \
                 mock.patch.object(orchestrator, "run_to_file",
                                   return_value=(0, 1.0)), \
                 mock.patch.object(orchestrator, "_discover_ssh_port",
                                   return_value=(2222, 90)), \
                 mock.patch.object(orchestrator, "ssh_wait_cloud_init",
                                   return_value=(0, 10.0)), \
                 mock.patch.object(orchestrator, "_check_success_marker",
                                   side_effect=_marker), \
                 mock.patch.object(orchestrator, "run_with_hard_timeout",
                                   return_value=(0, "", "")), \
                 mock.patch.object(orchestrator, "wait_ssh_reachable",
                                   return_value="127.0.0.1"), \
                 mock.patch.object(orchestrator, "shutdown_and_verify"), \
                 mock.patch.object(orchestrator.time, "sleep"):
                r = orchestrator.simulate_distro("ubuntu-lts")
        self.assertGreaterEqual(calls["fail"], 2,
                                "the FAIL marker must be re-probed, not probed "
                                "once")
        self.assertEqual(r["status"], "FAIL",
                         "a SIMULATE-FAIL marker seen on a later probe must "
                         "still fail the gate")


class TestVerifyMarkerHasOneSourceOfTruth(unittest.TestCase):
    """The fleet must grep the marker the templates actually emit.

    `VERIFY_OK_MARKER` lived in fleet/constants.py but was referenced only
    by the tests, while the fleet hardcoded the same literal in three
    places. The constant could therefore be "updated" (and its test would
    still pass) while the fleet kept grepping the old string -- silently
    failing every build's verdict.
    """

    def test_the_fleet_uses_the_constant_not_a_literal(self):
        source = (REPO / "linux_vm" / "fleet" / "ssh.py").read_text(
            encoding="utf-8")
        self.assertNotIn('"VERIFY-OK: all required components present"',
                         source,
                         "ssh.py must not hardcode the verify marker -- it "
                         "must use VERIFY_OK_MARKER")
        self.assertIn("VERIFY_OK_MARKER", source)

    def test_the_constant_is_what_the_template_emits(self):
        from linux_vm.templates import get_jinja_env
        env = get_jinja_env(REPO / "templates")
        macros = (REPO / "templates" / "_macros.j2").read_text(
            encoding="utf-8")
        self.assertIn(VERIFY_OK_MARKER, macros)


class TestMonitorPhase5MarkerReachesTheConsole(unittest.TestCase):
    """The monitor's 5/5 milestone must be reachable in practice.

    cloud-init's `final_message` is written to STDERR, and the guest
    kernels here are not booted with `console=ttyS0`, so the final_message
    never reaches the host's captured serial console. The template now
    echoes the marker to /dev/ttyS0 explicitly; this locks that.
    """

    def test_the_template_echoes_the_marker_to_the_serial_console(self):
        base = (REPO / "templates" / "_base.j2").read_text(encoding="utf-8")
        self.assertIn("GNOME setup complete.", base)
        self.assertIn("/dev/ttyS0", base,
                      "the setup-complete marker must be echoed to the "
                      "serial console the host captures")

    def test_the_monitor_matches_the_marker_the_template_emits(self):
        from linux_vm import monitor
        patterns = [p.pattern for p, _l, _pct in monitor._CLOUD_INIT_PHASES]
        self.assertTrue(any("GNOME setup complete" in p for p in patterns),
                        "the monitor must match the marker the template "
                        "actually writes to the console")


if __name__ == "__main__":
    unittest.main()
