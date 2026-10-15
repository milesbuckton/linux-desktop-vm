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

# Password shapes that break YAML if a template interpolates them unquoted:
# `password: {{ PASSWORD }}` renders `--password 1234` as an int and
# `--password yes` as a bool, and cloud-init rejects both. The real-world
# cause was a long-standing bug in the `chpasswd:` block, fixed by
# single-quoting every interpolation -- but the gate only ever rendered
# PASSWORD="x", so nothing would have caught a regression.
HOSTILE_PASSWORDS = (
    "1234",            # renders as int
    "yes",             # renders as bool
    "0000",
    "true",
    "it's-a-quote",    # needs the YAML '' escape
    'say "hi"',        # double quotes inside
    "a:b",             # colon-space would end a block mapping
)


def render_context(distro: str, guest_arch: str = "aarch64",
                   password: str = "x") -> dict[str, Any]:
    """Build the same Jinja context setup_vm.py builds at runtime.

    Optional fields (e.g. simulate_only) are layered in by callers. Pass
    `password=` to exercise hostile password shapes; templates must keep
    every interpolation YAML-single-quoted so the shape survives parsing.
    """
    return {
        "USERNAME": "testuser",
        "HOSTNAME": f"{distro}-vm",
        "VM_NAME": f"{distro} GNOME",
        "DISPLAY_NAME": distro,
        "PASSWORD": password,
        "ROOT_PASSWORD": password,
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
        # setup_vm.py always passes this (empty when the fleet's host-served
        # extension server isn't running). Kept here so lint renders match
        # the runtime context shape -- the name must stay in lockstep with
        # _gs_extensions_common.j2 (a mismatch there silently disables the
        # host-served URL; see tests/test_logic.py wiring test).
        "GNOME_EXT_URL": "",
    }
