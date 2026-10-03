"""QEMU tool discovery, disk preparation, and launcher rendering."""

from __future__ import annotations
import dataclasses
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional
from .config import VMConfig, SSH_PORT_RANGE, DISK_FILENAME, LAUNCHER_FILENAME
from .host import HostPlatform, guest_arch_for_host
from .log import log
from .provider import list_running_qemu_pids, find_running_ssh_port  # noqa: F401

@dataclasses.dataclass(frozen=True)
class QemuTools:
    """Resolved host-side tool paths."""
    qemu_system: Path
    guest_arch: str
    ovmf_code: Optional[Path] = None
    ovmf_vars: Optional[Path] = None


# Sentinel string inserted into the QEMU `hostfwd` arg when the SSH-forward
# port should be picked at LAUNCH time by the generated launcher script.
_SSH_PORT_PLACEHOLDER = "__SETUP_VM_SSH_FORWARD_PORT__"


# --------------------------------------------------------------------------
# QEMU tool discovery
# --------------------------------------------------------------------------
# Homebrew on Apple Silicon lives under /opt/homebrew; on Intel under
# /usr/local. MacPorts uses /opt/local. Order = most-likely first.
# The x86_64 lists are retained defensively; only aarch64 is validated/supported.
QEMU_SYSTEM_PATHS_MACOS = {
  "x86_64": [
  "/opt/homebrew/bin/qemu-system-x86_64",
  "/usr/local/bin/qemu-system-x86_64",
  "/opt/local/bin/qemu-system-x86_64",
  ],
  "aarch64": [
  str(Path.home() / "VMs" / "qemu-system-aarch64-gl"),
  "/opt/homebrew/bin/qemu-system-aarch64",
  "/usr/local/bin/qemu-system-aarch64",
  "/opt/local/bin/qemu-system-aarch64",
  ],
}

OVMF_CODE_PATHS_MACOS = {
  "x86_64": [
  "/opt/homebrew/share/qemu/edk2-x86_64-code.fd",
  "/usr/local/share/qemu/edk2-x86_64-code.fd",
  "/opt/local/share/qemu/edk2-x86_64-code.fd",
  ],
  "aarch64": [
  "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
  "/usr/local/share/qemu/edk2-aarch64-code.fd",
  "/opt/local/share/qemu/edk2-aarch64-code.fd",
  ],
}
OVMF_VARS_PATHS_MACOS = {
  "x86_64": [
  "/opt/homebrew/share/qemu/edk2-i386-vars.fd",
  "/usr/local/share/qemu/edk2-i386-vars.fd",
  "/opt/local/share/qemu/edk2-i386-vars.fd",
  ],
  "aarch64": [
  "/opt/homebrew/share/qemu/edk2-aarch64-vars.fd",
  "/usr/local/share/qemu/edk2-aarch64-vars.fd",
  "/opt/local/share/qemu/edk2-aarch64-vars.fd",
  # QEMU ships the AArch64 NVRAM vars template as edk2-arm-vars.fd
  # (one vars file covers both 32-bit ARM and 64-bit aarch64).
  "/opt/homebrew/share/qemu/edk2-arm-vars.fd",
  "/usr/local/share/qemu/edk2-arm-vars.fd",
  "/opt/local/share/qemu/edk2-arm-vars.fd",
  ],
}


