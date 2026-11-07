"""Regression tests for the gate scripts' venv bootstrap.

Every test here locks a FIX applied to a defect that the Python 3.15
upgrade exposed, plus the two ways the re-exec itself could go wrong.

THE DEFECT: the four gate scripts re-exec into the repo's ``.venv`` so
they always have their dependencies (jinja2, yaml, pylint). The guard
that decided "am I already in the venv?" compared RESOLVED PATHS::

    if v.exists() and str(v) != sys.executable \\
            and v.resolve() != Path(sys.executable).resolve():
        os.execv(str(v), [str(v), __file__, *sys.argv[1:]])

A virtual environment's ``bin/python`` is a SYMLINK to the base
interpreter, so ``Path(v).resolve()`` and
``Path(sys.executable).resolve()`` are the SAME FILE whenever the venv
was built from the interpreter that is running the script. The guard
therefore no-op'd in exactly the case it exists for: running
``/opt/homebrew/bin/python3 scripts/lint-templates.py`` against a venv
built from that same Homebrew Python. The gates then died with
``ERROR: missing dependency: No module named 'jinja2'`` /
``ModuleNotFoundError: No module named 'jinja2'`` / ``TESTS-FAIL: 0
failure(s), 67 error(s) out of 186 tests`` instead of quietly using the
venv that was sitting right there.

It only ever worked by LUCK: the venv had been built from Homebrew
Python 3.14.7 while ``/opt/homebrew/bin/python3`` was 3.14.8_3, so the
resolved paths differed and the guard fired. Rebuild the venv from the
same Homebrew Python that is on PATH and it breaks -- which is exactly
what upgrading 3.14 -> 3.15 did.

THE FIX: one shared helper, `linux_vm/venv_bootstrap.py`, which asks
the interpreter the question it can actually answer -- "is this a
virtual environment?", i.e. ``sys.prefix != sys.base_prefix`` -- instead
of comparing symlink-resolved paths.

Two further hazards the helper also closes, both of which are locked
here:

* The re-exec could LOOP FOREVER. A venv whose ``pyvenv.cfg`` is
  missing or unreadable also reports ``sys.prefix ==
  sys.base_prefix``, so the guard re-fires after the re-exec and the
  process replaces itself indefinitely -- a silent, output-free spin.
  The helper's environment sentinel makes the re-exec happen AT MOST
  ONCE; the second pass reports the broken venv and exits 2.
* IMPORTING a gate script replaced the CALLING PROCESS. The gate
  scripts call their bootstrap at MODULE level, so a test doing
  ``runpy.run_path("scripts/audit-packages.py")`` under a non-venv
  interpreter had its own process ``os.execv``'d away: the unittest
  summary never printed and the suite exited 0 having verified
  nothing. The scripts therefore pass ``allow_exec=__name__ ==
  "__main__"``.

The behavioural tests exercise the shared helper directly (with
``in_venv`` and ``os.execv`` patched, so nothing is ever re-exec'd for
real); the gate scripts themselves are locked STATICALLY, because their
bootstrap is a one-line delegation to that helper.

Run from repo root:
    python3 scripts/run-tests.py
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

# The four gate scripts that re-exec into the repo's .venv.
GATE_SCRIPTS = (
    "scripts/lint-templates.py",
    "scripts/audit-packages.py",
    "scripts/run-tests.py",
    "scripts/smoke-test-cli.py",
)

#: The interpreter OUTSIDE any venv. `sys._base_executable` is the
#: binary the venv was built from; it is what makes "not in a venv"
#: reproducible in a subprocess.
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable

# The guard as it was BEFORE the fix, reproduced verbatim. It is loaded
# into a module of its own so the suite can prove the regression test
# discriminates: under the same fake environment the old guard performs
# NO exec.
OLD_GUARD_SOURCE = '''\
def old_bootstrap_venv() -> None:
    for cand in ("bin/python", "bin/python3"):
        v = REPO / ".venv" / cand
        if v.exists() and str(v) != sys.executable and v.resolve() != Path(sys.executable).resolve():
            os.execv(str(v), [str(v), __file__, *sys.argv[1:]])
'''


class _FakeSys:
    """A stand-in for the `sys` module as the bootstrap guard sees it."""

    def __init__(self, prefix: str, base_prefix: str, executable: str) -> None:
        self.prefix = prefix
        self.base_prefix = base_prefix
        self.executable = executable
        self.argv = ["gate-script.py"]


class _RecordingOs:
    """A stand-in for `os` that records execv instead of performing it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def execv(self, path: str, argv: list[str]) -> None:  # noqa: D102
        self.calls.append((path, argv))


