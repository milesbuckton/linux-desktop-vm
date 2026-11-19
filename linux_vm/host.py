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
    """Only Apple Silicon is validated for unattended VM builds."""
    return machine.lower() in ("arm64", "aarch64")


def guest_arch_for_host(machine: str) -> str:
    """Map host `platform.machine()` to the guest/QEMU arch token.

    arm64/aarch64 hosts run aarch64 guests (the only validated path; the
    mapping also covers x86_64 hosts for defensive completeness). Anything
    else raises (the gate in orchestrate.py already refuses to run, so
    this is a defensive backstop).
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
    return "UTC"


def physical_cpu_count() -> Optional[int]:
    """Best-effort physical-core count (macOS: sysctl hw.physicalcpu).

    Returns None if it can't be determined. Logical cores are deliberately
    avoided: hyperthreaded CPUs report 2x in hw.ncpu, and counting
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
    the host. The floor (8 GB) is the minimum for a usable GNOME desktop and
    WINS over halving, so on hosts with < 16 GB the VM may use more than
    half of the host's RAM (an 8 GB Mac gives the VM the full 8 GB; the host
    relies on macOS memory compression + swap). The cap (32 GB) reflects
    that past that point extra RAM barely matters for a desktop VM.
    """
    if host_mem is None:
        host_mem = host_memory_mb()
    if host_mem is None:
        return 16384
    return max(8192, min(host_mem // 2, 32768))


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
QEMU_VIRGL_INSTALL_HINT = "brew tap milesbuckton/qemu-virgl && brew install qemu-virgl"
QEMU_VIRGL_OPT_QEMU_IMG = Path("/opt/homebrew/opt/qemu-virgl/bin/qemu-img")


def _is_qemu_virgl_keg_path(path: Path) -> bool:
    """True when `path` resolves into a Homebrew `Cellar/qemu-virgl` keg."""
    try:
        parts = path.resolve().parts
    except OSError:
        return False
    return any(a == "Cellar" and b == "qemu-virgl" for a, b in zip(parts, parts[1:]))


def find_qemu_img(
    host: HostPlatform,
    qemu_system: Optional[Path] = None,
) -> Optional[Path]:
    """Locate the qemu-virgl `qemu-img`; a stock Homebrew `qemu` one is never used.

    Probe order:
      1. /opt/homebrew/opt/qemu-virgl/bin/qemu-img -- qemu-virgl is keg-only,
         so its qemu-img is never on PATH; the opt symlink survives
         reinstalls, unlike a forced `brew link`.
      2. Next to the real (symlink-resolved) path of the detected,
         virgl-verified `qemu_system` binary (covers custom GL builds).
      3. PATH, only when it resolves into a Cellar/qemu-virgl keg.
    """
    if QEMU_VIRGL_OPT_QEMU_IMG.is_file():
        return QEMU_VIRGL_OPT_QEMU_IMG
    if qemu_system is not None:
        try:
            sibling = qemu_system.resolve().parent / "qemu-img"
        except OSError:
            sibling = None
        if sibling is not None and sibling.is_file():
            return sibling
    found = shutil.which("qemu-img")
    if found and _is_qemu_virgl_keg_path(Path(found)):
        return Path(found)
    return None


def _require_package(import_name: str, package_name: str) -> None:
    """Fail clearly when a runtime dependency is missing; never install globally."""
    try:
        __import__(import_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency {package_name!r}. Activate the project .venv "
            "and install its declared requirements."
        ) from exc


def ensure_pycdlib() -> None:
    _require_package("pycdlib", "pycdlib")


def ensure_certifi() -> None:
    """Require certifi and refresh the module-level SSL context."""
    global _SSL_CONTEXT
    try:
        import certifi
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'certifi'. Activate the project .venv and "
            "install its declared requirements."
        ) from exc
    _SSL_CONTEXT = _ssl.create_default_context(cafile=certifi.where())


def ensure_jinja2() -> None:
    _require_package("jinja2", "Jinja2")