def _find_ovmf_near_qemu(qemu_bin: Path, guest_arch: str) -> tuple[Optional[Path], Optional[Path]]:
    """Discover OVMF firmware files relative to the qemu-system binary.

    Homebrew may install QEMU under a versioned Cellar path (e.g.
    /opt/homebrew/Cellar/qemu/11.2.0/bin/qemu-system-aarch64) whose
    share/qemu/ sibling holds the firmware. The static path lists above
    only cover the unversioned symlink prefix (/opt/homebrew/share/qemu/);
    when those miss, we walk up from the binary to find the actual
    share/qemu/ directory.

    qemu_bin is resolved so a symlink at /opt/homebrew/bin/qemu-system-aarch64
    lands us in the versioned Cellar dir (where share/qemu/ actually lives).
    Callers must therefore pass the canonical or symlink path -- if they
    pre-resolve it to a path the walk-up may not find share/qemu/.
    """
    code_name = "edk2-aarch64-code.fd" if guest_arch == "aarch64" else "edk2-x86_64-code.fd"
    vars_names = (["edk2-aarch64-vars.fd", "edk2-arm-vars.fd"]
                  if guest_arch == "aarch64" else ["edk2-i386-vars.fd"])
    # 5 levels is enough for Homebrew (/opt/homebrew/Cellar/qemu-v/.../bin
    # -> up to /opt/homebrew/Cellar/qemu-v/.../share/qemu/ in 2-3 hops);
    # custom installs deeper than 5 levels deep are not supported.
    _MAX_OVMF_WALKUP_LEVELS = 5
    # Walk up from qemu-system binary looking for share/qemu/
    candidate = qemu_bin.resolve().parent
    for _ in range(_MAX_OVMF_WALKUP_LEVELS):
        share_qemu = candidate / "share" / "qemu"
        if share_qemu.is_dir():
            code = share_qemu / code_name
            vars_ = next((share_qemu / n for n in vars_names if (share_qemu / n).exists()), None)
            if code.exists() or vars_ is not None:
                return (code if code.exists() else None, vars_)
        candidate = candidate.parent
        if candidate == candidate.parent:
            break
    return None, None


def _find_first(paths: list[str]) -> Optional[Path]:
    for p in paths:
        if Path(p).exists():
            return Path(p)
    return None


def _qemu_has_virgl(qemu: Path, guest_arch: str) -> bool:
    """Return True if *qemu* was built with virglrenderer support.

    Stock Homebrew QEMU has ``virtio-gpu-pci`` but not
    ``virtio-gpu-gl-pci``; only qemu-virgl (or our custom GL build)
    exposes the GL variant.  Probe by checking the Mach-O load commands
    for a link to libvirglrenderer — instant and impossible to false-negative.
    """
    try:
        proc = subprocess.run(
            ["otool", "-L", str(qemu)],
            capture_output=True, text=True, timeout=10,
        )
        return "libvirglrenderer" in proc.stdout
    except Exception:
        return False


def _probe_env() -> dict[str, str]:
    """Env for capability probes, mirroring the launch environment.

    The qemu-virgl binary resolves EGL/virgl via dylibs in /opt/homebrew/lib
    at load time; a probe launched without DYLD_LIBRARY_PATH can fail for
    environment reasons and look like a missing device.
    """
    env = dict(os.environ)
    dyld = env.get("DYLD_LIBRARY_PATH", "")
    if "/opt/homebrew/lib" not in dyld.split(":"):
        env["DYLD_LIBRARY_PATH"] = (
            "/opt/homebrew/lib" + (":" + dyld if dyld else "")
        )
    return env


def _warn_if_probe_dead(qemu: Path, proc: subprocess.CompletedProcess) -> None:
    """Warn when a probe binary produced no output at all.

    A SIGKILLed or otherwise non-executable QEMU binary (bad code
    signature, missing dylib) exits non-zero with empty stdout, which is
    indistinguishable from "device absent" in the probe results and used
    to silently downgrade the launcher to the non-GL llvmpipe GPU device.
    """
    if proc.returncode != 0 and not proc.stdout.strip():
        log(
            f"Probe of {qemu} failed to run (rc={proc.returncode}, no "
            "output); capability results are unreliable -- the launcher "
            "may fall back to a non-GL GPU device.",
            "warn",
        )


