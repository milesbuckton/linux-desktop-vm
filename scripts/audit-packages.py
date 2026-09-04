#!/usr/bin/env python3
"""Cross-distro package alignment audit.

Renders each distro template, extracts the package list (from both
cloud-init `packages:` block AND from apt/dnf commands embedded
in runcmd), categorises each package by likely intent, and prints a
side-by-side table so it's obvious where the templates disagree on what
to install for a given functional area.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from linux_vm.config import DISTROS, DISTRO_TEMPLATE
from linux_vm.templates import get_jinja_env, parse_cloud_config

TEMPLATES = REPO / "templates"


def render_context(distro: str, guest_arch: str = "x86_64") -> dict:
    return {
        "USERNAME": "testuser",
        "HOSTNAME": f"{distro}-vm",
        "VM_NAME": f"{distro} GNOME",
        "DISPLAY_NAME": distro,
        "PROVIDER": "qemu",
        "PASSWORD": "x",
        "ROOT_PASSWORD": "root",
        "SSH_PUBLIC_KEY": "ssh-ed25519 AAAA test@lint",
        "TIMEZONE": "Africa/Johannesburg",
        "INSTANCE_ID": f"{distro}-vm-lintsim",
        "DISTRO_ID": distro,
        "GUEST_ARCH": guest_arch,
        "marker_name": distro,
        "display_name": distro,
        "simulate_only": False,
    }


def extract_packages(distro: str) -> set[str]:
    env = get_jinja_env(TEMPLATES)
    tmpl = env.get_template(DISTRO_TEMPLATE[distro])

    # Gentoo installs via `emerge` from a systemd service, NOT the cloud-init
    # `packages:` block, so its install list lives in the simulate dry-run
    # file `/etc/install-simulate-pkgs.txt` (auto-derived from `_gentoo_pkgs`).
    # Render in simulate mode and read that file so Gentoo shows up in the
    # parity table instead of blank.
    if distro == "gentoo":
        ctx = render_context(distro)
        ctx["simulate_only"] = True
        rendered = tmpl.render(**ctx)
        loaded = parse_cloud_config(rendered) or {}
        pkgs: set[str] = set()
        for wf in loaded.get("write_files") or []:
            if isinstance(wf, dict) and wf.get("path") == "/etc/install-simulate-pkgs.txt":
                for line in (wf.get("content") or "").splitlines():
                    tok = line.strip()
                    if tok:
                        pkgs.add(tok)
                break
        return pkgs

    rendered = tmpl.render(**render_context(distro))
    loaded = parse_cloud_config(rendered) or {}

    pkgs: set[str] = set()
    for p in loaded.get("packages") or []:
        if isinstance(p, str):
            pkgs.add(p.strip("'\""))
        elif isinstance(p, list) and len(p) >= 2:
            pkgs.add(str(p[1]).strip("'\""))

    rc_text = "\n".join(str(x) for x in (loaded.get("runcmd") or []))
    install_cmd_re = re.compile(
        r"(?:apt-get install|apt install|emerge)\s+"
        r"(?:-y\s+|--noconfirm\s+--needed\s+|--getbinpkg\s+|--getbinpkgonly\s+|--verbose\s+)*"
        r"([A-Za-z0-9_.@+:/>&'\" -][\w\s.@+:/>&'\" -]*?)"
        r"(?:\s*\|\||\s*&&|\s*\|\s|\s*;|\s*\"|\s*\\n|$)"
    )
    for match in install_cmd_re.finditer(rc_text):
        tokens = match.group(1).split()
        for tok in tokens:
            # Strip YAML list brackets and trailing quotes that leak in
            # from the [sh, -c, "dnf install -y ..."] format.
            tok = tok.strip("]\"'")
            if tok in ("-y", "-n", "--noconfirm", "--needed", "-t", "pattern", "install"):
                continue
            if "=" in tok or tok.startswith("-") or tok.startswith("/"):
                continue
            if re.match(r"^[@A-Za-z][\w.@+:/-]*$", tok):
                pkgs.add(tok)

    return pkgs


CATEGORIES = {
    # Each category lists every distro's package atom that provides the
    # capability. Ubuntu uses apt names; Gentoo uses portage category/name atoms.
    # Categories that are USE-flag-gated on Gentoo (e.g. the gnome-software
    # Flatpak plugin) or simply not packaged (Snapshot) have no Gentoo atom and
    # will show "-".
    "GNOME Shell": ["gnome-shell", "gnome-base/gnome-shell"],
    "Display Manager": ["gdm", "gdm3", "gnome-extra/gdm"],
    "Settings": ["gnome-control-center", "gnome-extra/gnome-control-center"],
    "File manager": ["nautilus", "gnome-extra/nautilus"],
    "Software (GNOME)": ["gnome-software", "gnome-extra/gnome-software"],
    "Software Flatpak plugin": ["gnome-software-plugin-flatpak"],
    "Console terminal": ["gnome-console", "gnome-terminal", "gui-apps/gnome-console"],
    "Text editor": ["gnome-text-editor", "gui-apps/gnome-text-editor", "app-editors/gnome-text-editor"],
    "Tweaks": ["gnome-tweaks", "gnome-extra/gnome-tweaks"],
    "Extensions UI": ["gnome-extensions", "gnome-extensions-app", "gnome-shell-extensions",
                      "gnome-extra/gnome-shell-extensions"],
    "Weather": ["gnome-weather", "gnome-extra/gnome-weather"],
    "Calendar": ["gnome-calendar", "gnome-extra/gnome-calendar"],
    "Help": ["yelp", "gnome-extra/yelp"],
    "Sound recorder": ["gnome-sound-recorder", "vocalis", "media-sound/gnome-sound-recorder"],
    "Bluez stack": ["bluez", "net-wireless/bluez"],
    "Bluetooth panel": ["gnome-bluetooth", "gnome-bluetooth-3.0", "gnome-extra/gnome-bluetooth",
                        "net-wireless/gnome-bluetooth"],
    "USB tools": ["usbutils", "sys-apps/usbutils"],
    "Smartcard": ["pcsc-lite", "pcscd", "pcsclite", "sys-apps/pcsc-lite"],
    "Flatpak": ["flatpak", "sys-apps/flatpak"],
    "PipeWire": ["pipewire", "media-video/pipewire"],
    "PipeWire pulse": ["pipewire-pulse", "pipewire-pulseaudio", "media-sound/pipewire-pulse"],
    "PipeWire alsa": ["pipewire-alsa", "media-sound/pipewire-alsa"],
    "PipeWire jack": ["pipewire-jack", "pipewire-jack-audio-connection-kit", "media-libs/pipewire-jack"],
    "WirePlumber": ["wireplumber", "media-video/wireplumber"],
    "RealtimeKit": ["rtkit", "sys-auth/rtkit"],
    "xdg-desktop-portal-gnome": ["xdg-desktop-portal-gnome", "sys-apps/xdg-desktop-portal-gnome"],
    "Mesa Vulkan": ["mesa-vulkan-drivers", "vulkan-loader", "libvulkan1", "media-libs/vulkan-loader"],
    "Vulkan tools": ["vulkan-tools", "dev-util/vulkan-tools"],
    "Mesa demo (glxgears)": ["mesa-utils", "Mesa-demo-x", "glx-utils", "mesa-demos", "x11-apps/mesa-progs"],
    "Epiphany": ["epiphany", "epiphany-browser", "www-client/epiphany"],
    "Video player": ["totem", "media-video/totem"],
    "Image viewer (loupe)": ["loupe", "gui-apps/loupe", "media-gfx/loupe"],
    "Snapshot (camera)": ["snapshot", "gnome-snapshot"],
    "Document viewer (Papers/Evince)": ["papers", "evince", "app-text/evince", "app-text/papers"],
    "Mail (geary)": ["geary", "mail-client/geary"],
    "Git": ["git", "dev-vcs/git"],
    "Nano editor": ["nano", "app-editors/nano"],
    "Python pip": ["python3-pip", "python3-venv", "python313-pip", "dev-python/pip"],
    "lsb_release": ["lsb-release", "lsb_release", "sys-apps/lsb-release"],
    "fastfetch": ["fastfetch", "app-misc/fastfetch"],
    "fonts (Fira Code)": ["fonts-firacode", "fira-code-fonts", "media-fonts/fira-code"],
    "net-tools": ["net-tools", "net-misc/net-tools", "sys-apps/net-tools"],
    "rclone": ["rclone", "net-misc/rclone"],
    "GVfs MTP/PTP": ["gvfs-backends", "gvfs-mtp", "gvfs-gphoto2",
                     "gvfs-backend-mtp", "gvfs-backend-gphoto2", "gnome-extra/gvfs",
                     "net-libs/gvfs", "gnome-base/gvfs"],
    "Google Chrome": ["google-chrome-stable", "www-client/google-chrome"],
    "NetworkManager": ["networkmanager", "net-misc/networkmanager"],
    "D-Bus": ["dbus", "sys-apps/dbus"],
    "GNOME keyring": ["gnome-keyring", "app-crypt/gnome-keyring"],
    "GNOME Online Accounts": ["gnome-online-accounts", "gnome-extra/gnome-online-accounts", "net-libs/gnome-online-accounts"],
    "GNOME Backgrounds": ["gnome-backgrounds", "gnome-extra/gnome-backgrounds", "x11-themes/gnome-backgrounds"],
    "Icon theme (Adwaita)": ["adwaita-icon-theme", "gnome-icon-theme", "x11-themes/adwaita-icon-theme"],
    "GTK themes (standard)": ["gnome-themes-extra", "gnome-themes-standard", "x11-themes/gnome-themes-standard"],
    "ubuntu-desktop-minimal": ["ubuntu-desktop-minimal"],
    "GNOME meta (gentoo)": ["gnome-base/gnome"],
}


def main() -> None:
    pkgs_by_distro = {}
    for d in DISTROS:
        pkgs_by_distro[d] = extract_packages(d)

    colw = 26
    short = {"gentoo": "gentoo", "ubuntu-lts": "ubuntu-lts"}
    header = "Category".ljust(34) + " | " + " | ".join(short[d].ljust(colw) for d in DISTROS)
    print(header)
    print("-" * len(header))
    for category, candidates in CATEGORIES.items():
        cells = []
        for d in DISTROS:
            found = next((c for c in candidates if c in pkgs_by_distro[d]), None)
            cells.append((found or "-")[:colw].ljust(colw))
        print(category.ljust(34) + " | " + " | ".join(cells))

    all_absent = [c for c in CATEGORIES
                  if not any(set(CATEGORIES[c]) & pkgs_by_distro[d]
                             for d in DISTROS)]
    if all_absent:
        print()
        print("Categories absent on ALL distros (provided by the desktop")
        print("meta-package, or deliberately omitted):")
        print("  " + ", ".join(all_absent))

    print()
    print("=== uncategorised packages per distro (probably distro-specific) ===")
    all_known = {item for sublist in CATEGORIES.values() for item in sublist}
    for d in DISTROS:
        extras = sorted(pkgs_by_distro[d] - all_known)
        print(f"\n--- {d} ({len(extras)}) ---")
        for p in extras:
            print(f"  {p}")


if __name__ == "__main__":
    main()