def _import_source(source: str, module_name: str, tmpdir: str) -> object:
    """Write `source` to a temp module and import it.

    `importlib` rather than the `exec` builtin: the module body is only
    a function definition, so there is nothing to execute beyond that,
    and this keeps the gate's pylint score at 10.00.
    """
    path = Path(tmpdir) / f"{module_name}.py"
    # `from __future__ import annotations` keeps PEP 604 annotations in
    # the extracted signature lazy, so the module also imports on the
    # older interpreters the host may still have.
    path.write_text("from __future__ import annotations\n" + source,
                    encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_venv_like_repo(tmp: str) -> tuple[Path, Path]:
    """Build a repo dir whose .venv/bin/python is a symlink to `target`.

    Returns (repo_dir, venv_python).
    """
    repo = Path(tmp) / "repo"
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(target=sys.executable)
    return repo, venv_python


def _resolved_path_comparisons(script_rel: str) -> list[str]:
    """Every `Path(sys.executable).resolve()`-shaped call in a script.

    AST-based, so a DOCSTRING or comment that merely MENTIONS the old
    expression (the fix documents it at length) is not a hit -- only
    real code is.
    """
    source = (REPO / script_rel).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=script_rel)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "resolve":
            continue
        rendered = ast.unparse(node)
        if "sys.executable" in rendered:
            hits.append(rendered)
    return hits


def _bootstrap_calls(script_rel: str) -> list[ast.Call]:
    """Every `bootstrap_venv(...)` call in a gate script.

    The three gate scripts wrap the call in a private `_bootstrap_venv()`
    function that is then called at module level; the smoke test calls
    the helper directly. So the call is looked for anywhere in the tree.
    """
    source = (REPO / script_rel).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=script_rel)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", "")
        if name == "bootstrap_venv":
            calls.append(node)
    return calls