def _qemu_supports(qemu: Path, guest_arch: str, what: str, name: str) -> bool:
    """Return whether this qemu build supports a chardev backend or device.

    Homebrew QEMU builds vary: recent ones omit SPICE support (spicevmc)
    and virtio-vga, which made the launcher crash before the guest booted.
    Probe the binary at build time and default to conservative True if the
    probe itself fails, so a working configuration never regresses.
    """
    machine = {"x86_64": "q35", "aarch64": "virt"}[guest_arch]
    try:
        if what == "chardev":
            proc = subprocess.run(
                [str(qemu), "-machine", machine, "-chardev", "help"],
                capture_output=True, text=True, timeout=15,
                env=_probe_env(),
            )
            _warn_if_probe_dead(qemu, proc)
            return name in proc.stdout.split()
        if what == "device":
            proc = subprocess.run(
                [str(qemu), "-machine", machine, "-device", "help"],
                capture_output=True, text=True, timeout=15,
                env=_probe_env(),
            )
            _warn_if_probe_dead(qemu, proc)
            return f'name "{name}"' in proc.stdout
        return True
    except Exception:
        return True


def _qemu_device_has_prop(qemu: Path, guest_arch: str, device: str, prop: str) -> bool:
    """Return whether a QEMU device exposes a given property.

    Probes ``qemu -device <device>,help`` and looks for ``prop=<type>`` in
    the output.  Falls back to True if the probe itself fails.
    """
    machine = {"x86_64": "q35", "aarch64": "virt"}[guest_arch]
    try:
        proc = subprocess.run(
            [str(qemu), "-machine", machine, "-device", f"{device},help"],
            capture_output=True, text=True, timeout=15,
            env=_probe_env(),
        )
        _warn_if_probe_dead(qemu, proc)
        return f"{prop}=" in proc.stdout
    except Exception:
        return True


def _qemu_display_gl_works(qemu: Path, guest_arch: str) -> bool:
    """Return whether this QEMU build's cocoa display supports gl=es.

    The cocoa display backend must implement a GL context creation callback
    for ``-display cocoa,gl=es`` to work.  Homebrew bottle rebuilds have
    been known to drop this support (the device probe finds virtio-gpu-gl-pci
    because virglrenderer is compiled in, but the cocoa display module
    lacks the GL rendering interface).  Probe by attempting a minimal QEMU
    startup with ``-display cocoa,gl=es`` and checking for the fatal error
    message ``OpenGL is not supported by display backend``.  Falls back to
    True if the probe itself fails (so a working configuration never
    regresses).
    """
    machine = {"x86_64": "q35", "aarch64": "virt"}[guest_arch]
    try:
        proc = subprocess.run(
            [str(qemu),
             "-machine", machine,
             "-cpu", "cortex-a72" if guest_arch == "aarch64" else "qemu64",
             "-m", "256",
             "-display", "cocoa,gl=es",
             "-device", "virtio-gpu-gl-pci",
             "-serial", "file:/dev/null", "-monitor", "none"],
            capture_output=True, text=True, timeout=10,
            env=_probe_env(),
        )
        combined = proc.stdout + proc.stderr
        if "OpenGL is not supported by display backend" in combined:
            return False
    except Exception:
        pass
    return True


def _ensure_qemu_app(qemu_bin: Path) -> Path:
    """Return the path to the QEMU binary to execute.

    Previously this wrapped the binary in a macOS .app bundle for cosmetic
    Dock-icon purposes.  The bundle introduced signing, firmware-symlink,
    and cache-invalidation complexity for zero functional benefit, and it
    conflicted with our custom GL-patched QEMU binary.  Simplified to
    return the raw binary path directly.
    """
    return qemu_bin


def _top_level_prefix(p: Path) -> Optional[str]:
    """Return the macOS package-manager top-level dir (/opt/homebrew, /usr/local, /opt/local)
    for `p`, or None if `p` is not under any known prefix.

    Used to verify the QEMU binary and the OVMF firmware it uses come from
    the same package manager; a cross-prefix combination (e.g. a MacPorts
    qemu-system with a Homebrew OVMF) can produce an ABI-incompatible
    firmware and a silent boot failure.
    """
    s = str(p.resolve())
    for prefix in ("/opt/homebrew/", "/usr/local/", "/opt/local/"):
        if s.startswith(prefix):
            return prefix
    return None


