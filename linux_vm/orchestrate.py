"""Orchestration: CLI argument parsing, main(), and _build_one_vm()."""
from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import textwrap
from pathlib import Path
from typing import Optional, Callable

from .config import VMConfig, DISTRO_DEFAULTS, banner, filename_from_url
from .download import (
    DISTROS, download_to_cache, materialize_image, verify_hash,
)
from .host import (
    HostPlatform, detect_platform, detect_timezone, ensure_certifi,
    ensure_jinja2, ensure_pycdlib, find_qemu_img, guest_arch_for_host,
    install_qemu_img, is_supported_host_arch, physical_cpu_count,
    recommended_vcpus, host_memory_mb, recommended_memory_mb,
    reconfigure_stdout_utf8,
)
from .log import C, log
from . import qemu
from .templates import build_seed_iso, render_jinja2_template


def _find_or_install_qemu_img(host: HostPlatform):
    """Find qemu-img or install it via Homebrew."""
    qemu_img = find_qemu_img(host)
    if qemu_img is None:
        try:
            install_qemu_img(host)
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            log(f"Failed to install qemu-img automatically: {exc}", "err")
            return None
        qemu_img = find_qemu_img(host)
    return qemu_img


def _install_if_missing(installer_func: Callable[[], None], package_name: str) -> None:
    """Call an ensure_* installer, logging a failure to auto-install.

    The ensure_* functions themselves catch ImportError and drive the
    pip install, so there's no dead try/except-ImportError ladder here.
    """
    try:
        installer_func()
    except subprocess.CalledProcessError:
        log(f"Failed to install {package_name} automatically.", "err")


def _build_vm_config(args, _host: HostPlatform, defaults: dict, resolved) -> VMConfig:
    """Build VMConfig from parsed args and resolved image metadata."""
    # Default credentials are predictable (user password == username, root
    # password == "root") because the host SSH forward is loopback-only (M5),
    # so it is never reachable from the LAN. `--password` still overrides the
    # user password; both are recorded in the chmod-600 install-info.txt.
    username = args.username or defaults["username"]
    password = args.password or username
    cfg = VMConfig(
        vm_name=args.vm_name or "",
        hostname=args.hostname or defaults["hostname"],
        username=username,
        password=password,
        root_password="root",
        vcpus=args.vcpus,
        memory_mb=args.memory_mb,
        disk_gb=args.disk_gb,
        timezone=args.timezone or detect_timezone(),
        target_dir=args.target_dir.resolve(),
        ssh_port=args.ssh_port,
    )
    cfg.target_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.vm_name:
        cfg.vm_name = f"{resolved.name} GNOME"
    return cfg


