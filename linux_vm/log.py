"""ANSI colours and console logging for the orchestrator, monitor, and scripts."""
from __future__ import annotations


# --------------------------------------------------------------------------
# ANSI colours
# --------------------------------------------------------------------------
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    OK = "\033[32m"
    WARN = "\033[33m"
    ERR = "\033[31m"
    INFO = "\033[36m"


def log(msg: str, level: str = "info") -> None:
    prefix = {
        "info": f"{C.INFO}[ * ]{C.RESET}",
        "ok": f"{C.OK}[ ok ]{C.RESET}",
        "warn": f"{C.WARN}[ ! ]{C.RESET}",
        "err": f"{C.ERR}[ X ]{C.RESET}",
        "step": f"{C.BOLD}{C.INFO}==>{C.RESET}",
    }.get(level, "[ . ]")
    print(f"{prefix} {msg}", flush=True)
