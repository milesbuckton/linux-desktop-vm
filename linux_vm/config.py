"""VM configuration, distro defaults, and shared helpers."""
from __future__ import annotations

import dataclasses
import textwrap
import uuid
from pathlib import Path
from typing import Optional

from .log import C
from .host import recommended_vcpus


@dataclasses.dataclass
class VMConfig:
    vm_name: str = "Ubuntu LTS GNOME"
    hostname: str = "ubuntu-lts-vm"
    username: str = "ubuntu"
    password: str = ""
    root_password: str = ""
    vcpus: int = dataclasses.field(default_factory=recommended_vcpus)
    memory_mb: int = 16384
    disk_gb: int = 80
    timezone: str = "Africa/Johannesburg"
    target_dir: Path = dataclasses.field(default_factory=lambda: Path())
    instance_id: str = dataclasses.field(
        default_factory=lambda: f"vm-{uuid.uuid4().hex[:8]}"
    )
    ssh_port: Optional[int] = None

    @property
    def seed_filename(self) -> str:
        return "seed.iso"

    @property
    def seed_path(self) -> Path:
        return self.target_dir / self.seed_filename


# Generic cloud-init wait ceiling (seconds). Used by the fleet builder as the
# fallback when a distro doesn't override it in DISTRO_DEFAULTS. Gentoo
# overrides this (emerge --sync + a full GNOME binpkg install routinely
# exceeds the 60-min cap). The fleet builder imports this constant so the
# number lives in exactly one place (see fleet/constants.py).
DEFAULT_CLOUD_INIT_WAIT_TIMEOUT_SEC = 3600

_BASE_DEFAULTS: dict[str, object] = {
    "vm_name": None,
    "hostname": "",
    "username": "",
    # Per-distro cloud-init wait ceiling (seconds). Gentoo's `emerge --sync`
    # + full GNOME binary install routinely exceeds the generic 60-min cap;
    # give it headroom without slowing the apt/dnf distros.
    "cloud_init_wait_timeout_sec": DEFAULT_CLOUD_INIT_WAIT_TIMEOUT_SEC,
}

DISTRO_ORDER = [
    "ubuntu-lts",
    "gentoo",
]

DISTRO_DEFAULTS: dict[str, dict[str, object]] = {
    "gentoo":     {"hostname": "gentoo-vm",     "username": "gentoo", "cloud_init_wait_timeout_sec": 21600},
    "ubuntu-lts": {"hostname": "ubuntu-lts-vm", "username": "ubuntu"},
}

DISTROS = list(DISTRO_ORDER)

for distro, overrides in DISTRO_DEFAULTS.items():
    merged = dict(_BASE_DEFAULTS)
    merged.update(overrides)
    DISTRO_DEFAULTS[distro] = merged

DISTRO_TEMPLATE = {
    "gentoo": "gentoo.j2",
    "ubuntu-lts": "ubuntu.j2",
}

# This map is the single source of truth for which template renders a
# distro. download.DISTROS[].user_data_template derives from it, and the
# lint/audit scripts read it directly. Keep its keys in lockstep with
# DISTRO_ORDER -- a distro added to one but not the other is a bug.
if set(DISTRO_TEMPLATE) != set(DISTRO_ORDER):
    raise RuntimeError(
        "distro/template registry mismatch between DISTRO_ORDER and "
        f"DISTRO_TEMPLATE: {sorted(set(DISTRO_TEMPLATE) ^ set(DISTRO_ORDER))}"
    )

# Host port range used for the QEMU SSH host-forward. The generated launcher
# picks a free port from this range at launch time; the fleet builder and the
# monitor probe the same range. (inclusive start, exclusive end)
SSH_PORT_RANGE = (2222, 2322)


def filename_from_url(url: str) -> str:
    """Extract the last path segment of a URL as a local filename."""
    from urllib.parse import urlparse
    return Path(urlparse(url).path).name or "cloud-image.qcow2"


def banner(distro_name: str) -> None:
    bar = "=" * 64
    title = f"{distro_name} + GNOME (Wayland) -- Auto-Installer"
    print(
        textwrap.dedent(
            f"""
            {C.BOLD}{bar}
              {title}
            {bar}{C.RESET}
            """
        ).strip()
    )
