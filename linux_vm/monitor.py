"""Monitor subcommand: tail console.log + report cloud-init phase progress."""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

from .config import SSH_PORT_RANGE
from .provider import find_running_ssh_port


# Cloud-init phases in execution order. Each entry is (regex, label,
# percent_complete_when_phase_starts).
_CLOUD_INIT_PHASES = [
    (re.compile(r"cloud-init\[\d+\]: Cloud-init v\. .* running 'init-local'"),
     "1/5 init-local: kernel done, networking + DHCP", 5),
    (re.compile(r"cloud-init\[\d+\]: Cloud-init v\. .* running 'init'(?!-)"),
     "2/5 init: datasource detected, user creation", 10),
    (re.compile(r"cloud-init\[\d+\]: Cloud-init v\. .* running 'modules:config'"),
     "3/5 modules:config: ssh keys, chpasswd (light)", 20),
    (re.compile(r"cloud-init\[\d+\]: Cloud-init v\. .* running 'modules:final'"),
     "4/5 modules:final: heavy install + runcmd starts", 25),
    (re.compile(r"setup complete after [\d.]+ seconds"),
     "5/5 setup complete -- rebooting into GDM", 100),
]


def _draw_progress_bar(pct: int, label: str, width: int = 40) -> str:
    filled = int(width * pct / 100)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {pct:3d}%  {label}"


def _try_ssh_with_key(target_dir: Path, username: str | None = None):
    """Open an SSH client to the VM using the per-VM ed25519 key.

    Returns a connected paramiko SSHClient, "auth_pending" sentinel,
    or None on any failure.
    """
    ssh_key = target_dir / "ssh_key"
    if not ssh_key.exists():
        return None
    try:
        import paramiko  # type: ignore[import-not-found]
    except ImportError:
        return None
    import logging
    logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)

    info_path = target_dir / "install-info.txt"
    parsed_user = username
    # The launcher picks a free port from 2222-2322 at start time, so the
    # only reliable source is the running qemu-system cmdline. Fall back to
    # probing the whole range when the VM isn't running yet.
    discovered = find_running_ssh_port(target_dir)
    ports_to_try = [discovered] if discovered else list(range(*SSH_PORT_RANGE))
    if info_path.exists():
        info = info_path.read_text(encoding="utf-8", errors="replace")
        m_user = re.search(r"^Username:\s+(\S+)", info, re.MULTILINE)
        if m_user:
            parsed_user = m_user.group(1)
    pkey = paramiko.Ed25519Key.from_private_key_file(str(ssh_key))
    for port in ports_to_try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect("127.0.0.1", port=port, username=parsed_user,
                      pkey=pkey, timeout=4, banner_timeout=4,
                      allow_agent=False, look_for_keys=False)
            return c
        except paramiko.AuthenticationException:
            c.close()
            return "auth_pending"  # type: ignore[return-value]
        except Exception:
            c.close()
            continue
    return None


