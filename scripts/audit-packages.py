#!/usr/bin/env python3
"""Cross-distro package alignment audit (fleet Gate 0b).

Renders each distro template, extracts the package list (from both
cloud-init `packages:` block AND from apt/emerge commands embedded
in runcmd), categorises each package by likely intent, and prints a
side-by-side table so it's obvious where the templates disagree on what
to install for a given functional area.

Then applies the REQUIRED verdict (see REQUIRED below): every
(category, distro) pair listed there must be present in the extracted
package set, otherwise the script exits 1. Exit 0 = AUDIT-OK. This is
what makes the audit runnable as a hard pre-flight gate in
linux_vm/fleet/main.py (skipped with --no-audit).
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from linux_vm.config import DISTROS, DISTRO_TEMPLATE
from linux_vm.audit import gentoo_atoms_from_text, shell_script
from linux_vm.templates import get_jinja_env, parse_cloud_config
from linux_vm.test_context import render_context

TEMPLATES = REPO / "templates"


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
        # ...plus the atoms the REAL gentoo-install.sh emerges. The simulate
        # file is a dry-run projection of _gentoo_pkgs, but gentoo-install.sh
        # is what actually installs the system, so judging only the
        # projection lets a category "pass" for Gentoo on the strength of a
        # list the real build never installs. This merge used to be dead code:
        # the block sat after the `return pkgs` above, guarded by the same
        # `if distro == "gentoo"`, so it could never run.
        real = parse_cloud_config(tmpl.render(**render_context(distro))) or {}
        for wf in real.get("write_files") or []:
            if isinstance(wf, dict) and str(wf.get("path", "")).endswith("gentoo-install.sh"):
                pkgs.update(gentoo_atoms_from_text(str(wf.get("content") or "")))
        return pkgs

    rendered = tmpl.render(**render_context(distro))
    loaded = parse_cloud_config(rendered) or {}

    pkgs: set[str] = set()
    for p in loaded.get("packages") or []:
        if isinstance(p, str):
            pkgs.add(p.strip("'\""))
        elif isinstance(p, list) and len(p) >= 2:
            pkgs.add(str(p[1]).strip("'\""))

    def raw_cmd(item):
        # Shared with scripts/lint-templates.py's syntax check, so a package
        # list spelled `["bash", "-lc", ...]` cannot hide from the audit while
        # still being executed by the build. This used to unwrap only
        # `["sh", "-c", ...]`, which silently lost the mesa-fork build block
        # (templates/ubuntu.j2) -- a real package list the audit never saw.
        script = shell_script(item)
        return script if script is not None else str(item)

    rc_text = "\n".join(raw_cmd(x) for x in (loaded.get("runcmd") or []))
    # Split on shell control operators, not on a fixed terminator set. The
    # body of a package list is whitespace-separated tokens with no `;`, `&`
    # or `|` in them, so stopping at the first one is both simpler and more
    # permissive than matching a specific tail like `|| true;` -- which is
    # what used to silently drop the mesa-fork build block
    # (templates/ubuntu.j2: `apt-get install -y ... rustc cargo >>$MESA_LOG
    # 2>&1 || true`), because `$` is not a valid package character and the
    # old trailing-anchor shape could never match a redirect.
    install_cmd_re = re.compile(
        r"(?:apt-get install|apt install|emerge)\s+"
        r"(?:-y\s+|--noconfirm\s+--needed\s+|--getbinpkg\s+|--getbinpkgonly\s+|--verbose\s+)*"
        r"([^;&|\n]+)"
    )
    pkg_token_re = re.compile(r"^[A-Za-z0-9][\w.@+:./-]*$")
    for match in install_cmd_re.finditer(rc_text):
        for tok in match.group(1).split():
            # Strip YAML list brackets and quotes that leak in from the
            # [sh, -c, "apt-get install ..."] entry shape (a non-shell entry
            # is currently stringified by raw_cmd, so `['python', ...]` can
            # appear). `2>&1`, `>>$LOG` and `||` are dropped by the pattern
            # below because of the `>`/`|`/`$` characters.
            tok = tok.strip("]\"'")
            if pkg_token_re.match(tok):
                pkgs.add(tok)

    return pkgs


CATEGORIES = {
    # Each category lists every distro's package atom that provides the
    # capability. Ubuntu uses apt names; Gentoo uses portage category/name atoms.
    # Categories that are USE-flag-gated on Gentoo (e.g. the gnome-software
    # Flatpak plugin) or simply not packaged (Snapshot) have no Gentoo atom and
    # will show "-".
    "GNOME Shell": ["gnome-shell", "gnome-base/gnome-shell"],
    "Display Manager": ["gdm", "gdm3", "gnome-extra/gdm", "gnome-base/gdm"],
    "Settings": ["gnome-control-center", "gnome-extra/gnome-control-center",
                 "gnome-base/gnome-control-center"],
    "File manager": ["nautilus", "gnome-extra/nautilus", "gnome-base/nautilus"],
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
    "snapd": ["snapd", "sys-apps/snapd", "app-containers/snapd"],
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
    "Firefox": ["firefox", "www-client/firefox"],
    "Video player": ["totem", "media-video/totem"],
    "Image viewer (loupe)": ["loupe", "gui-apps/loupe", "media-gfx/loupe"],
    "Snapshot (camera)": ["snapshot", "gnome-snapshot"],
    "Document viewer (Papers/Evince)": ["papers", "evince", "app-text/evince", "app-text/papers"],
    "Mail (geary)": ["geary", "mail-client/geary"],
    "Git": ["git", "dev-vcs/git"],
    "Nano editor": ["nano", "app-editors/nano"],
    "Python pip": ["python3-pip", "python3-venv", "python313-pip",
                   "python3.14-venv", "dev-python/pip"],
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
    # Same-app pairs where both distros install the app explicitly (added so
    # the Gate 0b verdict can cover them; previously they sat uncategorised).
    "Calculator": ["gnome-calculator", "gnome-extra/gnome-calculator"],
    "Characters": ["gnome-characters", "gnome-extra/gnome-characters"],
    "Disk utility": ["gnome-disk-utility", "sys-apps/gnome-disk-utility"],
    "Contacts": ["gnome-contacts", "gnome-extra/gnome-contacts"],
    "System monitor": ["gnome-system-monitor", "gnome-extra/gnome-system-monitor"],
    "Screenshot (legacy)": ["gnome-screenshot", "media-gfx/gnome-screenshot"],
    "Plymouth": ["plymouth", "sys-boot/plymouth"],
    "Sound theme (freedesktop)": ["sound-theme-freedesktop",
                                  "x11-themes/sound-theme-freedesktop"],
    "Distro branding": ["ubuntu-wallpapers", "x11-themes/gnome-backgrounds"],
}

# ---------------------------------------------------------------------------
# Gate 0b verdict: required category x distro expectations.
#
# This is what turns the audit from a printout into a hard gate. Each
# category listed for a distro must be EXPLICITLY present in that distro's
# extracted package set (cloud-init `packages:` + install commands; for
# gentoo the simulate target list). Seeded from README's "Apps
# preinstalled on every VM" contract and each template's current explicit
# list, so a healthy fleet passes every pair -- any future removal of one
# of these from either template fails the gate in ~10 sec, hours before
# the verify-block would notice on a real build.
#
# Categories are deliberately ABSENT from a distro's entry when the app is:
#   * ubuntu-desktop-minimal-provided on Ubuntu (gnome-shell, Settings,
#     File manager, gnome-software, gnome-console, totem, geary,
#     gnome-weather, gnome-keyring, ...) -- the meta itself
#     ("ubuntu-desktop-minimal") is required instead and anchors all of
#     them;
#   * gnome-base/gnome-meta-provided on Gentoo (gnome-shell,
#     gnome-keyring) -- required via "GNOME meta (gentoo)" instead;
#   * best-effort non-package installs (Flatseal, Gear Lever, PowerShell
#     tarball, Snapshot-on-Gentoo via Flathub) -- untracked by design;
#   * USE-flag-gated builds (gnome-software-plugin-flatpak);
#   * pipewire-pulse/alsa/jack on Gentoo, where the pulse/alsa/jack
#     compat comes from media-video/pipewire USE flags, not packages.
#
# When adding an app to both templates, add its category here too.
REQUIRED: dict[str, list[str]] = {
    "ubuntu-lts": [
        # desktop foundation / curated set (explicit in ubuntu.j2)
        "ubuntu-desktop-minimal", "Display Manager", "Text editor",
        "Tweaks", "Extensions UI", "Weather", "Calendar", "Help",
        "Bluez stack",
        "USB tools", "Flatpak", "snapd", "PipeWire", "PipeWire pulse",
        "PipeWire alsa", "PipeWire jack", "WirePlumber", "RealtimeKit",
        "xdg-desktop-portal-gnome", "Mesa Vulkan", "Vulkan tools",
        "Mesa demo (glxgears)", "Epiphany", "Firefox",
        "Video player",
        "Image viewer (loupe)", "Snapshot (camera)",
        "Document viewer (Papers/Evince)", "Mail (geary)", "Git",
        "Nano editor",
        "Python pip", "fastfetch", "fonts (Fira Code)", "net-tools",
        "rclone",
        "GVfs MTP/PTP", "Google Chrome", "GTK themes (standard)",
        "Calculator", "Characters", "Disk utility", "Plymouth",
        "Sound theme (freedesktop)", "Distro branding",
    ],
    "gentoo": [
        # fully explicit via gentoo.j2 _gentoo_pkgs / gentoo-install.sh
        "GNOME meta (gentoo)", "Display Manager", "Settings",
        "File manager", "Software (GNOME)", "Console terminal",
        "Text editor", "Tweaks", "Extensions UI", "Weather",
        "Calendar", "Help", "Sound recorder", "Bluez stack",
        "Bluetooth panel", "USB tools", "Smartcard", "Flatpak", "snapd",
        "PipeWire", "WirePlumber", "RealtimeKit",
        "xdg-desktop-portal-gnome", "Mesa Vulkan", "Vulkan tools",
        "Mesa demo (glxgears)", "Epiphany", "Firefox", "Video player",
        "Image viewer (loupe)", "Document viewer (Papers/Evince)",
        "Mail (geary)", "Git", "Nano editor", "Python pip",
        "lsb_release", "fastfetch", "fonts (Fira Code)", "net-tools",
        "rclone", "GVfs MTP/PTP", "Google Chrome", "NetworkManager",
        "GNOME Online Accounts", "GNOME Backgrounds",
        "Icon theme (Adwaita)", "GTK themes (standard)", "Calculator",
        "Characters", "Disk utility", "Contacts", "System monitor",
        "Screenshot (legacy)", "Plymouth", "Sound theme (freedesktop)",
        "Distro branding",
    ],
}


def check_required(pkgs_by_distro: dict) -> list[str]:
    """Return human-readable failures for missing required categories.

    Validates the REQUIRED table itself too: unknown categories and
    distros missing from REQUIRED are failures, so a typo or a newly
    added distro can't silently weaken the gate.
    """
    failures: list[str] = []
    for distro in sorted(set(REQUIRED) | set(pkgs_by_distro)):
        if distro not in REQUIRED:
            failures.append(
                f"{distro}: no REQUIRED expectations defined for this distro")
    for distro, cats in REQUIRED.items():
        have = pkgs_by_distro.get(distro, set())
        for cat in cats:
            if cat not in CATEGORIES:
                failures.append(
                    f"{distro}: REQUIRED names unknown category {cat!r}")
                continue
            if not any(c in have for c in CATEGORIES[cat]):
                failures.append(
                    f"{distro}: missing required category {cat!r} "
                    f"(expected one of: {', '.join(CATEGORIES[cat])})")
    return failures


def main() -> None:
    pkgs_by_distro = {}
    for d in DISTROS:
        pkgs_by_distro[d] = extract_packages(d)

    colw = 26
    # Distro names are used verbatim as column headers; an intermediate
    # name-shortening dict that mapped every name to itself was pure noise.
    header = "Category".ljust(34) + " | " + " | ".join(d.ljust(colw) for d in DISTROS)
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

    # ---- Gate 0b verdict (last, so it's the final line of output) ----
    print()
    print("=== required-category verdict ===")
    failures = check_required(pkgs_by_distro)
    for d in DISTROS:
        req = REQUIRED.get(d, [])
        miss = sum(1 for f in failures if f.startswith(f"{d}:"))
        print(f"  {d}: {len(req) - miss}/{len(req)} required categories present")
    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        print(f"AUDIT-FAIL: {len(failures)} required expectation(s) violated")
        sys.exit(1)
    total_req = sum(len(v) for v in REQUIRED.values())
    print(f"AUDIT-OK: all {total_req} required category/distro pairs present")


if __name__ == "__main__":
    main()