def detect_tools(host: HostPlatform) -> QemuTools:
    guest_arch = guest_arch_for_host(host.arch)
    qemu = _find_first(QEMU_SYSTEM_PATHS_MACOS[guest_arch])
    if qemu is None:
        raise RuntimeError(
            f"qemu-system-{guest_arch} not found at any of: "
            f"{', '.join(QEMU_SYSTEM_PATHS_MACOS[guest_arch])}. "
            "Install qemu-virgl via Homebrew or place the GL binary at "
            f"{QEMU_SYSTEM_PATHS_MACOS[guest_arch][0]}."
        )
    # Reject stock Homebrew QEMU: it lacks virglrenderer and the
    # cocoa,gl=es display backend.  Only qemu-virgl (or our custom GL
    # binary) has the virtio-gpu-gl-pci device we need.
    if not _qemu_has_virgl(qemu, guest_arch):
        raise RuntimeError(
            f"{qemu} is not a virgl-enabled QEMU build. "
            "This project requires qemu-virgl. Install it with: "
            "brew install milesbuckton/qemu-virgl/qemu-virgl"
        )
    # Discover OVMF relative to the qemu-system binary FIRST (always correct
    # for versioned Cellar installs). Static paths are a fallback only.
    dyn_code, dyn_vars = _find_ovmf_near_qemu(qemu, guest_arch)
    ovmf_code = _find_first(OVMF_CODE_PATHS_MACOS[guest_arch])
    ovmf_vars = _find_first(OVMF_VARS_PATHS_MACOS[guest_arch])
    # Prefer binary-relative discovery; fall back to static paths only when
    # the binary-relative walk-up doesn't find an OVMF (e.g. non-Homebrew install).
    candidate_code = dyn_code if dyn_code is not None else ovmf_code
    candidate_vars = dyn_vars if dyn_vars is not None else ovmf_vars
    # Prefix safety: if the binary and the firmware come from different
    # package managers (e.g. a MacPorts qemu-system using a Homebrew OVMF),
    # the firmware may be ABI-incompatible and the VM will fail to boot
    # silently. Refuse the mismatch and let the caller fall back to the
    # binary-relative firmware.
    qemu_prefix = _top_level_prefix(qemu)
    for cand in (candidate_code, candidate_vars):
        if cand is None:
            continue
        cp = _top_level_prefix(cand)
        if qemu_prefix is not None and cp is not None and cp != qemu_prefix:
            log(
                f"OVMF firmware {cand} is under {cp.rstrip('/')} but "
                f"qemu-system {qemu} is under {qemu_prefix.rstrip('/')} -- "
                "ignoring the cross-prefix firmware to avoid an ABI mismatch.",
                "warn",
            )
            if cand is candidate_code:
                candidate_code = dyn_code if dyn_code is not None else None
            if cand is candidate_vars:
                candidate_vars = dyn_vars if dyn_vars is not None else None
    ovmf_code = candidate_code
    ovmf_vars = candidate_vars
    return QemuTools(
        qemu_system=qemu,
        guest_arch=guest_arch,
        ovmf_code=ovmf_code,
        ovmf_vars=ovmf_vars,
    )


def install_hint(host: HostPlatform) -> str:
    guest_arch = guest_arch_for_host(host.arch)
    return (
        f"qemu-system-{guest_arch} not found. Install via Homebrew:\n"
        "         brew install qemu\n"
        "       HVF acceleration is built into macOS; no extra step.\n"
        "       Then re-run this script."
    )


