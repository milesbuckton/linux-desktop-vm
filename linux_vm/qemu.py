"""QEMU tool discovery, disk preparation, and launcher rendering."""

from __future__ import annotations
import fcntl
import dataclasses
import hashlib
import os
import plistlib
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
            out = subprocess.run(
                [str(qemu), "-machine", machine, "-chardev", "help"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            return name in out.split()
        if what == "device":
            out = subprocess.run(
                [str(qemu), "-machine", machine, "-device", "help"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            return f'name "{name}"' in out
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
        out = subprocess.run(
            [str(qemu), "-machine", machine, "-device", f"{device},help"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return f"{prop}=" in out
    except Exception:
        return True


def _ensure_qemu_app(qemu_bin: Path) -> Path:
    """Return a path to `qemu_bin` that lives inside a proper macOS .app bundle.

    Homebrew ships qemu-system-* as bare binaries, and the QEMU cocoa display
    never sets a Dock icon itself (the icon it loads is only for the About
    panel) -- so the Dock shows the generic "exec" icon. We build
    ~/VMs/QEMU.app once (official QEMU icon + Info.plist + a copy of the
    real binary) and exec through the bundle path, which makes macOS give the
    Dock the QEMU logo. Any failure falls back to the raw binary: purely
    cosmetic, never a launch blocker.

    The bundle is shared across concurrent builds/VMs, so mutation is
    guarded: a host-level flock serialises rebuilds, and the rebuild itself
    is skipped when the bundled exe already matches the source binary
    (path + size + mtime recorded in a marker file). Without the marker a
    fresh copy + re-sign would happen on EVERY build and could race a VM
    already launching from the bundle (M6).
    """
    if sys.platform != "darwin":
        return qemu_bin
    try:
        bundle = Path.home() / "VMs" / "QEMU.app"
        lock_path = bundle.parent / ".QEMU.app.lock"
        with lock_path.open("w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                return _ensure_qemu_app_locked(qemu_bin, bundle)
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except Exception:
        return qemu_bin


def _link_qemu_datadir(qemu_bin: Path, bundle: Path) -> None:
    """Symlink the source QEMU install's `share/qemu/` into the bundle.

    QEMU resolves firmware and romfile paths relative to its own
    binary location (`<binary>/../share/qemu/...`). The source install
    layout -- e.g. /opt/homebrew/Cellar/qemu-virgl/11.1.1/bin/ with a
    sibling /opt/homebrew/Cellar/qemu-virgl/11.1.1/share/qemu/ --
    satisfies this; the bundle's Contents/MacOS/ layout does not, so
    without this symlink QEMU aborts with
    `failed to find romfile efi-virtio.rom` on devices that need
    option ROMs (virtio-net-pci, e1000, etc.).

    Best-effort: if we can't find a source share/qemu (e.g. a
    user-installed QEMU without the standard layout), the bundle
    is built without the link and QEMU will likely fail at launch
    with the same romfile error -- which is the same failure mode
    as before, not a regression. The caller still gets a signed
    bundle and the failure surfaces at first launch.
    """
    # Walk up from the source binary looking for share/qemu/, same
    # algorithm as _find_ovmf_near_qemu. This is correct for Homebrew
    # Cellar installs (the supported configuration).
    _MAX_DATADIR_WALKUP_LEVELS = 5
    candidate = qemu_bin.resolve().parent
    for _ in range(_MAX_DATADIR_WALKUP_LEVELS):
        share_qemu = candidate / "share" / "qemu"
        if share_qemu.is_dir():
            break
        candidate = candidate.parent
        if candidate == candidate.parent:
            share_qemu = None
            break
    else:
        share_qemu = None
    if share_qemu is None:
        log(
            f"Could not find share/qemu next to {qemu_bin}; bundle will not "
            "include a firmware/romfile symlink. QEMU may fail to launch "
            "with 'failed to find romfile' errors.",
            "warn",
        )
        return
    # Contents/share/qemu -> source's share/qemu. From the binary's
    # perspective this puts share/qemu at <Contents/MacOS>/../share/qemu,
    # which is the standard layout QEMU expects when it resolves
    # firmware/romfile paths relative to its own binary.
    contents = bundle / "Contents"
    share_dir = contents / "share"
    link_path = share_dir / "qemu"
    # Ensure Contents/share/ exists as a directory (not a symlink) so
    # the qemu symlink inside it is unambiguous.
    if share_dir.is_symlink():
        # Stale symlink from a prior install; resolve and verify it
        # points at a real directory before re-using.
        target = share_dir.resolve()
        if not target.is_dir():
            share_dir.unlink()
    if not share_dir.exists():
        contents.mkdir(parents=True, exist_ok=True)
        share_dir.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink() and link_path.resolve() == share_qemu.resolve():
        return  # already correct
    if link_path.is_symlink() or link_path.exists():
        if link_path.is_symlink():
            link_path.unlink()
        else:
            # Real directory at the symlink path -- a prior install
            # copied share/qemu contents here. Don't clobber; trust it.
            return
    link_path.symlink_to(share_qemu)


def _sha256(p: Path) -> str:
    """SHA-256 of a file's contents (streaming; qemu-system is ~33 MB)."""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_qemu_app_locked(qemu_bin: Path, bundle: Path) -> Path:
    """Build/sign the QEMU.app bundle for `qemu_bin` (lock already held)."""
    exe_name = qemu_bin.name
    bundled_exe = bundle / "Contents" / "MacOS" / exe_name
    # Marker and entitlements plist live OUTSIDE the .app bundle so
    # macOS code-signing validation doesn't treat them as subcomponents
    # (which causes "code object is not signed at all In subcomponent"
    # or "invalid or unsupported format for signature" errors).
    marker = bundle.parent / f".{exe_name}.bundle-src"
    hv_entitlements = bundle.parent / f".{exe_name}.hv-entitlements.plist"
    stat = qemu_bin.stat()
    # Marker = resolved path + size + mtime + content hash of the SOURCE
    # binary. The hash is what actually protects us: a Homebrew/local
    # rebuild can replace the binary in place at the same Cellar path
    # with a same-second mtime, and size can coincidentally match across
    # builds. Without the hash, the 2026-09-10 incident happens: the
    # marker claims the bundle matches qemu-virgl/master while the
    # bundled exe is a stale copy.
    src_hash = _sha256(qemu_bin)
    signature = f"{qemu_bin.resolve()}\n{stat.st_size}\n{stat.st_mtime_ns}\n{src_hash}\n"
    # The bundled exe is codesigned after copying, which rewrites its
    # __LINKEDIT segment and changes its hash. So we cannot compare the
    # bundled exe's on-disk hash to src_hash; instead we record the
    # PRE-SIGN hash of the copy in a sibling marker and compare that.
    presign_marker = bundle.parent / f".{exe_name}.bundle-presign"
    copy_is_real_file = bundled_exe.exists() and not bundled_exe.is_symlink()
    marker_matches = marker.exists() and marker.read_text() == signature
    # The presign marker carries "<sha256>\n<size>\n<mtime_ns>\n" of the
    # bundled exe as recorded right after a verified copy + successful
    # sign. Comparing size+mtime of the CURRENT bundled exe against the
    # marker catches a post-marker rewrite of the exe (observed
    # 2026-09-10: exe replaced ~2h after the source, markers claimed a
    # match, the stale 32.8 MB exe crashed in the cocoa gl=es path).
    # Codesign does not change st_size/st_mtime_ns semantics here
    # (in-place rewrite keeps mtime in the same second it signs, and we
    # write the marker afterwards), so an exact match is safe.
    presign_matches = False
    if presign_marker.exists() and copy_is_real_file:
        parts = presign_marker.read_text().splitlines()
        if len(parts) == 3:
            be_stat = bundled_exe.stat()
            presign_matches = parts == [
                src_hash, str(be_stat.st_size), str(be_stat.st_mtime_ns)
            ]
    if copy_is_real_file and marker_matches and presign_matches:
        return bundled_exe

    bundled_exe.parent.mkdir(parents=True, exist_ok=True)
    if bundled_exe.exists() or bundled_exe.is_symlink():
        bundled_exe.unlink()
    # Copy (NOT symlink/hardlink): codesign refuses symlinked
    # executables, and a hardlink shares the inode with the Homebrew
    # binary so re-signing would mutate it. A copy keeps the bundle
    # self-contained and independently signable.
    # If the copy fails, the bundle build is broken: don't fall back to
    # a symlink, which would re-introduce the codesign-refusal bug
    # documented above and leave the VM unable to launch under HVF.
    shutil.copy2(qemu_bin, bundled_exe)
    # Hash the copy BEFORE signing: codesign rewrites __LINKEDIT, so
    # post-sign the hash no longer matches the source. If the source was
    # being rebuilt in place while we copied (observed 2026-09-10:
    # marker said Cellar/qemu-virgl/master, bundled exe was a stale
    # 32.8 MB binary vs the 33 MB source), the copy captured stale
    # content. Fail closed to the raw binary rather than signing and
    # recording a marker over content that was never actually copied.
    presign_hash = _sha256(bundled_exe)
    if presign_hash != src_hash:
        log(
            f"Bundled copy of {qemu_bin} does not hash-match its source "
            "(source was likely replaced mid-copy by a rebuild). "
            "Falling back to the raw qemu-system binary this run.",
            "warn",
        )
        try:
            bundled_exe.unlink()
        except OSError:
            pass
        return qemu_bin
    icon_src = Path(__file__).resolve().parent.parent / "assets" / "qemu.icns"
    if icon_src.exists():
        (bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
        shutil.copy(icon_src, bundle / "Contents" / "Resources" / "qemu.icns")
    (bundle / "Contents").mkdir(parents=True, exist_ok=True)
    # QEMU looks for firmware/romfiles at `<binary>/../share/qemu/...`
    # (the source install layout: /opt/homebrew/Cellar/qemu-virgl/X/bin
    # has a sibling /opt/homebrew/Cellar/qemu-virgl/X/share/qemu/). When
    # the binary lives inside our bundle at Contents/MacOS/, that path
    # resolves to Contents/share/qemu/ which doesn't exist by default,
    # so QEMU fails with "failed to find romfile efi-virtio.rom" on
    # virtio-net-pci et al. Symlink the source's share/qemu into the
    # bundle to make the layout match.
    # Resolved on every rebuild (marker check above) so a QEMU upgrade
    # automatically re-links the new version's share/qemu.
    _link_qemu_datadir(qemu_bin, bundle)
    with (bundle / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump({
            "CFBundleName": "QEMU",
            "CFBundleDisplayName": "QEMU",
            "CFBundleExecutable": exe_name,
            "CFBundleIdentifier": f"org.qemu.{exe_name}",
            "CFBundleVersion": "1.0",
            "CFBundleShortVersionString": "1.0",
            "CFBundlePackageType": "APPL",
            "CFBundleIconFile": "qemu",
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "NSHighResolutionCapable": True,
        }, fh)
    # Ad-hoc sign with the HVF + DYLD entitlements so Apple's
    # Hypervisor framework works, TCC treats QEMU.app as a real app
    # (mic), AND DYLD_LIBRARY_PATH is honoured for libepoxy's
    # runtime dlopen of libEGL.dylib (cocoa,gl=es display init).
    # The plist lives beside the .app (NOT inside Contents/) because
    # macOS scans the bundle tree for code objects; a stray .plist
    # inside Contents/ is treated as a subcomponent and breaks validation.
    hv_entitlements.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n<dict>\n"
        "\t<key>com.apple.security.hypervisor</key>\n\t<true/>\n"
        "\t<key>com.apple.security.cs.allow-dyld-environment-variables</key>\n\t<true/>\n"
        "</dict>\n</plist>\n"
    )
    # Sign only the executable (NOT --deep on the whole app, which is
    # deprecated and re-signs shared resources). The launcher execs
    # this binary directly, so the entitlements belong on it.
    # If codesign fails (binary not on PATH, malformed entitlements, keychain
    # locked), the bundle would launch without HVF / DYLD entitlements --
    # the VM would silently fail to boot. Log loudly and return the raw
    # qemu-system path so the dock-icon cosmetic fails closed, not open.
    sign_result = subprocess.run(
        ["codesign", "--force",
         "--entitlements", str(hv_entitlements), "-s", "-", str(bundled_exe)],
        check=False, capture_output=True, text=True,
    )
    if sign_result.returncode != 0:
        log(
            f"codesign failed (rc={sign_result.returncode}) for {bundled_exe}: "
            f"{sign_result.stderr.strip()}. Falling back to raw qemu-system "
            "binary; HVF / DYLD entitlements will be absent.",
            "warn",
        )
        return qemu_bin
    # Only record the markers after a successful sign so a failed sign
    # triggers a rebuild next time. The presign marker records the
    # bundled exe's pre-sign hash plus its post-sign size+mtime so the
    # fast-path above can detect a later in-place rewrite of the exe
    # (codesign rewrote the on-disk bytes, so the hash alone can't be
    # re-verified against the exe directly).
    marker.write_text(signature)
    be_stat = bundled_exe.stat()
    presign_marker.write_text(
        f"{presign_hash}\n{be_stat.st_size}\n{be_stat.st_mtime_ns}\n"
    )
    return bundled_exe


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
    via_path = shutil.which(f"qemu-system-{guest_arch}")
    if via_path:
        qemu = Path(via_path)
    if qemu is None:
        raise RuntimeError(f"qemu-system-{guest_arch} not found")
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

    argv: list[str] = [str(_ensure_qemu_app(qemu))]
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
    # Both x86_64 and aarch64 use virtio-vga-gl as first choice — the only
    # device type that works with Venus per all reference configs
    # (tm23forest, peppergrayxyz gist, Collabora blog).  On aarch64 QEMU
    # builds that lack virtio-vga-gl, fall back to virtio-gpu-gl-pci
    # (the non-VGA PCI GL variant, which also supports venus=on).
    gl_dev = "virtio-vga-gl" if _qemu_supports(qemu, guest_arch, "device", "virtio-vga-gl") else "virtio-gpu-gl-pci"
    gpu_dev = gl_dev if _qemu_supports(qemu, guest_arch, "device", gl_dev) else "virtio-vga"
    if not _qemu_supports(qemu, guest_arch, "device", gpu_dev):
        gpu_dev = "virtio-gpu-pci"
    venus = (gpu_dev == gl_dev
             and _qemu_device_has_prop(qemu, guest_arch, gl_dev, "venus"))
    gpu_spec = f"{gpu_dev},xres=3456,yres=2234"
    if venus:
        # hostmem=4G: Venus needs sufficient shared memory for blob
        # resources.  512m was too small; reference configs use 4G.
        gpu_spec += ",venus=on,blob=on,hostmem=4G"
    display = "cocoa,gl=es" if gpu_dev == gl_dev else "cocoa"
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
