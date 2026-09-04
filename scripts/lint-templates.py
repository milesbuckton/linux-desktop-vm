#!/usr/bin/env python3
"""Pre-commit / pre-fleet template lint.

Renders every supported distro via the same jinja2
context the real setup_vm.py uses, then yaml.safe_load()s the result.
Catches:
  * Jinja syntax errors / undefined variables
  * Missing required template blocks (verify, post_runcmd, etc.)
  * Cloud-init YAML structural problems (bad indentation, unbalanced
    quotes, missing top-level keys)
  * Verify-block absence (which would let the build silently succeed
    on a half-installed VM -- the very class of bug we keep hitting)

Exits 0 on full pass, non-zero on any failure (suitable for CI / pre-fleet gate).

Does NOT touch the network or any VM. Runs in ~2-3 seconds.
"""
from __future__ import annotations
import re
import sys
import traceback
from pathlib import Path

try:
    import jinja2
    import yaml
except ImportError as e:
    sys.stderr.write(f"ERROR: missing dependency: {e}\nInstall: pip install jinja2 pyyaml\n")
    sys.exit(2)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from linux_vm.config import DISTROS, DISTRO_TEMPLATE
from linux_vm.templates import get_jinja_env, parse_cloud_config

TEMPLATES = REPO / "templates"

# Minimum required top-level keys in any rendered user-data.
REQUIRED_KEYS = {"users", "write_files", "runcmd"}


def render_context(distro: str, guest_arch: str = "x86_64") -> dict:
    """Build the same jinja context setup_vm.py would build at runtime."""
    return {
        "USERNAME": "testuser",
        "HOSTNAME": f"{distro}-vm",
        "VM_NAME": f"{distro} GNOME",
        "DISPLAY_NAME": distro,
        "PROVIDER": "qemu",
        "PASSWORD": "x",
        "ROOT_PASSWORD": "root",
        "PASSWORD_HASH": "$6$dummy$dummy",
        "ROOT_PASSWORD_HASH": "$6$dummy$dummy",
        "SSH_PUBLIC_KEY": "ssh-ed25519 AAAA test@lint",
        "TIMEZONE": "Africa/Johannesburg",
        "INSTANCE_ID": f"{distro}-vm-lintsim",
        "DISTRO_ID": distro,  # canonical slug; templates branch on this
        "GUEST_ARCH": guest_arch,
        "marker_name": distro,
        "display_name": distro,
        "simulate_only": False,
    }


def lint_one(env: jinja2.Environment, distro: str,
             simulate_only: bool = False, guest_arch: str = "x86_64") -> list[str]:
    """Return a list of error strings (empty list = pass)."""
    errors: list[str] = []
    tmpl_name = DISTRO_TEMPLATE[distro]
    try:
        tmpl = env.get_template(tmpl_name)
    except jinja2.exceptions.TemplateError as e:
        return [f"template load: {type(e).__name__}: {e}"]

    ctx = render_context(distro, guest_arch)
    ctx["simulate_only"] = simulate_only
    try:
        rendered = tmpl.render(**ctx)
    except jinja2.exceptions.TemplateError as e:
        return [f"render: {type(e).__name__}: {e}"]
    except Exception as e:
        return [f"render: unexpected {type(e).__name__}: {e}\n{traceback.format_exc()}"]

    # Strip the cloud-init "#cloud-config" header so yaml can load, then
    # parse via the shared helper (raises on YAML errors).
    try:
        loaded = parse_cloud_config(rendered)
    except yaml.YAMLError as e:
        return [f"yaml: {type(e).__name__}: {e}"]

    if not isinstance(loaded, dict):
        return [f"yaml: top-level is {type(loaded).__name__}, expected dict"]

    missing = REQUIRED_KEYS - set(loaded)
    if missing:
        errors.append(f"missing top-level keys: {sorted(missing)}")

    runcmd = loaded.get("runcmd", []) or []
    runcmd_text = "\n".join(str(x) for x in runcmd)
    if simulate_only:
        if "SIMULATE-OK" not in runcmd_text:
            errors.append("simulate-mode runcmd does not contain SIMULATE-OK marker; add a {% block simulate_install %} that emits it on success.")
        if "SIMULATE-FAIL" not in runcmd_text:
            errors.append("simulate-mode runcmd does not emit SIMULATE-FAIL on failure.")
    else:
        if "VERIFY-OK" not in runcmd_text:
            errors.append("runcmd block does not contain the verify gate (searched for 'VERIFY-OK' literal). Add a {% block verify %} that prints it on success.")
        if "VERIFY-FAIL" not in runcmd_text:
            errors.append("runcmd block does not emit 'VERIFY-FAIL' on failure. Verify gate must fail loudly so the orchestrator catches it.")

    return errors


