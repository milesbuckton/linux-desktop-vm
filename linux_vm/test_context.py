"""Shared Jinja2 render context for lint/audit scripts.

DRY: the lint-templates, audit-packages, and any future static-analysis
script that renders a template needs the SAME context shape that
setup_vm.py builds at runtime, otherwise a script using a different
context can mask a real template error (or vice versa). Drift between
these two callers has cost time in the past (AGENTS.md), so the
context is now single-source.
"""
from __future__ import annotations

from typing import Any


def render_context(distro: str, guest_arch: str = "aarch64") -> dict[str, Any]:
    """Build the same Jinja context setup_vm.py builds at runtime.

    Optional fields (e.g. simulate_only) are layered in by callers.
    """
    return {
        "USERNAME": "testuser",
        "HOSTNAME": f"{distro}-vm",
        "VM_NAME": f"{distro} GNOME",
        "DISPLAY_NAME": distro,
        "PASSWORD": "x",
        "ROOT_PASSWORD": "root",
        "PASSWORD_HASH": "$6$dummy$dummy",
        "ROOT_PASSWORD_HASH": "$6$dummy$dummy",
        "SSH_PUBLIC_KEY": "ssh-ed25519 AAAA test@lint",
        "TIMEZONE": "Africa/Johannesburg",
        "INSTANCE_ID": f"{distro}-vm-lintsim",
        "DISTRO_ID": distro,
        "GUEST_ARCH": guest_arch,
        "marker_name": distro,
        "display_name": distro,
        "simulate_only": False,
    }