def prepare_disk(
    source: Path,
    target: Path,
    disk_gb: int,
    qemu_img: Path,
) -> None:
    """For QEMU we keep qcow2 native: copy + resize."""
    log(f"Preparing qcow2 disk ({source.name} -> {target.name}) ...", "step")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    subprocess.check_call(
        [str(qemu_img), "convert", "-p", "-O", "qcow2", str(source), str(target)]
    )
    log(f"Resizing qcow2 to {disk_gb} GB ...", "step")
    subprocess.check_call(
        [str(qemu_img), "resize", str(target), f"{disk_gb}G"]
    )
    log("Disk ready.", "ok")


def _build_qemu_argv(
    cfg: VMConfig,
    firmware: str,
    tools: QemuTools,
    target_dir: Path,
) -> list[str]:
    """Build the qemu-system argv for the host's guest architecture.

    Assembled from focused helpers below (machine/core, seed, network,
    graphics, audio, usb, serial, spice, guest-agent, firmware) so each
    subsystem is easy to reason about in isolation.
    """
    qemu = tools.qemu_system
    if qemu is None:
        raise RuntimeError(
            "qemu-system not found in the host's QEMU install."
        )
    guest_arch = tools.guest_arch
    if guest_arch not in ("x86_64", "aarch64"):
        raise RuntimeError(
            f"Unsupported guest architecture for QEMU: {guest_arch!r}"
        )

    ssh_port = str(cfg.ssh_port) if cfg.ssh_port is not None else _SSH_PORT_PLACEHOLDER
    disk = target_dir / DISK_FILENAME
    seed = target_dir / cfg.seed_filename

    argv: list[str] = [str(qemu)]
    argv += _argv_machine_core(cfg, guest_arch, disk)
    argv += _argv_seed(guest_arch, seed)
    argv += _argv_network(ssh_port)
    # virgl-enabled QEMU builds (e.g. `brew install
    # milesbuckton/qemu-virgl/qemu-virgl`) expose GL-variant GPU devices and
    # render the guest in hardware instead of llvmpipe.
    # Probe the binary: plain Homebrew QEMU lacks the `-gl-` devices, so
    # it keeps the existing llvmpipe path. Only use `cocoa,gl=es` when a
    # GL device was actually selected (the two come from the same build
    # flags).
    gfx = _argv_graphics(qemu, guest_arch)
    argv += gfx

    # Venus requires a memory-backend object so the virtio-gpu device can
    # map guest RAM into the render-server process for blob resources.
    # memory-backend-memfd is Linux-only; memory-backend-ram works on
    # macOS HVF.  The -machine memory-backend=<obj> tells QEMU to back
    # guest RAM with this object instead of its anonymous default.
    # Reference configs: tm23forest.com, peppergrayxyz gist — all use
    # -object memory-backend-memfd + -machine memory-backend=<obj>.
    if any("venus=on" in a for a in argv):
        mem_mb = cfg.memory_mb
        mem_obj = f"memory-backend-ram,id=mem1,size={mem_mb}M"
        argv += ["-object", mem_obj]
        # Patch the -machine spec to reference the memory backend.
        try:
            m_idx = argv.index("-machine")
            argv[m_idx + 1] += ",memory-backend=mem1"
        except (ValueError, IndexError):
            pass

    argv += _argv_audio(guest_arch)
    argv += _argv_usb()
    argv += _argv_serial(target_dir)
    argv += _argv_spice(qemu, guest_arch)
    argv += _argv_firmware(firmware, tools, target_dir, guest_arch)
    return argv


def _argv_machine_core(
    cfg: VMConfig, guest_arch: str, disk: Path
) -> list[str]:
    """Guest name, machine type, CPU, SMP topology, RAM, and the boot disk."""
    machine_spec = {
        "x86_64": "q35,accel=hvf:tcg",
        # kernel-irqchip=off: qemu-virgl master (11.1.50, 2026-09 bottle) hangs
        # EDK2 boot under HVF with its default in-kernel vGIC (virt-11.1+) —
        # vCPU spins at a fixed PC with zero HVF exits. Forcing userspace GIC
        # emulation restores boot. Revisit once fixed upstream.
        "aarch64": "virt,accel=hvf:tcg,kernel-irqchip=off",
    }[guest_arch]
    return [
        "-name", cfg.vm_name,
        "-machine", machine_spec,
        "-cpu", "host",
        "-smp", f"cpus={cfg.vcpus},sockets=1,cores={cfg.vcpus},threads=1",
        "-m", str(cfg.memory_mb),
        "-drive", f"file={disk},if=none,format=qcow2,id=hd0",
        "-device", "virtio-blk-pci,drive=hd0,bootindex=1",
    ]


