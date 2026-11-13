"""Shared Jinja2 render context.

DRY: the lint-templates, audit-packages, and any future static-analysis
script that renders a template needs the SAME context that setup_vm.py
builds at runtime, otherwise a script using a different context can mask a
real template error (or vice versa). Drift between these two callers has
cost time in the past (AGENTS.md), so the context is now single-source:
linux_vm/orchestrate.py:_build_one_vm calls render_context() rather than
hand-rolling a dict of its own, which is how this file drifted the first
time (the runtime literal was missing display_name and carried five keys no
template has ever read).

`tests/test_logic.py::TestRenderContextIsSingleSource` locks that wiring, so
a key added here without a runtime caller -- or vice versa -- fails Gate 0d
instead of being discovered by the next overnight build.
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

# Human-readable distro name for `_base.j2`'s `final_message`. The templates
# each carry `{% set display_name = display_name | default('Ubuntu') %}`, so
# an UNSET display_name falls back per-distro -- but a context that supplies it
# for one distro and not another would render two different final_messages
# depending on which caller built the context, which is precisely the drift
# this module exists to prevent. Keyed here so lint and runtime agree.
DISPLAY_NAMES = {
    "ubuntu-lts": "Ubuntu",
    "gentoo": "Gentoo",
}


def render_context(distro: str, guest_arch: str = "aarch64",
                   password: str = "x") -> dict[str, Any]:
    """Build the same Jinja context setup_vm.py builds at runtime.

    Every key here must be a key the runtime actually supplies. A key that
    exists ONLY for the gates is a trap: `PASSWORD_HASH: "$6$dummy$dummy"`
    rendered clean through Gate 0 while `setup_vm.py` raised
    StrictUndefined, and the mirror image -- runtime supplying a value the
    gates never render -- means the gate validated a template branch the
    build does not take.

    Optional fields (e.g. simulate_only) are layered in by callers. Pass
    `password=` to exercise hostile password shapes; templates must keep
    every interpolation YAML-single-quoted so the shape survives parsing.
    """
    return {
        "USERNAME": "testuser",
        "HOSTNAME": f"{distro}-vm",
        "PASSWORD": password,
        "ROOT_PASSWORD": password,
        "SSH_PUBLIC_KEY": "ssh-ed25519 AAAA test@lint",
        "TIMEZONE": "Africa/Johannesburg",
        "INSTANCE_ID": f"{distro}-vm-lintsim",
        "display_name": DISPLAY_NAMES.get(distro, distro),
        "simulate_only": False,
        "GUEST_ARCH": guest_arch,
        "marker_name": distro,
        # setup_vm.py always passes this (empty when the fleet's host-served
        # extension server isn't running). Kept here so lint renders match
        # the runtime context shape -- the name must stay in lockstep with
        # _gs_extensions_common.j2 (a mismatch there silently disables the
        # host-served URL; see tests/test_logic.py wiring test).
        "GNOME_EXT_URL": "",
    }
