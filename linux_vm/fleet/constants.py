"""Fleet-wide constants and configuration for the VM build orchestrator."""
from __future__ import annotations
from pathlib import Path

from ..config import (
    DISTRO_ORDER as DISTROS,
    DISTRO_DEFAULTS,
    DEFAULT_CLOUD_INIT_WAIT_TIMEOUT_SEC,
)

REPO = Path(__file__).resolve().parent.parent.parent
USERNAMES = {d: DISTRO_DEFAULTS[d]["username"] for d in DISTROS}
OUT_ROOT = Path.home() / "VMs"
CACHE_ROOT = OUT_ROOT / "cache"  # shared cloud-image cache + host-served ext zips
MASTER_LOG = OUT_ROOT / "build-fleet.log"
SSH = "ssh"

DISTRO_MIRROR = {
    "gentoo": "distfiles.gentoo.org",
    "ubuntu-lts": "archive.ubuntu.com",
}

BUILD_TIMEOUT_SEC = 1800       # 30 min for phase 1
SSH_REACHABLE_TIMEOUT_SEC = 1800  # 30 min for TCP socket to open
CLOUD_INIT_WAIT_TIMEOUT_SEC = DEFAULT_CLOUD_INIT_WAIT_TIMEOUT_SEC  # 60 min for cloud-init to finish (most distros: 10-30 min).
                                    # 60-min cap lets us fail-fast on a stuck VM instead of
                                    # the old 3-h wait that silently masked the
                                    # `degraded done` bug for hours.
                                    # Sourced from config.DEFAULT_CLOUD_INIT_WAIT_TIMEOUT_SEC
                                    # so the number lives in exactly one place.
SHUTDOWN_TIMEOUT_SEC = 60      # Grace period we give a guest that ACCEPTED
                                    # `sudo poweroff` to actually exit before
                                    # lifecycle.shutdown_and_verify force-kills
                                    # it. A healthy guest is gone in <20s; a
                                    # guest stuck mid-cloud-init never will be,
                                    # so this must stay short -- at 300s every
                                    # build paid 5 min to be SIGKILLed anyway.
                                    # (It was also dead: referenced nowhere.)
VERIFY_DEAD_TIMEOUT_SEC = 180  # 3 min to verify VM process is gone

# NOTE: ~/VMs is deliberately NOT created at import time. Any module that
# imports constants used to get that filesystem side effect (a stray
# directory from running --help, or from a test importing linux_vm.fleet).
# The mkdir now happens where a VM is actually about to be written -- see
# ensure_out_root() below, called from fleet.main and fleet.orchestrator.

VERIFY_OK_MARKER = "VERIFY-OK: all required components present"


def ensure_out_root() -> Path:
    """Create ~/VMs and return it.

    Called instead of doing it at import time: modules that only need the
    constants (tests, --help) must not create a directory as a side effect.
    """
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    return OUT_ROOT
