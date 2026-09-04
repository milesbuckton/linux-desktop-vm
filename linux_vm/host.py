"""Host platform detection, tool discovery, and shared utilities."""
from __future__ import annotations

import dataclasses
import os
import platform
import shutil
import ssl as _ssl
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .log import log

try:
    import certifi as _certifi  # type: ignore
    _SSL_CONTEXT: Optional[_ssl.SSLContext] = _ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CONTEXT = None


# --------------------------------------------------------------------------
# Platform abstraction
# --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class HostPlatform:
    system: str
    arch: str
    is_macos: bool


_HOST_ARCH_TO_GUEST = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "arm64": "aarch64", "aarch64": "aarch64",
}


def is_supported_host_arch(machine: str) -> bool:
    return machine.lower() in _HOST_ARCH_TO_GUEST


def guest_arch_for_host(machine: str) -> str:
    """Map host `platform.machine()` to the guest/QEMU arch token.

    x86_64/amd64/x64 hosts run x86_64 guests; arm64/aarch64 hosts run
    aarch64 guests. Anything else raises (the gate in orchestrate.py
    already refuses to run, so this is a defensive backstop).
    """
    try:
        return _HOST_ARCH_TO_GUEST[machine.lower()]
    except KeyError:
        raise ValueError(
            f"Unsupported host architecture: {machine!r} "
            "(supported: x86_64, amd64, x64, arm64, aarch64)"
        ) from None


def detect_platform() -> HostPlatform:
    system = platform.system()
    machine = platform.machine().lower()
    is_macos = system == "Darwin"
    return HostPlatform(
        system=system,
        arch=machine,
        is_macos=is_macos,
    )


def detect_timezone() -> str:
    """Best-effort IANA timezone detection on macOS."""
    system = platform.system()
    if system == "Darwin":
        try:
            link = os.readlink("/etc/localtime")
            if "zoneinfo/" in link:
                return link.split("zoneinfo/")[-1]
        except OSError:
            pass
    return "Africa/Johannesburg"


def physical_cpu_count() -> Optional[int]:
    """Best-effort physical-core count (macOS: sysctl hw.physicalcpu).

    Returns None if it can't be determined. Logical cores are deliberately
    avoided: hyperthreaded Intel Macs report 2x in hw.ncpu, and counting
    them would oversubscribe the host.
    """
    for key in ("hw.physicalcpu", "hw.ncpu"):
        try:
            out = subprocess.run(
                ["sysctl", "-n", key],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                n = int(out.stdout.strip())
                if n > 0:
                    return n
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
    cpus = os.cpu_count()
    return cpus if cpus and cpus > 0 else None


def recommended_vcpus(cores: Optional[int] = None) -> int:
    """Default vCPUs for a new VM: half of the host's physical cores,
    clamped to [2, 8].

    Halving leaves the other half of the cores free for the host and other
    apps. The floor
    keeps a 2-vCPU minimum (a 1-vCPU desktop is unusable) and the cap
    reflects that past ~8 cores a GNOME desktop + package install is
    I/O-bound, so extra vCPUs add scheduling overhead without benefit.
    """
    if cores is None:
        cores = physical_cpu_count()
    if cores is None:
        return 4
    return max(2, min(cores // 2, 8))


def host_memory_mb() -> Optional[int]:
    """Best-effort physical RAM in MB (macOS: sysctl hw.memsize).

    Returns None if it can't be determined.
    """
    system = platform.system()
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                n = int(out.stdout.strip())
                if n > 0:
                    return n // (1024 * 1024)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    return None


def recommended_memory_mb(host_mem: Optional[int] = None) -> int:
    """Default RAM in MB for a new VM: half of the host's RAM,
    clamped to [8192, 32768].

    Halving follows the same logic as vCPUs — leave the other half free for
    the host. The floor (8 GB) is the minimum for a usable GNOME desktop;
    the cap (32 GB) reflects that past that point extra RAM barely matters
    for a desktop VM.
    """
    if host_mem is None:
        host_mem = host_memory_mb()
    if host_mem is None:
        return 16384
    if host_mem < 8192:
        return 8192
    if host_mem > 65536:
        return 32768
    return host_mem // 2


def reconfigure_stdout_utf8() -> None:
    """Force UTF-8 output where possible."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------
def find_qemu_img(host: HostPlatform) -> Optional[Path]:
    extra: list[Path] = [
        Path("/opt/homebrew/bin/qemu-img"),
        Path("/usr/local/bin/qemu-img"),
        Path("/opt/local/bin/qemu-img"),
    ]
    found = shutil.which("qemu-img")
    if found:
        return Path(found)
    for c in extra:
        if c.exists():
            return c
    return None


def install_qemu_img(host: HostPlatform) -> None:
    """Best-effort install of qemu-img via Homebrew."""
    log("Installing qemu via Homebrew ...", "step")
    if shutil.which("brew") is None:
        raise RuntimeError(
            "Homebrew not found. Install it from https://brew.sh first."
        )
    subprocess.check_call(["brew", "install", "qemu"])


def _pip_install(package: str) -> None:
    """Install a Python package into the active environment.

    Inside a venv, pip rejects --user, so we install directly into the
    venv. Outside a venv on PEP 668 (externally managed environment)
    systems (e.g. Homebrew Python) we fall back to --break-system-packages.
    """
    if sys.prefix != sys.base_prefix:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", package]
        )
        return
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "--quiet", package]
        )
        return
    except subprocess.CalledProcessError:
        pass
    # PEP 668 systems block --user installs; retry with --break-system-packages.
    log(f"pip --user failed for {package}, retrying with --break-system-packages", "warn")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "--quiet", package]
    )


def ensure_pycdlib() -> None:
    try:
        import pycdlib  # noqa: F401
        return
    except ImportError:
        pass
    log("Installing pycdlib (Python ISO creation) ...", "step")
    _pip_install("pycdlib")


def ensure_certifi() -> None:
    """Install certifi if missing, then refresh the module-level SSL
    context so subsequent _urlopen calls use the current CA bundle."""
    global _SSL_CONTEXT
    try:
        import certifi  # noqa: F401
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = _ssl.create_default_context(cafile=certifi.where())
        return
    except ImportError:
        pass
    log("Installing certifi (Mozilla CA bundle) ...", "step")
    _pip_install("certifi")
    import certifi  # type: ignore
    _SSL_CONTEXT = _ssl.create_default_context(cafile=certifi.where())


def ensure_jinja2() -> None:
    """Install Jinja2 if missing."""
    try:
        import jinja2  # noqa: F401
        return
    except ImportError:
        pass
    log("Installing Jinja2 (template engine for cloud-init user-data) ...", "step")
    _pip_install("Jinja2")
