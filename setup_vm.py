#!/usr/bin/env python3
"""setup_vm.py -- Thin shim that delegates to the linux_vm package.

All logic lives in linux_vm/.
"""
from __future__ import annotations
from linux_vm.__main__ import entry

if __name__ == "__main__":
    entry()
