"""Process execution helpers with hard timeouts for the fleet orchestrator."""
from __future__ import annotations
import os
import signal
import subprocess
import time

def run_with_hard_timeout(cmd: list, timeout_sec: float) -> tuple[int, str, str]:
    """subprocess.run replacement that ALWAYS returns within timeout_sec + grace.

    Mitigation: spawn via Popen, poll with short waits, and on timeout use
    kill() to tear down the process. Returns (returncode, stdout, stderr).
    returncode=-99 means we timed out.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return proc.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        # Drain pipes with our own short timeout so we don't hang here either.
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return -99, stdout or "", stderr or ""
    except Exception as e:
        # Defensive: kill the child so it doesn't outlive us
        try:
            proc.kill()
        except Exception:
            pass
        return -98, "", f"{type(e).__name__}: {e}"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """SIGKILL `proc` plus everything it spawned, then best-effort reap.

    `subprocess.run(..., timeout=)` (and a bare `proc.kill()`) signal ONLY
    the direct child. `setup_vm.py --start` launches QEMU with
    start_new_session=True (linux_vm/qemu.py:launch), so on a phase-1 timeout
    the VM outlives its python parent, keeps holding host RAM/CPU and an open
    qcow2 for the rest of the fleet run, and starves the next build --
    preflight_cleanup() has already run by then. Killing the whole process
    group is the only reliable way to take the VM down with its parent, which
    is why run_to_file starts the child in its own session (making it a group
    leader, and never us).

    Never raises: teardown must not mask the caller's verdict.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    try:
        proc.kill()
    except OSError:
        pass
    # Paranoia: never killpg our own group -- that would take the fleet with it.
    if pgid is None or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def run_to_file(cmd: list, log_path, timeout: int, append: bool = False) -> tuple[int, float]:
    """Run `cmd` with output tee'd to `log_path` under a hard `timeout`.

    Returns (rc, elapsed_sec) with rc == -1 for a timeout and -2 if the
    wrapper itself failed. A timeout takes down the child's entire process
    group (see _kill_process_tree).
    """
    from pathlib import Path
    log_path = Path(log_path)
    mode = "a" if append else "w"
    start = time.time()
    try:
        with log_path.open(mode, encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n# cmd: {' '.join(str(c) for c in cmd)}\n\n")
            fh.flush()
            # start_new_session=True: makes the child a process-group leader so
            # _kill_process_tree can reach QEMU. Side effect: a Ctrl-C on the
            # fleet's terminal no longer reaches the child directly, so the
            # KeyboardInterrupt branch below tears it down explicitly.
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                proc.wait(timeout=timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                rc = -1
            except BaseException:
                _kill_process_tree(proc)
                raise
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# orchestrator error: {type(e).__name__}: {e}\n")
        rc = -2
    return rc, time.time() - start