# Expected real-build installs that are intentionally NOT in the simulate
# dry-run (best-effort tiers / defensive repairs / legacy fallbacks /
# vendor-repo apps).
# Kept in sync with the templates: google-chrome-stable is best-effort
#. The real install stays best-effort; the marker, not the
# build, reports it.
EXPECTED_GAPS = {
    "gentoo": {"gdm", "net-misc/networkmanager-gnome"},
    "ubuntu-lts": set(),
}


def _simulate_pkg_set(loaded: dict) -> set:
    for f in loaded.get("write_files", []) or []:
        if isinstance(f, dict) and f.get("path") == "/etc/install-simulate-pkgs.txt":
            return {line.strip() for line in (f.get("content") or "").splitlines() if line.strip()}
    return set()


def _gentoo_atoms_from_text(text: str) -> set:
    """Extract Gentoo package atoms (category/name) from emerge calls and
    PKGS="..." variable assignments in a shell script body."""
    atoms: set = set()
    for line in text.splitlines():
        # emerge --getbinpkg / --getbinpkgonly / --pretend calls.
        if "emerge " in line and ("--getbinpkg" in line or "--pretend" in line):
            after_emerge = re.sub(r"^.*?emerge\s+(?:--[\w-]+\s+)*", "", line)
            for pkg in re.findall(r"([\w][\w.-]*/[\w][\w.-]+)", after_emerge):
                if pkg.startswith("-") or "/" in pkg.split("/")[0]:
                    continue
                if pkg.startswith(("tmp/", "dev/", "var/", "etc/", "usr/", "opt/", "proc/", "boot/", "root/", "home/")):
                    continue
                atoms.add(pkg)
        # PKGS="cat/pkg cat/pkg ..." variable assignment (multi-line list).
        if "PKGS=" in line:
            for pkg in re.findall(r"([\w][\w.-]*/[\w][\w.-]+)", line):
                atoms.add(pkg)
    return atoms


def _real_install_set(distro: str, loaded: dict) -> set:
    s: set = set()
    if loaded.get("packages"):
        s.update(p for p in loaded["packages"] if isinstance(p, str))
    runcmd = loaded.get("runcmd", []) or []
    bootcmd = loaded.get("bootcmd", []) or []

    def raw_cmd(item):
        if isinstance(item, list) and len(item) >= 3 and item[0] == "sh" and item[1] == "-c":
            return item[2]
        return str(item)

    text = "\n".join(raw_cmd(x) for x in runcmd + bootcmd)

    for line in text.splitlines():
        if "$(" in line:
            continue
        for m in re.finditer(r"dnf install -y\s*'?(@?[\w.\-]+)", line):
            s.add(m.group(1))
        for m in re.finditer(r"apt-get install -y\s+(\S+)", line):
            s.add(m.group(1))
        # Gentoo: extract package atoms from emerge commands.
        if "emerge " in line and ("--getbinpkg" in line or "--pretend" in line):
            s.update(_gentoo_atoms_from_text(line))
    # Gentoo's real install runs from a standalone systemd service script
    # (gentoo-install.sh, a write_file) decoupled from cloud-init so a
    # Portage sync hang can't block SSH. Parse that script's emerge calls
    # + PKGS="..." so the parity gate sees the same package set the
    # simulate dry-run enumerates.
    if distro == "gentoo":
        for f in loaded.get("write_files", []) or []:
            if not isinstance(f, dict):
                continue
            # write_file paths are rendered relative (no leading slash).
            if f.get("path", "").endswith("gentoo-install.sh"):
                s.update(_gentoo_atoms_from_text(f.get("content") or ""))
    return s


