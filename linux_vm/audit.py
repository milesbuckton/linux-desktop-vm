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
from pathlib import Path


# Shell binaries and the command flags that make argv[-1] a script rather
# than an operand. Used by `shell_script()` to recognise a
# `[<shell>, <-flag...>, <script>]` cloud-init entry. Keeping this in the
# shared module is what stops the two gates disagreeing: the audit script
# used to unwrap only `["sh", "-c", ...]`, so a package list spelled
# `["bash", "-lc", ...]` was invisible to the parity audit while still
# being executed by the build.
_SHELL_BINARIES = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash"}
_SHELL_CMD_FLAGS = {"-c", "-lc", "-cl", "-ic", "-i", "-l"}


# Atomic regex used by both scripts: a Gentoo atom is `category/name` where
# category and name are word characters, dots, hyphens. Note the leading
# `[\w]` -- it is what makes a USE-flag-style negation (`-category/name`) or
# an absolute path (`/etc/foo`) unmatchable, so no extra guard is needed for
# either. What the regex CAN'T exclude is a filesystem path whose first
# segment is word-shaped (`etc/init.d`, `usr/lib`, `tmp/x`), which is why the
# segment blocklist below is applied to EVERY match, on every branch.
_ATOM_RE = re.compile(r"([\w][\w.-]*/[\w][\w.-]+)")

# Path-shaped prefixes that are never a real Portage category. `dev/` is in
# the list only as belt-and-braces: real categories are `dev-python`,
# `dev-util`, ... so it never matches a live atom.
_PATH_PREFIXES = ("tmp/", "dev/", "var/", "etc/", "usr/", "opt/",
                  "proc/", "boot/", "root/", "home/")


def _is_real_atom(pkg: str) -> bool:
    """True if `pkg` looks like a Portage atom rather than a file path."""
    return not pkg.startswith(_PATH_PREFIXES)


def shell_script(entry: object) -> str | None:
    """Return the script of a `[<shell>, <-flag...>, <script>]` entry.

    None when the entry is not a shell-with-a-script invocation. Accepts
    any known shell binary (not just `sh`) and any of the combined flag
    forms, so an entry can never escape a gate by spelling the shell
    differently -- which is exactly the drift the parity extractor and
    the template syntax check used to disagree about.

    Shared by scripts/lint-templates.py (which syntax-checks every
    extracted script) and scripts/audit-packages.py (which reads package
    lists out of them).
    """
    if not (isinstance(entry, list) and len(entry) >= 3):
        return None
    binary = entry[0]
    if not (isinstance(binary, str)
            and Path(binary).name in _SHELL_BINARIES):
        return None
    flags = [a for a in entry[1:-1] if isinstance(a, str)]
    if not flags or not set(flags) <= _SHELL_CMD_FLAGS:
        return None
    script = entry[-1]
    return script if isinstance(script, str) else None


def gentoo_atoms_from_text(text: str) -> set[str]:
    """Extract Gentoo package atoms (category/name) from emerge calls and
    `PKGS="..."` variable assignments in a shell script body.

    Applies the same filesystem-leading segment blocklist to both branches.
    It used to apply only to the `emerge` branch, so a `PKGS=` line leaked
    `etc/init.d/foo`-shaped entries into the audit set -- contradicting this
    docstring and inflating both the printed table and the REQUIRED verdict.
    """
    atoms: set[str] = set()
    for line in text.splitlines():
        if "emerge " in line and ("--getbinpkg" in line or "--pretend" in line):
            after_emerge = re.sub(r"^.*?emerge\s+(?:--[\w-]+\s+)*", "", line)
            atoms.update(p for p in _ATOM_RE.findall(after_emerge)
                         if _is_real_atom(p))
        if "PKGS=" in line:
            atoms.update(p for p in _ATOM_RE.findall(line)
                         if _is_real_atom(p))
    return atoms