def _argv_seed(guest_arch: str, seed: Path) -> list[str]:
    """Cloud-init seed ISO.

    x86_64: q35 machine with an AHCI/IDE CD-ROM for the seed ISO.
    aarch64: the virt machine has no IDE; attach the seed as a
    virtio-blk device. A USB-storage seed must NOT be used here: it
    enumerates too late for cloud-init's ds-identify (which runs from
    the cloud-init-generator at early boot, before USB is up), so
    cloud-init disables itself for the whole boot. virtio-blk is
    discovered with the root device, before ds-identify runs.
    """
    if guest_arch == "x86_64":
        return [
            "-drive",
            f"file={seed},if=none,format=raw,id=cd0,media=cdrom,readonly=on",
            "-device", "ide-cd,drive=cd0",
        ]
    return [
        "-drive",
        f"file={seed},if=none,format=raw,id=cd0,readonly=on",
        "-device", "virtio-blk-pci,drive=cd0",
    ]


def _argv_network(ssh_port: str) -> list[str]:
    """User-mode NIC with a loopback-only SSH host-forward (M5)."""
    return [
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
        "-device", "virtio-net-pci,netdev=net0",
    ]


def _argv_graphics(qemu: Path, guest_arch: str) -> list[str]:
    """GPU device + display, probing for a virgl GL device.

    Venus configuration follows the canonical setup from tm23forest.com and
    the peppergrayxyz gist: virtio-vga-gl (not virtio-gpu-gl-pci), -vga none,
    blob=on, hostmem=4G.  The -vga none flag prevents a duplicate VGA scanout
    when using virtio-vga-gl (the device IS the VGA device).  On aarch64 the
    virt machine has no legacy VGA, so -vga none is harmless but kept for
    consistency with x86_64 reference configs.
    """
    # Probe for the best GL-capable GPU device.
    gl_dev = "virtio-vga-gl" if _qemu_supports(qemu, guest_arch, "device", "virtio-vga-gl") else "virtio-gpu-gl-pci"
    gl_ok = _qemu_supports(qemu, guest_arch, "device", gl_dev)

    # The cocoa display backend may not implement the GL context interface
    # (bottle rebuilds have dropped it).  If GL display is unavailable, we
    # must fall back to a non-GL device — a GL device without a GL display
    # backend triggers "The display backend does not have OpenGL support".
    display_gl = gl_ok and _qemu_display_gl_works(qemu, guest_arch)

    if display_gl:
        gpu_dev = gl_dev
    else:
        # Non-GL fallback: virtio-vga (legacy VGA) preferred, else virtio-gpu-pci
        gpu_dev = "virtio-vga" if _qemu_supports(qemu, guest_arch, "device", "virtio-vga") else "virtio-gpu-pci"

    # Venus only works on a GL device with GL display support.
    venus = (display_gl
             and _qemu_device_has_prop(qemu, guest_arch, gl_dev, "venus"))
    gpu_spec = f"{gpu_dev},xres=3456,yres=2234"
    if venus:
        # hostmem=4G: Venus needs sufficient shared memory for blob
        # resources.  512m was too small; reference configs use 4G.
        gpu_spec += ",venus=on,blob=on,hostmem=4G"
    display = "cocoa,gl=es" if display_gl else "cocoa"
    argv = ["-device", gpu_spec]
    if venus:
        # -vga none: prevent the default VGA device from being created
        # alongside virtio-vga-gl (avoids dual-scanout assertion on
        # x86_64; harmless on aarch64).
        argv += ["-vga", "none"]
    argv += ["-display", display]
    return argv