def _extract_archive_safely(qcow2: Path, target_dir: Path) -> None:
    """Extract an archived cloud image, rejecting path-traversal members.

    Cloud images are fetched over the network and may reach extraction
    unverified when no checksum was available (see download.verify_hash),
    so a crafted archive must not be able to write outside target_dir
    (tar-slip / path-traversal CVE class). Applied to BOTH extraction
    paths -- the shelled-out system `tar -xJf` and the tarfile fallback.
    """
    target = target_dir.resolve()

    def _member_is_safe(name: str) -> bool:
        if not name or name.startswith("/"):
            return False
        parts = Path(name).parts
        if any(p == ".." for p in parts):
            return False
        try:
            (target / name).resolve().relative_to(target)
            return True
        except ValueError:
            return False

    try:
        listing = subprocess.check_output(
            ["tar", "-tJf", str(qcow2)], text=True,
        ).splitlines()
        unsafe = [n for n in listing if not _member_is_safe(n)]
        if unsafe:
            raise RuntimeError(
                "Archive contains path-traversal member(s), refusing to "
                f"extract: {unsafe[:3]}"
            )
        subprocess.check_call(
            ["tar", "-xJf", str(qcow2), "-C", str(target)]
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        with tarfile.open(qcow2, "r:xz") as tar:
            members = tar.getmembers()
            unsafe = [m.name for m in members if not _member_is_safe(m.name)]
            if unsafe:
                raise RuntimeError(
                    "Archive contains path-traversal member(s), refusing to "
                    f"extract: {unsafe[:3]}"
                ) from exc
            try:
                # py3.12+: 'data' filter blocks absolute paths, '..' and
                # symlink/hardlink escapes on top of the member check above.
                tar.extractall(target, members=members, filter="data")
            except TypeError:
                # pre-3.12: no filter kwarg -- members already validated.
                tar.extractall(target, members=members)


def _ensure_image(resolved, qcow2: Path, cache_dir: Path, distro: str) -> None:
    """Download/verify/materialise the cloud image for one VM.

    The shared cache path is always used: download_to_cache() verifies the
    hash once on the cached file. A hardlink into the target dir shares the
    inode so it needs no re-hash; only a real cross-volume copy is verified
    again. A stale target qcow2 is detected and re-downloaded.
    """
    if qcow2.exists():
        log(f"Reusing existing image: {qcow2}", "info")
        try:
            verify_hash(
                qcow2,
                alg=resolved.hash_alg,
                hex_digest=resolved.hash_hex,
                sums_url=resolved.hash_url,
            )
            return
        except RuntimeError as e:
            if "Checksum mismatch" not in str(e):
                raise
            log("Stale cached image detected -- SHA mismatch. Deleting and re-downloading.", "warn")
            qcow2.unlink()

    log(f"Downloading to shared cache for {distro}...", "step")
    cached = download_to_cache(
        resolved.image_url, cache_dir, distro,
        f"{resolved.name} cloud image",
        expected_hash_alg=resolved.hash_alg,
        expected_hash_hex=resolved.hash_hex,
        expected_hash_url=resolved.hash_url,
    )
    materialize_image(cached, qcow2)
    if not qcow2.samefile(cached):
        verify_hash(
            qcow2,
            alg=resolved.hash_alg,
            hex_digest=resolved.hash_hex,
            sums_url=resolved.hash_url,
        )


def parse_args(default_target: Path) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Automated Linux + GNOME VM setup on QEMU",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--distro",
        choices=sorted(DISTROS.keys()),
        default="ubuntu-lts",
        help="Guest distro to install",
    )
    p.add_argument(
        "--target-dir",
        type=Path,
        default=default_target,
        help="Directory to create the VM in",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Shared cloud-image cache directory. Default: ~/VMs/cache. "
            "Downloaded once per distro across the whole fleet."
        ),
    )
    p.add_argument("--vm-name", default=None, help="Display name (default: distro-derived)")
    p.add_argument("--hostname", default=None, help="Guest hostname (default: distro-derived)")
    p.add_argument("--username", default=None, help="Primary user (default: distro-derived)")
    p.add_argument(
        "--password",
        default=None,
        help="Override the guest user password (default: password == username)",
    )
    p.add_argument(
        "--vcpus",
        type=int,
        default=None,
        help="Virtual CPUs for the VM (default: half of the host's physical "
             "cores, clamped to 2-8; computed once at startup)",
    )
    p.add_argument(
        "--memory-mb",
        type=int,
        default=None,
        help="RAM for the VM in MB (default: half of the host's physical "
             "memory, clamped to 8-32 GB; computed once at startup)",
    )
    p.add_argument("--disk-gb", type=int, default=80)
    p.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone (auto-detected from host if omitted)",
    )
    p.add_argument(
        "--ssh-port",
        type=int,
        default=None,
        help=(
            "Host port that forwards to guest port 22 (SSH). "
            "By default the script auto-detects a free port starting from 2222."
        ),
    )
    p.add_argument(
        "--start", action="store_true",
        help="Start the VM after building (default: build only, no launch)",
    )
    p.add_argument(
        "--keep-qcow2",
        action="store_true",
        help="Keep the intermediate qcow2 download (otherwise deleted after conversion)",
    )
    p.add_argument(
        "--prefetch",
        action="store_true",
        help=(
            "Download the cloud image to the shared cache and EXIT "
            "(no VM is built)."
        ),
    )
    p.add_argument(
        "--simulate",
        action="store_true",
        help=(
            "Render the cloud-init user-data in simulate-mode "
            "(simulate_only=True): the heavy install is skipped and a "
            "single per-distro --simulate / --dry-run / -p package-"
            "resolver check runs instead."
        ),
    )
    p.add_argument(
        "--gnome-ext-url",
        default=None,
        help="Internal: URL of the in-progress GNOME extension server (set by fleet orchestrator)",
    )
    return p.parse_args()


