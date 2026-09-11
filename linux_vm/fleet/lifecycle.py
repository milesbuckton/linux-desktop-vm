"""VM lifecycle management: pre-flight cleanup, shutdown, and verification."""
from __future__ import annotations
import subprocess
import time
from pathlib import Path

from .constants import OUT_ROOT, VERIFY_DEAD_TIMEOUT_SEC
from .ssh import log_master, ssh_cmd
from ..provider import list_running_qemu_pids


def kill_pid(pid: int) -> None:
    try:
        subprocess.run(["kill", "-9", str(pid)],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def preflight_cleanup() -> None:
    """Kill any leftover VM processes from previous runs."""
    log_master("preflight: scanning for orphan VMs ...")
    for pid in list_running_qemu_pids(str(OUT_ROOT)):
        log_master(f"preflight: force-kill qemu-system PID {pid}")
        kill_pid(pid)
    time.sleep(3)
    n_qemu = len(list_running_qemu_pids(str(OUT_ROOT)))
    log_master(f"preflight: post-cleanup qemu-system={n_qemu}")


def shutdown_and_verify(target_dir: Path, host: str, port: int,
                        username: str, ssh_key: Path, stop_log: Path) -> bool:
    """Shutdown the VM AND verify the process is gone. Returns True on success."""
    log_master(f"  phase-3: shutting down VM (qemu) ...")
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
    # Aggressively kill any qemu-system process whose cmdline references
    # this VM's target dir. We don't rely solely on sudo poweroff because
    # if cloud-init is still mid-install, sshd may be blocked / sudo may
    # hang. Force-kill is the only reliable way to free the resources.
    time.sleep(5)
    for _ in range(3):
        pids = list_running_qemu_pids(str(target_dir))
        if not pids:
            break
        for pid in pids:
            log_master(f"  phase-3: force-kill qemu-system PID {pid}")
            kill_pid(pid)
        time.sleep(3)

    # Verify: poll until the VM process is gone, or timeout
    deadline = time.time() + VERIFY_DEAD_TIMEOUT_SEC
    while time.time() < deadline:
        still_alive = len(list_running_qemu_pids(str(target_dir))) > 0
        if not still_alive:
            log_master("  phase-3: VM verified DEAD")
            return True
        time.sleep(5)

    log_master(f"  phase-3: VERIFY TIMEOUT -- VM still appears alive after {VERIFY_DEAD_TIMEOUT_SEC}s")
    return False