def lint_parity(env: jinja2.Environment, distro: str,
                guest_arch: str = "x86_64") -> list[str]:
    """Assert the simulate dry-run list == the real build's install set.

    The whole point of the simulate gate is that it validates exactly what
    a full build installs; hand-maintained duplicate lists drifted (bad
    package names, missing python3-venv, dead plymouth config), so the
    lists are now single-source. This check is the regression net: it
    fails if simulate ever validates a package the real build won't
    install, or omits one it will (beyond EXPECTED_GAPS).
    """
    tmpl = env.get_template(DISTRO_TEMPLATE[distro])

    ctx = render_context(distro, guest_arch)
    ctx["simulate_only"] = True
    try:
        sim = _simulate_pkg_set(parse_cloud_config(tmpl.render(**ctx)))
        ctx["simulate_only"] = False
        real = _real_install_set(distro, parse_cloud_config(tmpl.render(**ctx)))
    except (jinja2.TemplateError, yaml.YAMLError) as e:
        return [f"parity render: {type(e).__name__}: {e}"]

    only_sim = sim - real
    unexpected_real = (real - sim) - EXPECTED_GAPS.get(distro, set())
    errors: list[str] = []
    if only_sim:
        errors.append(f"simulate validates {len(only_sim)} package(s) the real build never installs: {sorted(only_sim)}")
    if unexpected_real:
        errors.append(f"real build installs {len(unexpected_real)} package(s) the simulate list omits: {sorted(unexpected_real)}")
    return errors


def lint_network_config() -> list[str]:
    """Validate templates/network-config.j2 as standalone YAML."""
    errors: list[str] = []
    path = TEMPLATES / "network-config.j2"
    if not path.exists():
        return ["network-config.j2 not found"]
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            errors.append(f"network-config.j2: top-level is {type(data).__name__}, expected dict")
    except yaml.YAMLError as e:
        errors.append(f"network-config.j2: YAML error: {e}")
    except OSError as e:
        errors.append(f"network-config.j2: read error: {e}")
    return errors


def main() -> int:
    # The env config lives in linux_vm.templates.get_jinja_env() so this
    # render is byte-identical to what setup_vm.py produces at runtime
    # (drift here cost us the simulate_install block once).
    env = get_jinja_env(TEMPLATES)
    total = 0
    failed = 0

    # First, validate standalone templates (network-config.j2).
    nc_errors = lint_network_config()
    if nc_errors:
        failed += 1
        for err in nc_errors:
            print(f"[FAIL] network-config.j2: {err}")
    else:
        print("[ ok ] network-config.j2")

    for distro in DISTROS:
        for guest_arch in ("x86_64", "aarch64"):
            for simulate_only in (False, True):
                total += 1
                tag = f"{distro}/qemu/{guest_arch}/{'sim' if simulate_only else 'real'}"
                errors = lint_one(env, distro, simulate_only=simulate_only,
                                  guest_arch=guest_arch)
                if errors:
                    failed += 1
                    print(f"[FAIL] {tag}")
                    for err in errors:
                        print(f"       {err}")
                else:
                    print(f"[ ok ] {tag}")
            total += 1
            # Parity: simulate dry-run list == real-build install set.
            tag_parity = f"{distro}/qemu/{guest_arch}/parity"
            p_errors = lint_parity(env, distro, guest_arch)
            if p_errors:
                failed += 1
                print(f"[FAIL] {tag_parity}")
                for err in p_errors:
                    print(f"       {err}")
            else:
                print(f"[ ok ] {tag_parity}")
    print(f"Total: {total} | Passed: {total - failed} | Failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
