"""Process execution helpers with hard timeouts for the fleet orchestrator."""
from __future__ import annotations
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


def run_to_file(cmd: list, log_path, timeout: int, append: bool = False) -> tuple[int, float]:
    from pathlib import Path
    log_path = Path(log_path)
    mode = "a" if append else "w"
    start = time.time()
    try:
        with log_path.open(mode, encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n# cmd: {' '.join(str(c) for c in cmd)}\n\n")
            fh.flush()
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
            rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    except Exception as e:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# orchestrator error: {type(e).__name__}: {e}\n")
        rc = -2
    return rc, time.time() - start
