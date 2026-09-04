"""QEMU VM launcher with HVF (macOS) acceleration.

This module provides standalone QEMU tool discovery and VM process utilities
used by monitor.py, fleet/lifecycle.py, and fleet/orchestrator.py.
The actual VM launching logic lives in qemu.py.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Running-VM discovery (shared by monitor.py + the fleet builder)
# --------------------------------------------------------------------------
def list_running_qemu_pids(scope: str) -> list[int]:
    """Return PIDs of qemu-system-* processes whose cmdline contains `scope`.

    Scoping by cmdline (e.g. OUT_ROOT, a target dir) keeps cleanup from
    killing unrelated QEMU VMs the user may have running elsewhere.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f", f"qemu-system.*{scope}"],
            capture_output=True, text=True, timeout=30,
        )
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def find_running_ssh_port(target_dir: Path) -> int | None:
    """Find the SSH host-forward port for the running QEMU VM under target_dir.

    Parses the qemu-system cmdline (hostfwd=tcp:127.0.0.1:PORT-:22; the
    loopback prefix is optional so launchers generated before the M5
    loopback bind are still understood). Returns None if the VM isn't
    running or the port can't be parsed.
    """
    for pid in list_running_qemu_pids(str(target_dir))[:1]:
        try:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_text(errors="replace").replace("\x00", " ")
            else:
                # macOS: use ps
                ps_r = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True, text=True, timeout=10,
                )
                cmdline = ps_r.stdout.strip()
            m = re.search(r"hostfwd=tcp:(?:127\.0\.0\.1:)?(\d+)-:22", cmdline)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None