def _argv_audio(guest_arch: str) -> list[str]:
    """Audio device, arch-split (virtio-sound on aarch64, HDA on x86_64)."""
    argv = ["-audiodev", "coreaudio,id=snd0"]
    if guest_arch == "aarch64":
        # aarch64 distro kernels ship ONLY the virtio_snd
        # driver for PCI audio -- no snd-hda-intel. Use virtio-sound-pci.
        # streams=1: QEMU's coreaudio host backend is output-only (HISTORY
        # #30), so expose just the playback stream -- the default streams=2
        # creates an input stream that can never open (virtio-sound.in
        # "no host audio driver" retries on every guest capture attempt).
        argv += ["-device", "virtio-sound-pci,streams=1,audiodev=snd0"]
    else:
        argv += ["-device", "intel-hda", "-device", "hda-output,audiodev=snd0"]
    return argv


def _argv_usb() -> list[str]:
    """xHCI controller + keyboard + tablet (absolute coords)."""
    return [
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd,bus=xhci.0",
        "-device", "usb-tablet,bus=xhci.0",
    ]


def _argv_serial(target_dir: Path) -> list[str]:
    """Host-captured serial console (console.log) + virtio-serial bus."""
    return [
        "-serial", f"file:{target_dir / 'console.log'}",
        "-device", "virtio-serial-pci",
    ]


def _argv_spice(qemu: Path, guest_arch: str) -> list[str]:
    """SPICE vdagent channel, only when the QEMU build supports it."""
    if _qemu_supports(qemu, guest_arch, "chardev", "spicevmc"):
        return [
            "-device", "virtserialport,chardev=spicechannel0,name=com.redhat.spice.0",
            "-chardev", "spicevmc,id=spicechannel0,name=vdagent",
        ]
    return []


def _argv_firmware(
    firmware: str, tools: QemuTools, target_dir: Path, guest_arch: str
) -> list[str]:
    """EFI firmware (OVMF) pflash drives; errors hard on aarch64."""
    if firmware != "efi":
        return []
    fw_code = tools.ovmf_code
    fw_vars = tools.ovmf_vars
    if fw_code is not None and fw_vars is not None:
        vm_vars = target_dir / "nvram.fd"
        if not vm_vars.exists():
            shutil.copy(fw_vars, vm_vars)
        return [
            "-drive", f"if=pflash,format=raw,readonly=on,file={fw_code}",
            "-drive", f"if=pflash,format=raw,file={vm_vars}",
        ]
    if guest_arch == "aarch64":
        # No SeaBIOS exists for aarch64 -- the guest cannot boot
        # without firmware, so this is a hard error, not a warning.
        raise RuntimeError(
            "aarch64 guest needs EDK2 firmware, but no OVMF "
            "(edk2-aarch64-code.fd / edk2-arm-vars.fd) was found in "
            "the QEMU install. Reinstall QEMU with `brew reinstall qemu`."
        )
    log(
        "OVMF firmware not found; falling back to SeaBIOS. "
        "Distros expecting UEFI may fail to boot.",
        "warn",
    )
    return []


# Per-VM artifact filenames live in config.py (DRY with the orchestrator
# and monitor which also look them up by name).