def default_target_dir(_host: HostPlatform, distro: str = "ubuntu-lts") -> Path:
    """Return the default target directory for the given distro."""
    folder = distro
    return Path.home() / "VMs" / folder


def default_cache_dir(_host: HostPlatform) -> Path:
    """Return the default cache directory path."""
    return Path.home() / "VMs" / "cache"


def main() -> int:
    reconfigure_stdout_utf8()
    host = detect_platform()
    placeholder = default_target_dir(host, "ubuntu-lts")
    args = parse_args(placeholder)

    # If --target-dir was not explicitly provided, derive it from the actual
    # distro (the placeholder above always defaults to ubuntu-lts).
    if args.target_dir.resolve() == placeholder:
        args.target_dir = default_target_dir(host, args.distro)

    banner(args.distro.capitalize())
    log(f"Host:   {host.system} ({host.arch})", "info")

    if not host.is_macos:
        log("This script supports macOS hosts only.", "err")
        return 2
    if not is_supported_host_arch(host.arch):
        log("This script supports x86_64 and arm64 (Apple Silicon) hosts only.", "err")
        return 2
    guest_arch = guest_arch_for_host(host.arch)
    log(f"Guest architecture: {guest_arch}", "info")

    host_cores = physical_cpu_count()
    vcpu_default = recommended_vcpus(host_cores)
    log(
        f"vCPU default: {vcpu_default} vCPU "
        f"(half of {host_cores or 'unknown'} host physical cores, clamped 2-8; "
        f"override with --vcpus)",
        "info",
    )
    # Resolve the vCPU default once here so it's a single source of truth.
    if args.vcpus is None:
        args.vcpus = vcpu_default

    host_mem = host_memory_mb()
    mem_default = recommended_memory_mb(host_mem)
    mem_gb = mem_default / 1024
    host_gb = f"{host_mem / 1024:.0f}" if host_mem else "unknown"
    log(
        f"Memory default: {mem_default} MB ({mem_gb:.0f} GB) "
        f"(half of {host_gb} GB host memory, clamped 8-32 GB; "
        f"override with --memory-mb)",
        "info",
    )
    if args.memory_mb is None:
        args.memory_mb = mem_default

    return _build_one_vm(args, host)