class TestSharedBootstrapHelper(unittest.TestCase):
    """`linux_vm/venv_bootstrap.py` owns the re-exec decisions."""

    def setUp(self) -> None:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from linux_vm import venv_bootstrap  # noqa: PLC0415
        self.mod = venv_bootstrap

    def test_in_venv_uses_prefix_not_paths(self) -> None:
        """`in_venv()` must be `sys.prefix != sys.base_prefix`."""
        source = (REPO / "linux_vm" / "venv_bootstrap.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source, filename="venv_bootstrap.py")
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "in_venv":
                rendered = ast.unparse(node)
                self.assertIn("sys.prefix != sys.base_prefix", rendered)
                self.assertNotIn(".resolve()", rendered)
                return
        self.fail("venv_bootstrap.py has no top-level in_venv()")

    def test_helper_execs_into_the_venv_when_not_in_one(self) -> None:
        """Not in a venv -> exec the venv interpreter, exactly once."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, venv_python = _make_venv_like_repo(tmp)
            recorder = _RecordingOs()
            with mock.patch.object(self.mod, "in_venv", return_value=False), \
                 mock.patch.object(self.mod.os, "execv", recorder.execv), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(self.mod.BOOTSTRAP_SENTINEL, None)
                self.mod.bootstrap_venv(repo, repo / "gate.py")
            self.assertEqual(len(recorder.calls), 1)
            self.assertEqual(recorder.calls[0][0], str(venv_python))
            # sanity: the venv python really does resolve to the same
            # file as the running interpreter -- that is the case the
            # OLD guard got wrong.
            self.assertEqual(
                venv_python.resolve(),
                Path(sys.executable).resolve(),
                "test setup must reproduce the symlink-to-base-interpreter "
                "shape, otherwise it proves nothing",
            )

    def test_helper_is_a_no_op_inside_a_venv(self) -> None:
        """Already in a venv -> return, so the exec cannot loop forever."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _venv_python = _make_venv_like_repo(tmp)
            recorder = _RecordingOs()
            with mock.patch.object(self.mod, "in_venv", return_value=True), \
                 mock.patch.object(self.mod.os, "execv", recorder.execv):
                self.assertIsNone(
                    self.mod.bootstrap_venv(repo, repo / "gate.py"))
            self.assertEqual(recorder.calls, [])

    def test_broken_venv_reports_instead_of_looping(self) -> None:
        """A venv with no pyvenv.cfg must NOT cause an exec loop.

        The sentinel is what makes the second pass report the problem
        instead of replacing the process forever.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo, _venv_python = _make_venv_like_repo(tmp)
            env = dict(os.environ)
            env[self.mod.BOOTSTRAP_SENTINEL] = "1"
            proc = subprocess.run(
                [BASE_PYTHON, "-c",
                 f"import sys; sys.path.insert(0, {str(REPO)!r});"
                 f"from pathlib import Path;"
                 f"from linux_vm.venv_bootstrap import bootstrap_venv;"
                 f"bootstrap_venv(Path({str(repo)!r}), Path({str(repo / 'gate.py')!r}), allow_exec=True)"],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertEqual(
                proc.returncode, 2,
                f"a broken venv must exit 2, not loop; stderr={proc.stderr!r}",
            )
            self.assertIn("does not look like a virtual environment",
                          proc.stderr)

    def test_importing_a_gate_script_never_replaces_the_process(self) -> None:
        """`runpy.run_path` on a gate script must not exec the caller.

        This is the "gate that quietly verifies nothing" failure: the
        gate script's module-level bootstrap used to `os.execv` the
        TEST process, so the unittest summary never printed and the
        suite exited 0 having checked nothing.

        Run with the BASE interpreter, i.e. one that is NOT in a venv
        -- that is the only situation in which the bootstrap would
        re-exec at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.py"
            probe.write_text(
                "import runpy\n"
                "print('BEFORE')\n"
                f"runpy.run_path({str(REPO / 'scripts' / 'audit-packages.py')!r}, run_name='audit_not_main')\n"
                "print('AFTER')\n",
                encoding="utf-8")
            proc = subprocess.run(
                [BASE_PYTHON, str(probe)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertIn("BEFORE", proc.stdout)
            self.assertIn(
                "AFTER", proc.stdout,
                "the calling process was replaced by the gate script's "
                f"re-exec; stdout={proc.stdout!r} stderr={proc.stderr!r}",
            )


class TestGateScriptsDelegateToTheHelper(unittest.TestCase):
    """Each gate script must delegate, and only exec when it is __main__."""

    def test_every_gate_script_delegates_to_the_shared_helper(self) -> None:
        for script_rel in GATE_SCRIPTS:
            with self.subTest(script=script_rel):
                calls = _bootstrap_calls(script_rel)
                self.assertEqual(
                    len(calls), 1,
                    f"{script_rel} must call the shared "
                    f"bootstrap_venv() helper exactly once",
                )
                call = calls[0]
                keywords = {kw.arg: ast.unparse(kw.value)
                            for kw in call.keywords}
                self.assertIn(
                    "allow_exec", keywords,
                    f"{script_rel} must pass allow_exec explicitly",
                )
                self.assertEqual(
                    keywords["allow_exec"], "__name__ == '__main__'",
                    f"{script_rel} may only re-exec when it is the script "
                    f"being run; importing a gate script must never replace "
                    f"the calling process",
                )

    def test_no_gate_script_compares_resolved_interpreter_paths(self) -> None:
        """Static lock: the resolved-path comparison must not come back."""
        for script_rel in GATE_SCRIPTS:
            with self.subTest(script=script_rel):
                hits = _resolved_path_comparisons(script_rel)
                self.assertEqual(
                    hits, [],
                    f"{script_rel} must not compare resolved interpreter "
                    f"paths -- a venv's bin/python is a symlink to the base "
                    f"interpreter, so that comparison is always False when "
                    f"it matters",
                )

    def test_the_old_resolved_path_guard_would_have_missed_this(self) -> None:
        """Prove the new test can actually detect the regression.

        The old guard is loaded verbatim below. Under the very same
        fake environment it performs NO exec -- which is why the gates
        died with "No module named 'jinja2'" after the 3.15 upgrade.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo, _venv_python = _make_venv_like_repo(tmp)
            recorder = _RecordingOs()
            module = _import_source(OLD_GUARD_SOURCE, "old_guard", tmp)
            module.sys = _FakeSys("/base", "/base", sys.executable)
            module.REPO = repo
            module.os = recorder
            module.Path = Path
            module.__file__ = "scripts/lint-templates.py"
            module.old_bootstrap_venv()
            self.assertEqual(
                recorder.calls, [],
                "the old resolved-path guard is expected to MISS this case; "
                "if it did not, the regression this suite locks would not "
                "have been real",
            )


if __name__ == "__main__":
    unittest.main()
