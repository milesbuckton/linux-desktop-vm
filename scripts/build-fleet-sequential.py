#!/usr/bin/env python3
"""Thin shim for the sequential VM build orchestrator.

Real implementation lives in the `linux_vm/fleet/` package. Kept as a stable
entry point so `python scripts/build-fleet-sequential.py` keeps working.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linux_vm.fleet import main  # noqa: E402

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent.parent)
    main()
