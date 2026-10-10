"""ANSI colours and console logging for the orchestrator, monitor, and scripts."""
from __future__ import annotations

import os
import sys


# --------------------------------------------------------------------------
# ANSI colours
# --------------------------------------------------------------------------
_CODES = {
    "RESET": "\033[0m",
    "BOLD": "\033[1m",
    "DIM": "\033[2m",
    "OK": "\033[32m",
    "WARN": "\033[33m",
    "ERR": "\033[31m",
    "INFO": "\033[36m",
}


def color_enabled() -> bool:
    """True when stdout is a real terminal (or FORCE_COLOR is set).

    Escape codes in a captured stream are pure noise: the fleet orchestrator
    pipes this stdout into <distro>.build.log, where `\033[32m[ ok ]\033[0m`
    breaks grep for `ok]` and makes the log unreadable in a terminal. Colour
    was emitted unconditionally, so every log line of every build carried it.
    """
    return bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty()


class _Palette:
    """ANSI palette that collapses to empty strings off a terminal.

    Attribute access, not plain constants, so the TTY check happens per use
    (a process can be attached to a pty after import, and tests capture
    stdout). `C.OK` outside a TTY is "" -- nothing else in the codebase cares
    what the escape sequences are, only that they bracket the text.
    """

    def __getattr__(self, name: str) -> str:
        code = _CODES.get(name)
        if code is None:
            raise AttributeError(f"no colour named {name!r}")
        return code if color_enabled() else ""


# Single shared instance: `C.OK`, not `C().OK`.
C = _Palette()


def log(msg: str, level: str = "info") -> None:
    prefix = {
        "info": f"{C.INFO}[ * ]{C.RESET}",
        "ok": f"{C.OK}[ ok ]{C.RESET}",
        "warn": f"{C.WARN}[ ! ]{C.RESET}",
        "err": f"{C.ERR}[ X ]{C.RESET}",
        "step": f"{C.BOLD}{C.INFO}==>{C.RESET}",
    }.get(level, "[ . ]")
    print(f"{prefix} {msg}", flush=True)
