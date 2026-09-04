"""SSH-based orchestration helpers: DNS check, SSH readiness, cloud-init
monitoring, marker verification, and diagnostic capture."""
from __future__ import annotations
import os
import re
import shlex
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .constants import SSH, MASTER_LOG
from .executor import run_with_hard_timeout


def log_master(msg: str) -> None:
    """Write one timestamped line to the fleet master log AND stdout.

    Mirrors the pre-refactor behaviour exactly: raw line to stdout with a
    flush (no ANSI colour / log() wrapper) so piped and redirected output
    stays greppable, plus an append to MASTER_LOG (OUT_ROOT/build-fleet.log).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with MASTER_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    import sys
    sys.stdout.write(line)
    sys.stdout.flush()


def ssh_cmd(host: str, port: int, username: str, ssh_key: str | Path,
             remote_cmd: str, *, connect_timeout: int = 30,
             server_alive: bool = False, batch_mode: bool = True) -> list[str]:
    """Build the canonical `ssh` argv used across the fleet orchestrator.

    Centralising the option block keeps the 7 call sites from drifting
    (a missing BatchMode / ServerAlive flag at one site was a real bug).
    """
    opts = [
        "-i", str(ssh_key),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={connect_timeout}",
    ]
    if server_alive:
        opts += ["-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
    if batch_mode:
        opts += ["-o", "BatchMode=yes"]
    opts += ["-p", str(port), f"{username}@{host}"]
    return [SSH, *opts, remote_cmd]


def check_guest_dns(host: str, port: int, username: str, ssh_key_str: str, mirror: str) -> bool:
    """SSH into the guest and verify DNS resolves the distro's mirror."""
    cmd = ssh_cmd(
        host, port, username, ssh_key_str,
        f"getent hosts {mirror}",
        connect_timeout=10,
    )
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True
        print(f"  guest DNS check: getent hosts {mirror} -> exit {r.returncode}: {r.stderr.strip()}")
    except Exception as e:
        print(f"  guest DNS check: {e}")
    return False


def wait_ssh_reachable(host: str, port: int, timeout: int) -> str | None:
    """Wait for TCP port `port` to accept connections on `host`.

    Returns the host that became reachable, or None on timeout.
    """
    deadline = time.time() + timeout
    current = host
    while time.time() < deadline:
        try:
            with socket.create_connection((current, port), timeout=5):
                return current
        except Exception:
            pass
        time.sleep(5)
    return None


