#!/usr/bin/env python3
"""Python source lint (fleet Gate 0c; CI's changed-files check).

Runs pylint over the repo's Python files and fails on any message pylint
reports. Two scopes:

  (default)              every tracked ``*.py`` -- what the fleet gate runs
                         before a multi-hour build, so a syntax/name error
                         in linux_vm/ or scripts/ costs seconds, not hours
  --changed-since <ref>  only files touched in ``<ref> -> working tree``
                         (plus untracked new files) -- what CI runs on push
                         and on a manual dispatch, so a push gets feedback
                         on exactly what it changed instead of the whole tree

If <ref> cannot be resolved (force-push, a root commit, or the all-zero
"before" sha of a branch's first push) the script falls back to the full
scan rather than silently linting nothing, and says so.

pylint lookup order: this interpreter (``python -m pylint``), PATH, then
``<repo>/.venv``. A missing pylint is a hard setup error (exit 2), never a
silent pass.

Exit codes: 0 = clean, 1 = pylint findings, 2 = setup failure (no pylint,
no git, bad argument). No network, no VM; a full run takes ~15-20 s.

Usage:
    python scripts/lint-python.py                  # whole tree (Gate 0c)
    python scripts/lint-python.py --changed-since HEAD^
"""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parent.parent
RCFILE = REPO / ".pylintrc"
ZERO_SHA = "0" * 40
PYLINT_TIMEOUT_SEC = 600  # full-tree run is ~15-20 s; 10 min only fires if pylint hangs
SCORE_RE = re.compile(r"rated at ([0-9.]+)/10")


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git in the repo root, returning the completed process (text mode)."""
    return subprocess.run(["git", *args], cwd=str(REPO), text=True,
                          capture_output=True, timeout=120)


def _die(msg: str) -> NoReturn:
    print(f"[FAIL] {msg}")
    print("PYLINT-FAIL: setup error -- " + msg)
    sys.exit(2)


def find_pylint() -> list[str]:
    """Resolve a runnable pylint, or exit 2 with install instructions.

    Deliberately does NOT auto-install: a gate that quietly pip-installs
    into whatever interpreter happens to be running it is worse than one
    that refuses and says why.
    """
    try:
        r = subprocess.run([sys.executable, "-m", "pylint", "--version"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return [sys.executable, "-m", "pylint"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    import shutil  # local: only needed on this path
    exe = shutil.which("pylint")
    if exe:
        return [exe]
    for candidate in (REPO / ".venv" / "bin" / "pylint",
                      REPO / ".venv" / "Scripts" / "pylint.exe"):
        if candidate.exists():
            return [str(candidate)]
    _die("pylint not found (tried `python -m pylint`, PATH, "
         f"{REPO / '.venv'}). Install it: python -m pip install pylint "
         "-- or skip this gate with --no-pylint")


def tracked_files() -> list[str]:
    """Every tracked *.py in the repo (the whole-tree scope)."""
    r = _git("ls-files", "-z", "*.py")
    if r.returncode != 0:
        _die(f"git ls-files failed: {r.stderr.strip() or 'git unavailable'}")
    return [f for f in r.stdout.split("\0") if f]


def changed_files(ref: str) -> tuple[list[str], str]:
    """Files changed since *ref*, or (all tracked, reason) as a fallback.

    Diffs the ref against the WORKING TREE (not just HEAD) so a local run
    also covers staged/unstaged edits, and adds untracked new files that a
    pure diff would miss. Deletions are excluded -- pylint cannot lint a
    file that is gone.
    """
    reason = ""
    if not ref or set(ref) <= {"0"}:
        ref, reason = "", f"ref {ref!r} is not a real commit (branch's first push?)"
    else:
        probe = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if probe.returncode != 0 or not probe.stdout.strip():
            reason = f"ref {ref!r} does not resolve to a commit (force-push or root commit)"

    if reason:
        print(f"[ .. ] {reason} -- falling back to the full tree")
        return tracked_files(), "full tree (unresolvable ref)"

    diff = _git("diff", "--no-renames", "--diff-filter=d", "--name-only",
                "-z", ref, "--", "*.py")
    if diff.returncode != 0:
        print(f"[ .. ] git diff {ref} failed: {diff.stderr.strip()} -- "
              "falling back to the full tree")
        return tracked_files(), "full tree (git diff failed)"
    files = [f for f in diff.stdout.split("\0") if f]

    # Untracked-but-present new files: invisible to a diff, real to pylint.
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z",
                     "--", "*.py")
    if untracked.returncode == 0:
        files.extend(f for f in untracked.stdout.split("\0") if f)
    return sorted(set(files)), f"changed since {ref}"


def main() -> int:
    p = argparse.ArgumentParser(
        prog="lint-python.py",
        description="pylint gate: whole tree, or only files changed since a git ref.",
    )
    p.add_argument("--changed-since", default=None, metavar="REF",
                   help="Lint only files changed since REF (CI's push/dispatch scope)")
    args = p.parse_args()

    if args.changed_since is None:
        files, scope = tracked_files(), "all tracked Python files"
    else:
        files, scope = changed_files(args.changed_since)

    print(f"scope: {scope} ({len(files)} file(s))")
    for f in files:
        print(f"  {f}")

    if not files:
        # Nothing in scope is a pass, not a no-op: the gate ran and found
        # nothing to check. Exit 0 so a docs-only push stays green -- and
        # resolve pylint AFTER this check so an empty run never depends on
        # pylint being installed at all.
        print("[ ok ] no Python files in scope")
        print(f"PYLINT-OK: no Python files in scope ({scope})")
        return 0

    pylint_cmd = find_pylint()

    cmd = [*pylint_cmd]
    if RCFILE.exists():
        cmd.append(f"--rcfile={RCFILE}")
    cmd.extend(files)
    try:
        r = subprocess.run(cmd, cwd=str(REPO), text=True,
                           capture_output=True, timeout=PYLINT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        print(f"[FAIL] pylint exceeded {PYLINT_TIMEOUT_SEC}s -- treating as a hang")
        print(f"PYLINT-FAIL: timeout after {PYLINT_TIMEOUT_SEC}s ({scope})")
        return 1
    except OSError as e:
        _die(f"could not execute pylint: {e}")

    # pylint writes findings to stdout; stream both so a failure is fully
    # diagnosable from the log alone (same contract as the other gates).
    for stream in (r.stdout, r.stderr):
        for line in (stream or "").splitlines():
            print(line)

    score_m = SCORE_RE.search(r.stdout or "")
    score = f"{score_m.group(1)}/10" if score_m else "n/a"

    if r.returncode == 0:
        print(f"[ ok ] pylint: {score} over {len(files)} file(s)")
        print(f"PYLINT-OK: {score}, {len(files)} file(s), scope: {scope}")
        return 0
    print(f"[FAIL] pylint: rc={r.returncode} over {len(files)} file(s) "
          f"(score {score}; bit 16 = usage error, see output above)")
    print(f"PYLINT-FAIL: rc={r.returncode}, {len(files)} file(s), scope: {scope}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
