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
SHUTDOWN_TIMEOUT_SEC = 300     # 5 min for soft shutdown
VERIFY_DEAD_TIMEOUT_SEC = 180  # 3 min to verify VM process is gone

OUT_ROOT.mkdir(parents=True, exist_ok=True)

VERIFY_OK_MARKER = "VERIFY-OK: all required components present"