def render_launcher(
    cfg: VMConfig,
    firmware: str,
    tools: QemuTools,
) -> str:
    """Render the shell launcher script."""
    argv = _build_qemu_argv(cfg, firmware, tools, cfg.target_dir)
    needs_port_probe = any(_SSH_PORT_PLACEHOLDER in a for a in argv)
    lines = ["#!/usr/bin/env bash",
             "# Auto-generated QEMU launcher. Tweak and re-run as needed.",
             "set -e"]
    if needs_port_probe:
        port_start, port_end = SSH_PORT_RANGE
        lines.append(f"""
# Pick a free TCP port for the SSH host-forward. Probed on 127.0.0.1
# because the forward binds loopback-only (M5: never expose the guest
# SSH on a non-loopback interface).
sshFwdPort=$(python3 - <<'PY'
import socket
for p in range({port_start}, {port_end}):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p)); s.close(); print(p); break
    except OSError: continue
else:
    print({port_start})
PY
)
echo \"[setup_vm launcher] SSH-forward host port: $sshFwdPort  (ssh -p $sshFwdPort ...)\"
""".strip())
    # Stale guest-agent socket from a previous run: QEMU refuses to
    # rebind over an existing unix socket path, so drop it first.
    qga_sock = cfg.target_dir / "qga.sock"
    lines.append(f"rm -f '{qga_sock}'  # stale qga.sock (QEMU won't rebind)")
    # libepoxy resolves EGL at runtime via dlopen("libEGL.dylib"),
    # which lives in /opt/homebrew/lib and is NOT on QEMU's default
    # dlopen search path. Export it so cocoa,gl=es display init
    # works. Must be set inside the script (not via subprocess env)
    # because macOS strips DYLD_* from processes without the
    # allow-dyld-environment-variables entitlement, and /bin/sh
    # (the script interpreter) does not have it.
    dyld_path = '"/opt/homebrew/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"'
    lines.append(f"export DYLD_LIBRARY_PATH={dyld_path}")
    # Venus renderer (virgl_render_server) needs VK_ICD_FILENAMES to locate
    # MoltenVK on the host.  Without it, venus init fails and QEMU aborts
    # with "virgl could not be initialized: -1".
    lines.append('export VK_ICD_FILENAMES="/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json"')
    lines.append("exec \\")
    for i, a in enumerate(argv):
        if _SSH_PORT_PLACEHOLDER in a:
            before, after = a.split(_SSH_PORT_PLACEHOLDER, 1)
            escaped_before = before.replace("'", "'\\''")
            escaped_after = after.replace("'", "'\\''")
            escaped = f"'{escaped_before}'\"$sshFwdPort\"'{escaped_after}'"
            sep = " \\" if i < len(argv) - 1 else ""
            lines.append(f"  {escaped}{sep}")
        else:
            escaped = a.replace("'", "'\\''")
            sep = " \\" if i < len(argv) - 1 else ""
            lines.append(f"  '{escaped}'{sep}")
    return "\n".join(lines) + "\n"


def launch(definition_path: Path) -> None:
    """Spawn the launcher script as a detached session."""
    log("Starting QEMU VM ...", "step")
    console_log = definition_path.parent / "console.log"
    try:
        console_log.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"Note: could not delete {console_log}: {e}", "warn")
    try:
        # Add only the execute bits; ORing the full 0o755 mask would
        # also grant world-writability to the launcher script.
        definition_path.chmod(definition_path.stat().st_mode | 0o111)
    except OSError:
        pass
    # The qemu-virgl build resolves EGL at runtime via libepoxy dlopen of
    # libEGL.dylib, which lives in /opt/homebrew/lib and is NOT on QEMU's
    # default dlopen search path. Without it the cocoa,gl=es display init
    # aborts at startup (SIGABRT). Export it so the GPU offload path works.
    env = dict(os.environ)
    dyld = env.get("DYLD_LIBRARY_PATH", "")
    if "/opt/homebrew/lib" not in dyld.split(":"):
        env["DYLD_LIBRARY_PATH"] = (
            "/opt/homebrew/lib" + (":" + dyld if dyld else "")
        )
    # Pass VK_ICD_FILENAMES to the shell so it survives exec into
    # the QEMU process (macOS can strip env from non-entitled parents).
    icd_path = "/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json"
    if "VK_ICD_FILENAMES" not in env:
        env["VK_ICD_FILENAMES"] = icd_path
    subprocess.Popen(
        ["/bin/sh", str(definition_path)],
        cwd=str(definition_path.parent),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    log("QEMU launched. cloud-init will run unattended (~30 minutes).", "step")