def _build_one_vm(args, host) -> int:
    """Build (and optionally launch) ONE VM.

    Returns 0 on success, non-zero on any per-VM failure.
    """
    distro = DISTROS[args.distro]

    # ---- Prerequisites: QEMU tools (qemu-system + OVMF) ----
    log("Locating QEMU tools ...", "step")
    try:
        tools = qemu.detect_tools(host)
    except RuntimeError:
        log(qemu.install_hint(host), "err")
        return 3
    log(f"qemu-system:     {tools.qemu_system}", "ok")
    if tools.ovmf_code:
        log(f"ovmf_code:       {tools.ovmf_code}", "ok")
    if tools.ovmf_vars:
        log(f"ovmf_vars:       {tools.ovmf_vars}", "ok")

    # ---- Prerequisites: qemu-img ----------------------------------------
    qemu_img = _find_or_install_qemu_img(host)
    if qemu_img is None:
        log("qemu-img still not found after install attempt.", "err")
        return 4
    log(f"qemu-img:        {qemu_img}", "ok")

    # ---- Prerequisites: pycdlib + certifi -------------------------------
    _install_if_missing(ensure_pycdlib, "pycdlib")
    _install_if_missing(ensure_certifi, "certifi")
    _install_if_missing(ensure_jinja2, "Jinja2")

    # ---- Resolve cloud image --------------------------------------------
    guest_arch = guest_arch_for_host(host.arch)
    log(f"Discovering latest {args.distro.capitalize()} {guest_arch} image ...", "step")
    resolved = distro.resolve(guest_arch)
    log(f"Resolved: {resolved.name}", "ok")
    log(f"Image URL: {resolved.image_url}", "info")

    # ---- Build VMConfig --------------------------------------------------
    defaults = DISTRO_DEFAULTS[args.distro]
    cfg = _build_vm_config(args, host, defaults, resolved)
    log(f"Target directory: {cfg.target_dir}", "info")
    log(f"Timezone:         {cfg.timezone}", "info")
    log(f"User:             {cfg.username}@{cfg.hostname}", "info")
    log(f"Guest architecture: {guest_arch}", "info")

    # ---- Download cloud image -------------------------------------------
    qcow2 = cfg.target_dir / filename_from_url(resolved.image_url)
    cache_dir = args.cache_dir if args.cache_dir is not None else default_cache_dir(host)

    _ensure_image(resolved, qcow2, cache_dir, args.distro)

    # ---- Prefetch short-circuit ------------------------------------------
    if args.prefetch:
        log(f"--prefetch: cached image at {qcow2}; exiting without building VM.", "ok")
        return 0

    # ---- Compute per-VM artifact paths -----------------------------------
    disk_path = cfg.target_dir / qemu.DISK_FILENAME
    def_path = cfg.target_dir / qemu.LAUNCHER_FILENAME

    # ---- Extract archived images (.tar.xz containing disk.raw) ----------
    if qcow2.suffix.lower() == ".xz" or qcow2.name.lower().endswith(".tar.xz"):
        log(f"Extracting {qcow2.name} ...", "step")
        _extract_archive_safely(qcow2, cfg.target_dir)
        candidates = sorted(
            list(cfg.target_dir.glob("disk.raw"))
            + list(cfg.target_dir.glob("*.raw"))
            + list(cfg.target_dir.glob("*.qcow2")),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        candidates = [c for c in candidates if c != qcow2 and c != disk_path]
        if not candidates:
            log("Archive extraction succeeded but no disk image found inside.", "err")
            return 6
        if not args.keep_qcow2:
            try:
                qcow2.unlink()
            except OSError:
                pass
        qcow2 = candidates[0]
        log(f"Extracted disk image: {qcow2.name} ({qcow2.stat().st_size / 1e6:.1f} MB)", "ok")

    # ---- Prepare disk (qcow2 convert + resize) ---------------------------
    qemu.prepare_disk(qcow2, disk_path, cfg.disk_gb, qemu_img)
    if not args.keep_qcow2:
        try:
            qcow2.unlink()
            log("Removed intermediate cloud image.", "info")
        except OSError:
            pass

    # ---- Render templates -----------------------------------------------
    repo_root = Path(__file__).resolve().parent.parent
    templates_dir = repo_root / "templates"
    user_data_tmpl = templates_dir / distro.user_data_template
    meta_data_tmpl = templates_dir / "meta-data.j2"
    for t in (user_data_tmpl, meta_data_tmpl):
        if not t.exists():
            log(f"Missing template: {t}", "err")
            return 5

    # ---- Generate per-VM ephemeral SSH keypair --------------------------
    ssh_key_path = cfg.target_dir / "ssh_key"
    ssh_pub_path = cfg.target_dir / "ssh_key.pub"
    ssh_pub_text: Optional[str] = None
    if not ssh_key_path.exists() or not ssh_pub_path.exists():
        log("Generating per-VM SSH keypair (ed25519) ...", "step")
        try:
            subprocess.check_call(
                ["ssh-keygen", "-t", "ed25519",
                 "-f", str(ssh_key_path),
                 "-N", "",
                 "-C", f"setup_vm.py monitor key for {cfg.vm_name}",
                 "-q"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log(f"ssh-keygen unavailable ({e}); skipping monitor SSH key. "
                "monitor --tail will be limited to console.log scraping.",
                "warn")
    if ssh_pub_path.exists():
        ssh_pub_text = ssh_pub_path.read_text(encoding="utf-8").strip()
        try:
            os.chmod(ssh_key_path, 0o600)
        except OSError:
            pass

    user_data = render_jinja2_template(
        templates_dir,
        distro.user_data_template,
        {
            "HOSTNAME": cfg.hostname,
            "USERNAME": cfg.username,
            "PASSWORD": cfg.password,
            "ROOT_PASSWORD": cfg.root_password,
            "TIMEZONE": cfg.timezone,
            "SSH_PUBLIC_KEY": ssh_pub_text,
            "simulate_only": args.simulate,
            "marker_name": args.distro,
            "GUEST_ARCH": guest_arch,
            "GNOME_EXT_URL": args.gnome_ext_url or "",
        },
    )
    meta_data = render_jinja2_template(
        templates_dir,
        "meta-data.j2",
        {"INSTANCE_ID": cfg.instance_id, "HOSTNAME": cfg.hostname},
    )

    # ---- Build seed ISO --------------------------------------------------
    build_seed_iso(cfg.seed_path, user_data, meta_data, templates_dir)

    # ---- Generate VM definition (launcher script) ----
    firmware = "efi"
    log("Generating QEMU launcher script ...", "step")
    definition_text = qemu.render_launcher(cfg, firmware, tools)
    def_path.parent.mkdir(parents=True, exist_ok=True)
    def_path.write_text(definition_text, encoding="utf-8")
    log(f"Definition: {def_path}", "ok")

    # ---- Persist credentials snapshot -----------------------------------
    info_path = cfg.target_dir / "install-info.txt"
    info_path.write_text(
        textwrap.dedent(
            f"""\
            {resolved.name} + GNOME VM -- install info
            ===========================================
            Distro:     {resolved.name}
            Image URL:  {resolved.image_url}
            VM name:    {cfg.vm_name}
            Hostname:   {cfg.hostname}
            Username:   {cfg.username}
            Password:   {cfg.password}
            Root password: {cfg.root_password}  (set by cloud-init for console emergency access)
            Definition: {def_path}
            Disk:       {disk_path}
            Seed ISO:   {cfg.seed_path}
            Timezone:   {cfg.timezone}
            Resources:  {cfg.vcpus} vCPU, {cfg.memory_mb} MB RAM, {cfg.disk_gb} GB disk
            Instance:   {cfg.instance_id}

            Once booted, the console login banner shows the VM's IP and the
            ssh command. Inside the VM, run `check-vm` for a one-shot
            diagnostic of cloud-init status, GDM, guest-agent tools, and any
            recent errors. See README "First boot -- what to expect".

            Keep this file safe -- it is the only place your generated password is stored.
            """
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(info_path, 0o600)
    except OSError:
        pass

    # ---- Start VM --------------------------------------------------------
    if args.start:
        if args.simulate:
            log(
                "--simulate: launching VM in simulate mode "
                "(dry-run package resolver will run via cloud-init).",
                "info",
            )
        qemu.launch(def_path)

    # ---- Summary ---------------------------------------------------------
    print()
    print(f"{C.BOLD}{C.OK}Setup complete.{C.RESET}")
    print(f"{C.BOLD}Distro:    {C.RESET} {resolved.name}")
    print(f"{C.BOLD}Username:  {C.RESET} {cfg.username}")
    print(f"{C.BOLD}Password:  {C.RESET} {cfg.password}")
    print(f"{C.BOLD}Root pw:   {C.RESET} {cfg.root_password}")
    print(f"{C.BOLD}Definition:{C.RESET} {def_path}")
    print(f"{C.BOLD}Info:      {C.RESET} {info_path}")
    ga = "spice-vdagent"

    if args.distro == "ubuntu-lts":
        steps = (
            "  1. apt update + apt upgrade the Ubuntu LTS base system\n"
            f"  2. Install Ubuntu LTS desktop (ubuntu-desktop-minimal), "
            f"GNOME display manager, {ga}, rclone\n"
            "  3. Set graphical.target as default and enable GNOME display manager\n"
            "  4. Reboot into GDM (Wayland session)\n"
        )
    else:  # gentoo
        steps = (
            "  1. Profile sync + eselect repository setup (binhost enabled)\n"
            f"  2. emerge --getbinpkg gnome-base/gnome + individual GNOME apps + {ga}\n"
            "  3. Set graphical.target as default and enable GDM\n"
            "  4. Reboot into GDM (Wayland session)"
        )
    print(
        textwrap.dedent(
            f"""
            cloud-init will now:
            {steps}

            First boot takes ~5-27 min on an idle host for Ubuntu LTS
            (slower under CPU contention or a slow network); Gentoo ~53-208 min
            (binary packages + four forced arm64 source builds; ~53 min warm
            binhost, ~208 min when they land cold). When the
            GDM login screen appears, sign in as {C.BOLD}{cfg.username}{C.RESET}
            with the password above.
            """
        ).rstrip()
    )
    return 0