def _kill_ssh_child_processes(ssh_key: Path) -> None:
    """Kill orphaned `ssh` clients belonging to this VM's key.

    The pgrep pattern is anchored on the per-VM private key path (unique
    to this target dir) instead of a loose user@host:port match, so it can
    never match the orchestrator's own invocation or an unrelated
    interactive session. pgrep excludes itself by design; we also skip our
    own PID defensively.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"ssh.*{ssh_key}"],
            capture_output=True, text=True, timeout=10,
        )
        self_pid = str(os.getpid())
        for pid in result.stdout.split():
            if not pid.strip().isdigit():
                continue
            if pid.strip() == self_pid:
                continue
            try:
                subprocess.run(["kill", "-9", pid.strip()], capture_output=True, timeout=5)
            except Exception:
                pass
    except Exception:
        pass


def _check_success_marker(host: str, port: int, username: str, ssh_key: Path,
                          log_path: Path,
                          marker: str = "VERIFY-OK: all required components present",
                          target_dir: Path | None = None) -> bool:
    """Return True if `marker` was emitted to cloud-init-output.log.

    Also checks /var/log/verify-marker.log, which distro verify blocks write
    (fsync'd) in addition to stdout. On long-running builds (notably Gentoo,
    whose runcmd waits ~40 min for the install service) cloud-init's stdout
    buffering can drop the final verify echo before this grep runs; the marker
    file is durably on disk the instant the verify entry returns, so it is the
    authoritative source.
    """
    # --- Primary: SSH into guest and grep cloud-init-output.log (and the marker file) ---
    # SSH may be flaky under heavy load.
    # Retry up to 3 times with 10s delays to ride out transient outages.
    for _ssh_attempt in range(3):
        try:
            rc, stdout, stderr = run_with_hard_timeout(
                ssh_cmd(
                    host, port, username, ssh_key,
                    f"sudo -n grep -F {shlex.quote(marker)} /var/log/cloud-init-output.log /var/log/verify-marker.log 2>&1 || echo NO_MARKER",
                    server_alive=True,
                ),
                timeout_sec=30,
            )
            if rc == 0 and marker in stdout:
                return True
        except Exception:
            pass
        if _ssh_attempt < 2:
            time.sleep(10)
    # --- Fallback: read host-side console.log when SSH is dead ---
    if target_dir is not None:
        console_log = target_dir / "console.log"
        if console_log.exists():
            try:
                # Read last 200KB to avoid scanning huge logs from the top.
                # VERIFY-OK always appears near the end (last runcmd).
                with open(console_log, "rb") as f:
                    f.seek(0, 2)  # end
                    size = f.tell()
                    f.seek(max(0, size - 200_000))
                    tail = f.read().decode("utf-8", errors="replace")
                if marker in tail:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] SSH dead -- marker '{marker}' found in console.log fallback\n")
                    return True
            except Exception:
                pass
    return False


def ssh_wait_cloud_init(host: str, port: int, username: str, ssh_key: Path, log_path: Path, overall_timeout_sec: int,
                        marker: str = "VERIFY-OK: all required components present",
                        target_dir: Path | None = None) -> tuple[int, float]:
    """Poll cloud-init readiness via short SSH commands.

    Decision based on cloud-init's `extended_status` field (not just exit code):
      * extended_status: done            -> SUCCESS only if the VERIFY-OK/SIMULATE-OK marker is present (return 0); otherwise FAIL (return 1)
      * extended_status: error - done    -> SUCCESS only if the marker is present (return 0); otherwise TERMINAL FAILURE (return 1)
      * extended_status: error - running -> cloud-init had a non-fatal error
                                            (e.g. bootcmd) BUT is still working --
                                            KEEP WAITING
      * extended_status: running         -> normal, keep waiting
      * exit code 0/2 / SSH transient    -> keep waiting

    On terminal failure we dump:
      - last 200 lines of /var/log/cloud-init.log
      - /var/log/cloud-init-bootcmd.log (our bootcmd scripts redirect to this)
      - journalctl -p err for cloud-init services
    """
    overall_start = time.time()
    deadline = overall_start + overall_timeout_sec
    while time.time() < deadline:
        try:
            _kill_ssh_child_processes(ssh_key)

            rc, stdout, stderr = run_with_hard_timeout(
                ssh_cmd(
                    host, port, username, ssh_key,
                    "sudo -n cloud-init status --long",
                    server_alive=True,
                    connect_timeout=90,
                ),
                timeout_sec=120,
            )
            stdout = stdout.strip()
            ext_m = re.search(r"^extended_status:\s*(.+)$", stdout, re.MULTILINE)
            extended = ext_m.group(1).strip() if ext_m else "(no extended_status)"
            # Log EVERY probe (no backoff, no state-change-only logging) so
            # the operator can see the orchestrator is actually working.
            # Probe interval is fixed at 5 min below, so this writes one
            # line every 5 min during a wait -- plenty of visibility.
            now = time.time()
            with log_path.open("a", encoding="utf-8") as fh:
                # rc=-99 means our hard timeout fired (ssh stuck). rc=-98 means
                # other Popen exception. Either way the probe is unreliable;
                # log so the operator sees we're not silently stuck.
                probe_tag = " HARD-TIMEOUT" if rc == -99 else (" POPEN-EXC" if rc == -98 else "")
                fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] elapsed={int(now-overall_start)}s rc={rc} extended_status={extended!r}{probe_tag}\n")
            # Determine state. cloud-init's `extended_status` ladder:
            #   running              -> still working
            #   degraded running     -> still working + non-fatal warnings
            #   done                 -> finished cleanly, BUT only SUCCESS if our
            #                             runcmd success marker is present (cloud-init
            #                             reports `done` even when the verify block
            #                             emitted VERIFY-FAIL, since the block didn't
            #                             abort runcmd with a non-zero exit)
            #   degraded done        -> finished, but had non-fatal recoverable_errors
            #                             (SUCCESS only if marker present)
            #   error - done         -> terminal failure -- BUT first check our
            #                           runcmd success marker (cloud-init may
            #                           flag error because of a single noisy
            #                           postinst even though our user-data
            #                           runcmd block ran through to completion)
            if "error - done" in extended:
                # Cloud-init thinks it failed. Check if our final_message
                # ("<Distro> + GNOME setup complete after N seconds.") made
                # it to cloud-init-output.log. If so, our runcmd reached the
                # end -- treat as success-with-warnings, but STILL capture
                # diagnostics so the operator can post-mortem which package
                # postinst tripped cloud-init's error flag (useful for
                # tightening templates later).
                marker_ok = _check_success_marker(host, port, username, ssh_key, log_path, target_dir=target_dir)
                if marker_ok:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] cloud-init flagged 'error - done' BUT success marker present in cloud-init-output.log -- treating as success-with-warnings\n")
                        fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] capturing diagnostics anyway for post-mortem ...\n")
                    _capture_diagnostics(host, port, username, ssh_key, log_path, stdout)
                    return 0, time.time() - overall_start
                _capture_diagnostics(host, port, username, ssh_key, log_path, stdout)
                return 1, time.time() - overall_start
            if extended in ("done", "degraded done"):
                # SUCCESS only if our runcmd success marker is present.
                # cloud-init reports `done` even when the verify block emitted
                # VERIFY-FAIL (the block didn't abort runcmd with a non-zero
                # exit), so a plain `done` is NOT sufficient -- the marker is
                # the real build-success contract (AGENTS.md). A `done` without
                # the marker means the install half-failed; treat as FAILED.
                marker_ok = _check_success_marker(host, port, username, ssh_key, log_path, marker=marker, target_dir=target_dir)
                if marker_ok:
                    return 0, time.time() - overall_start
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] cloud-init status '{extended}' BUT success marker '{marker}' ABSENT in cloud-init-output.log -- treating as FAILED\n")
                _capture_diagnostics(host, port, username, ssh_key, log_path, stdout)
                return 1, time.time() - overall_start
            # Fallback: if cloud-init stays in running/degraded running
            # past the expected runcmd completion time, or if SSH is
            # unreachable (extended='(no extended_status)'), do a secondary
            # check for VERIFY-OK / SIMULATE-OK in cloud-init-output.log.
            # The verify block runs as the last runcmd entry; if the marker
            # is present, our user-data runcmd completed successfully even
            # if cloud-init is stuck on a post-runcmd module (final_message,
            # phone_home, scripts-per-instance, etc.) and never transitions
            # to `done`, or if sshd is overwhelmed by install load.
            # 15 min (900s) is well past any distro's verify-block timing
            # and avoids false-triggers from pre-runcmd log content.
            if (time.time() - overall_start) > 900:
                marker_ok = _check_success_marker(host, port, username, ssh_key, log_path, marker=marker, target_dir=target_dir)
                if marker_ok:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] extended_status={extended!r} BUT success marker present in cloud-init-output.log -- treating as completed\n")
                    return 0, time.time() - overall_start
            # Everything else (running / degraded running / error - running / unknown) -> keep waiting
        except Exception as e:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] probe exception: {type(e).__name__}: {e}\n")
            # Exception probe: try marker check as fallback if SSH is
            # unreachable (probe crashed) but we're past the runcmd window.
            if (time.time() - overall_start) > 900:
                try:
                    marker_ok = _check_success_marker(host, port, username, ssh_key, log_path, marker=marker, target_dir=target_dir)
                    if marker_ok:
                        with log_path.open("a", encoding="utf-8") as fh:
                            fh.write(f"[{datetime.now().strftime('%H:%M:%S')}] probe exception BUT success marker present in cloud-init-output.log -- treating as completed\n")
                        return 0, time.time() - overall_start
                except Exception:
                    pass
        # Check deadline BEFORE sleeping so we don't waste 5 min on a probe
        # that completed just past the deadline.
        if time.time() + 60 >= deadline:
            break
        time.sleep(300)  # 5 min between probes -- no backoff; matches the
                        # operator-visible heartbeat cadence above
    return -1, time.time() - overall_start


def _capture_diagnostics(host: str, port: int, username: str, ssh_key: Path,
                         log_path: Path, last_status: str) -> None:
    """Dump diagnostics on terminal cloud-init failure."""
    diag_cmd = (
        "echo '=== cloud-init status --long ==='; "
        "sudo -n cloud-init status --long 2>&1 || true; "
        "echo '=== /var/log/cloud-init-bootcmd.log (our bootcmd output) ==='; "
        "sudo -n cat /var/log/cloud-init-bootcmd.log 2>&1 || echo '(no bootcmd log file)'; "
        # cloud-init-output.log captures the literal stdout/stderr of every
        # command cloud-init runs (apt-get install, dnf install, etc.). When
        # `cc_package_update_upgrade_install` reports a ProcessExecutionError
        # with empty Stdout/Stderr, the actual error text is in THIS file --
        # the cloud-init internal journal in cloud-init.log only sees the
        # exit code. Grep for typical apt/dnf error markers so we
        # surface the real cause (Unable to locate package, Hash Sum
        # mismatch, conflicting decisions, Cannot install, Failed to fetch).
        "echo '=== /var/log/cloud-init-output.log (apt/dnf ERRORS only) ==='; "
        "sudo -n grep -E '^E:|^W:|^Err:|^N:|Unable to|Hash Sum|broken|conflict|no installation candidate|Could not|Cannot|Failed to|^Error:|^Failed:|^Problem:|^ Problem' /var/log/cloud-init-output.log 2>&1 | tail -100 || true; "
        "echo '=== last 60 lines of /var/log/cloud-init-output.log (raw tail) ==='; "
        "sudo -n tail -60 /var/log/cloud-init-output.log 2>&1 || true; "
        "echo '=== last 80 lines of /var/log/cloud-init.log (ERRORS only) ==='; "
        "sudo -n grep -E 'ERROR|WARNING|FAIL|Traceback' /var/log/cloud-init.log 2>&1 | tail -80 || true; "
        "echo '=== journalctl err for cloud-init ==='; "
        "sudo -n journalctl -u cloud-init -u cloud-init-local -u cloud-final -u cloud-config --no-pager -p err -n 50 2>&1 || true; "
        # /var/log/install-simulate.log is where the simulate-mode runcmd
        # redirects dnf install --assumeno / apt-get install --simulate /
        # simulate dry-run output. The marker SIMULATE-OK / SIMULATE-FAIL
        # lines go to cloud-init-output.log, but the actual resolver errors
        # (missing atoms, masked packages, USE-flag conflicts, dep cycles)
        # only land in install-simulate.log.
        # Without dumping this file, the orchestrator's FAIL report has
        # no actionable detail beyond "resolver failed".
        "echo '=== last 100 lines of /var/log/install-simulate.log (the resolver dry-run output) ==='; "
        "sudo -n tail -100 /var/log/install-simulate.log 2>&1 || echo '(no install-simulate.log -- simulate runcmd may not have fired)'; "
    )
    try:
        rc, stdout, stderr = run_with_hard_timeout(
            ssh_cmd(host, port, username, ssh_key, diag_cmd, server_alive=True),
            timeout_sec=180,
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== TERMINAL FAILURE DIAGNOSTIC ({datetime.now().strftime('%H:%M:%S')}) ===\n")
            fh.write(f"last cloud-init status output:\n{last_status}\n\n")
            fh.write("--- remote diagnostic ---\n")
            fh.write(stdout)
            if stderr:
                fh.write(f"\n--- diag stderr ---\n{stderr}\n")
            fh.write("\n=== end diagnostic ===\n")
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== DIAGNOSTIC CAPTURE FAILED: {type(e).__name__}: {e} ===\n")
