"""VM lifecycle management: pre-flight cleanup, shutdown, and verification."""
from __future__ import annotations
import subprocess
import time
from pathlib import Path

from .constants import OUT_ROOT, SHUTDOWN_TIMEOUT_SEC, VERIFY_DEAD_TIMEOUT_SEC
from . import ssh as _ssh
from .ssh import ssh_cmd
from ..provider import list_running_qemu_pids


def _note(stop_log: Path, msg: str) -> None:
    """Log to master AND to the per-VM stop log (best-effort, never raises).

    `stop_log` was accepted but never written to, so a caller tailing
    `~/VMs/<distro>.stop.log` saw an empty file even when the shutdown
    needed force-killing. Phase-3 evidence belongs in both places.

    `log_master` is reached through the MODULE (`_ssh.log_master`), not
    through a by-value `from .ssh import log_master` binding. The by-value
    import made this function's log lines unpatchable: a test that patched
    `fleet.ssh.log_master` saw ZERO calls while the real function wrote
    14 lines per suite run into the operator's live
    `~/VMs/build-fleet.log` -- a log that then claimed VMs were
    force-killed and had timed out, for VMs that never existed. Module
    attribute lookup is what makes `mock.patch.object(ssh, "log_master")`
    intercept it.
    """
    _ssh.log_master(msg)
    try:
        with stop_log.open("a", encoding="utf-8") as handle:
            handle.write(msg + "\n")
    except OSError:
        pass


def kill_pid(pid: int) -> None:
    try:
        subprocess.run(["kill", "-9", str(pid)],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def _qemu_pids(scope: str) -> list[int]:
    """`list_running_qemu_pids` that cannot abort the caller.

    `provider.list_running_qemu_pids` deliberately re-raises when pgrep
    exits with anything other than 0/1 (a bad pattern, or pgrep killed),
    and re-raises FileNotFoundError when pgrep is not on PATH. That is
    right for a caller that wants to KNOW, but wrong for the cleanup
    path: `shutdown_and_verify` runs inside `build_and_provision`'s
    `finally`, so an exception there masks any in-flight exception and
    escapes the function, aborting the whole fleet and losing the
    remaining distros -- the exact class AGENTS.md already documents for
    `pgrep` scope escaping.

    On an unusable pgrep we treat the VM as STILL ALIVE ("unknown =>
    assume alive"), because the alternative -- assuming it is dead --
    would skip the force-kill and leave a VM holding host RAM/CPU and
    the SSH port for the rest of the run. The reason is logged so the
    operator can tell "no VM" from "could not look".
    """
    try:
        return list_running_qemu_pids(scope)
    except (OSError, subprocess.CalledProcessError) as exc:
        _ssh.log_master(f"phase-3: could not list qemu-system processes "
                        f"({type(exc).__name__}: {exc}) -- assuming the VM "
                        f"is still alive")
        return [-1]


def preflight_cleanup() -> None:
    """Kill any leftover VM processes from previous runs."""
    _ssh.log_master("preflight: scanning for orphan VMs ...")
    for pid in _qemu_pids(str(OUT_ROOT)):
        if pid <= 0:
            continue
        _ssh.log_master(f"preflight: force-kill qemu-system PID {pid}")
        kill_pid(pid)
    time.sleep(3)
    n_qemu = len(_qemu_pids(str(OUT_ROOT)))
    _ssh.log_master(f"preflight: post-cleanup qemu-system={n_qemu}")


def shutdown_and_verify(target_dir: Path, host: str, port: int,
                        username: str, ssh_key: Path, stop_log: Path) -> bool:
    """Shutdown the VM AND verify the process is gone. Returns True on success.

    Two-stage, deliberately:

    1. Ask the guest to power itself off over SSH (only when we already have
       a working endpoint -- phase-1 failures have none).
    2. Give it SHUTDOWN_TIMEOUT_SEC to actually go away, POLLING for the
       process to exit rather than sleeping a fixed 5s and force-killing.

    Stage 2's polling is the point. A `sudo poweroff` returns as soon as the
    guest *accepts* the request; systemd then needs a few seconds to unmount
    and close the qcow2. The old code slept 5s and then SIGKILLed -- on the
    success path too -- which left the disk in whatever state a hard kill
    catches. Every fleet build reuses that disk (`--keep-qcow2`), so the next
    distro's build inherited a filesystem with no clean unmount and no fsck.
    A guest that is genuinely stuck (cloud-init mid-install, sshd blocked)
    never powers off, so the wait is bounded: after the grace period we
    force-kill as before, and the disk is then dirty by necessity.
    """
    _note(stop_log, "  phase-3: shutting down VM (qemu) ...")
    if host and port:
        cmd = ssh_cmd(
            host, port, username, ssh_key,
            "sudo systemctl poweroff || sudo poweroff || true",
            connect_timeout=10,
        )
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    # Grace period: does the guest go away on its own? A healthy guest is
    # gone within seconds; one that is mid-install never will be.
    deadline = time.time() + SHUTDOWN_TIMEOUT_SEC
    while time.time() < deadline:
        if not _qemu_pids(str(target_dir)):
            _note(stop_log, "  phase-3: VM powered itself off (clean shutdown)")
            return True
        time.sleep(5)

    # Grace expired. Aggressively kill any qemu-system process whose cmdline
    # references this VM's target dir. We don't rely solely on sudo poweroff
    # because if cloud-init is still mid-install, sshd may be blocked / sudo
    # may hang. Force-kill is the only reliable way to free the resources --
    # but say so in the log, because the qcow2 may now be dirty and the next
    # `--keep-qcow2` build reuses it.
    _note(stop_log, f"  phase-3: no clean shutdown within {SHUTDOWN_TIMEOUT_SEC}s "
                    f"-- force-killing (disk may be left unclean)")
    for _ in range(3):
        pids = _qemu_pids(str(target_dir))
        if not pids:
            break
        for pid in pids:
            # `kill -9 -1` signals EVERY process the user owns. The
            # sentinel from _qemu_pids() (pgrep unusable) must never
            # reach kill_pid().
            if pid <= 0:
                continue
            _note(stop_log, f"  phase-3: force-kill qemu-system PID {pid}")
            kill_pid(pid)
        time.sleep(3)

    # Verify: poll until the VM process is gone, or timeout
    deadline = time.time() + VERIFY_DEAD_TIMEOUT_SEC
    while time.time() < deadline:
        still_alive = len(_qemu_pids(str(target_dir))) > 0
        if not still_alive:
            _note(stop_log, "  phase-3: VM verified DEAD")
            return True
        time.sleep(5)

    _note(stop_log, f"  phase-3: VERIFY TIMEOUT -- VM still appears alive "
                    f"after {VERIFY_DEAD_TIMEOUT_SEC}s")
    return False