def monitor_main(argv: list[str]) -> int:
    """Tail console.log under <target_dir> and report cloud-init progress.

    Usage:
      setup_vm.py monitor <target_dir>
      setup_vm.py monitor <target_dir> --once
      setup_vm.py monitor <target_dir> --tail
    """
    p = argparse.ArgumentParser(
        prog="setup_vm.py monitor",
        description=(
            "Watch a VM's cloud-init progress by tailing its captured "
            "serial console. Press Ctrl-C to exit."
        ),
    )
    p.add_argument("target_dir", type=Path,
                   help="The VM's target directory (contains console.log)")
    p.add_argument("--once", action="store_true",
                   help="Print one snapshot and exit (don't tail)")
    p.add_argument("--tail", action="store_true",
                   help=("Live-tail /var/log/cloud-init-output.log via SSH."))
    args = p.parse_args(argv)
    console_path = args.target_dir / "console.log"
    if not console_path.exists():
        print(f"[ERR] {console_path} does not exist. Has the VM been started?",
              file=sys.stderr)
        return 1

    if args.tail:
        return _monitor_tail_loop(args.target_dir, console_path)

    seen_phase_idx = -1
    last_status_line_len = 0

    def _eval_state() -> tuple[int, str, int]:
        try:
            text = console_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return -1, "(no readable console.log yet)", 0
        latest_idx = -1
        for i, (pattern, _label, _pct) in enumerate(_CLOUD_INIT_PHASES):
            if pattern.search(text):
                latest_idx = i
        if latest_idx < 0:
            if "Linux version" in text or "Booting `" in text:
                return -1, "0/5 kernel booting (no cloud-init phase yet)", 2
            return -1, "(VM not yet producing output)", 0
        _, label, pct = _CLOUD_INIT_PHASES[latest_idx]
        return latest_idx, label, pct

    def _ssh_module_hint() -> Optional[str]:
        result = _try_ssh_with_key(args.target_dir)
        if result is None:
            return None
        if result == "auth_pending":
            return "sshd up; waiting for cc_users to install pub key"
        c = result
        try:
            si, so, se = c.exec_command("cloud-init status --long 2>&1 | head -40", timeout=8)
            out = so.read().decode(errors="replace")
            m = re.search(r"detail:\s*(.+)", out)
            if m:
                detail = m.group(1).strip()
                return detail[:80]
        except Exception:
            return None
        finally:
            c.close()
        return None

    def _print_status(label: str, pct: int, phase_changed: bool, hint: Optional[str]) -> None:
        nonlocal last_status_line_len
        display = label
        if hint:
            display = f"{label} :: {hint}"
        bar = _draw_progress_bar(pct, display)
        if phase_changed:
            print()
        pad = " " * max(0, last_status_line_len - len(bar))
        sys.stdout.write("\r" + bar + pad)
        sys.stdout.flush()
        last_status_line_len = len(bar)

    print(f"Monitoring {console_path} ...")
    print("(phase 4 'modules:final' is where 75-90% of the wall time goes)")
    if (args.target_dir / "ssh_key").exists():
        print("(SSH key present -- `--tail` would show live install output)")
    print()
    try:
        ssh_hint_cooldown = 0
        last_hint: Optional[str] = None
        while True:
            idx, label, pct = _eval_state()
            phase_changed = idx != seen_phase_idx
            if ssh_hint_cooldown <= 0:
                last_hint = _ssh_module_hint()
                ssh_hint_cooldown = 6
            else:
                ssh_hint_cooldown -= 1
            _print_status(label, pct, phase_changed, last_hint)
            if phase_changed and idx >= 0:
                seen_phase_idx = idx
            if idx == len(_CLOUD_INIT_PHASES) - 1:
                print()
                print()
                print("[ok] cloud-init reported setup complete.")
                print("     The VM will reboot automatically into GDM.")
                return 0
            if args.once:
                print()
                return 0
            time.sleep(5)
    except KeyboardInterrupt:
        print()
        print("[interrupted] (VM continues to run; cloud-init unaffected)")
        return 130


def _monitor_tail_loop(target_dir: Path, console_path: Path) -> int:
    """Live-tail /var/log/cloud-init-output.log via SSH."""
    print("[--tail] waiting for SSH on the VM (ssh_key required) ...")
    print("(probes every 20 sec; falls back to console.log after 20 min)")
    print()
    deadline = time.time() + 1200
    c = None
    last_state = ""
    while time.time() < deadline:
        result = _try_ssh_with_key(target_dir)
        if result is not None and result != "auth_pending":
            c = result
            break
        if result == "auth_pending":
            state_suffix = "(sshd up; waiting for cc_users to install pub key)"
            wait_secs = 30
        else:
            state_suffix = "(waiting for SSH)"
            wait_secs = 20
        try:
            text = console_path.read_text(encoding="utf-8", errors="replace")
            latest_idx = -1
            for i, (pattern, _label, _pct) in enumerate(_CLOUD_INIT_PHASES):
                if pattern.search(text):
                    latest_idx = i
            if latest_idx >= 0:
                _, label, pct = _CLOUD_INIT_PHASES[latest_idx]
                line = _draw_progress_bar(pct, f"{label} {state_suffix}")
            else:
                line = _draw_progress_bar(0, f"booting {state_suffix}")
            pad = " " * max(0, len(last_state) - len(line) + 10)
            sys.stdout.write("\r" + line + pad)
            sys.stdout.flush()
            last_state = line
        except OSError:
            pass
        time.sleep(wait_secs)
    if c is None:
        print()
        print("[ERR] SSH never came up; falling back to phase-bar mode.",
              file=sys.stderr)
        return 1
    print()
    print("[--tail] SSH connected; streaming /var/log/cloud-init-output.log ...")
    print("=" * 70)
    try:
        si, so, se = c.exec_command(
            "sudo tail -n 50 -F /var/log/cloud-init-output.log",
            timeout=None,
        )
        chan = so.channel
        import select
        while True:
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            r, _, _ = select.select([chan], [], [], 1.0)
            if not r:
                continue
            if chan.recv_ready():
                data = chan.recv(4096)
                if not data:
                    break
                try:
                    sys.stdout.write(data.decode("utf-8", errors="replace"))
                except Exception:
                    sys.stdout.buffer.write(data)
                sys.stdout.flush()
            if chan.recv_stderr_ready():
                err = chan.recv_stderr(4096)
                if err:
                    sys.stderr.write(err.decode("utf-8", errors="replace"))
                    sys.stderr.flush()
    except KeyboardInterrupt:
        print()
        print("[interrupted] (VM continues to run; cloud-init unaffected)")
        return 130
    except Exception as e:
        print()
        print(f"[ERR] tail stream lost ({type(e).__name__}: {e})")
        return 1
    finally:
        try:
            c.close()
        except Exception:
            pass
    return 0
