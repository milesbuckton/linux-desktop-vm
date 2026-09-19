"""Shared package-atom extraction from rendered cloud-init YAML.

DRY: both scripts/lint-templates.py and scripts/audit-packages.py need to
extract per-distro package atoms from the same rendered template. The
Gentoo case parses `emerge --getbinpkg ...` and `PKGS="..."` variable
assignments across multiple lines; if these two callers diverge, one
will silently disagree with the other on which packages the build
installs. Single source of truth.
"""
from __future__ import annotations
import re


# Atomic regex used by both scripts: a Gentoo atom is `category/name` where
# category and name are word characters, dots, hyphens. Block known
# filesystem-leading segments so `tmp/foo`, `etc/init.d/foo`, etc. don't
# match.
_ATOM_RE = re.compile(r"([\w][\w.-]*/[\w][\w.-]+)")


def gentoo_atoms_from_text(text: str) -> set[str]:
    """Extract Gentoo package atoms (category/name) from emerge calls and
    `PKGS="..."` variable assignments in a shell script body.

    Skips atoms that begin with filesystem-leading segments or with `-`
    (a USE-flag-style negation).
    """
    atoms: set[str] = set()
    for line in text.splitlines():
        if "emerge " in line and ("--getbinpkg" in line or "--pretend" in line):
            after_emerge = re.sub(r"^.*?emerge\s+(?:--[\w-]+\s+)*", "", line)
            for pkg in _ATOM_RE.findall(after_emerge):
                if pkg.startswith("-") or "/" in pkg.split("/")[0]:
                    continue
                if pkg.startswith(("tmp/", "dev/", "var/", "etc/", "usr/", "opt/",
                                   "proc/", "boot/", "root/", "home/")):
                    continue
                atoms.add(pkg)
        if "PKGS=" in line:
            for pkg in _ATOM_RE.findall(line):
                atoms.add(pkg)
    return atoms
