"""Regression tests for the findings of the repository code review.

Every test in here locks a FIX that was applied to a defect found by
reviewing the repo, so the defect cannot come back silently. The tests
are grouped by the finding they cover, and each one states the failure
it prevents in its docstring.

Run from repo root:
    python3 scripts/run-tests.py
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from linux_vm import download, templates
from linux_vm.templates import get_jinja_env
from linux_vm.test_context import render_context


# --------------------------------------------------------------------------
# H2 / H3 -- the Gentoo verify block must assert the whole curated app set
#            and must gate the mesa fork's GL driver and Vulkan ICDs.
# --------------------------------------------------------------------------
class TestGentooVerifyGatesCuratedSetAndMesa(unittest.TestCase):
    """Gentoo's verify block was a 13-of-67 stub with no mesa gate at all.

    Two defects, both invisible to a passing build:

    * The install loop swallows an emerge failure behind a trailing
      `|| true`, so a curated atom that failed to install left NO trace
      in the guest. The verify block asserted 13 of the 67 curated
      atoms, so the other 54 could all be missing and the build still
      reported VERIFY-OK.
    * The installer UNMERGES media-libs/mesa and x11-apps/mesa-progs and
      then builds the fork. A failed fork build therefore leaves the
      guest with NO GL driver at all -- and the old verify block said
      nothing about mesa, so it printed VERIFY-OK over a guest with no
      graphics stack. Ubuntu has had a mesa/ICD gate for a while (the
      duplicate-venus scar); Gentoo had none.
    """

    def _gentoo(self, simulate_only: bool = False) -> str:
        ctx = render_context("gentoo")
        ctx["simulate_only"] = simulate_only
        return get_jinja_env(REPO / "templates").get_template(
            "gentoo.j2").render(**ctx)

    def _verify_script(self) -> str:
        import yaml
        doc = yaml.safe_load(self._gentoo())
        for entry in doc.get("runcmd") or []:
            if isinstance(entry, list) and len(entry) >= 3 and entry[1] == "-c":
                script = str(entry[2])
                if "VERIFY-OK: all required components present" in script:
                    return script
        raise AssertionError("gentoo.j2 must have a verify block that prints "
                             "the success marker")

    def _install_script(self) -> str:
        import yaml
        doc = yaml.safe_load(self._gentoo())
        for wf in doc.get("write_files") or []:
            if isinstance(wf, dict) and str(wf.get("path", "")).endswith(
                    "gentoo-install.sh"):
                return str(wf.get("content") or "")
        raise AssertionError("gentoo-install.sh not found in the rendered "
                             "user-data")

    def test_verify_asserts_every_curated_atom(self):
        """The verify atom list must equal the install script's PKGS= list.

        Both come from the same source (`_gentoo_real_pkgs`), so a
        curated atom that is installed but not asserted -- or asserted
        but not installed -- is a build-time inconsistency.
        """
        script = self._verify_script()
        m = re.search(r"for ATOM in ([^;]+); do", script)
        self.assertIsNotNone(m,
                             "the verify block must iterate the curated atoms")
        asserted = m.group(1).split()

        pkgs = re.search(r'(?m)^PKGS="([^"]*)"$', self._install_script())
        self.assertIsNotNone(pkgs,
                             'PKGS="..." assignment not found in the install script')
        installed = pkgs.group(1).split()

        self.assertEqual(
            sorted(asserted), sorted(installed),
            "the verify block must assert exactly the atoms the installer "
            "emerges -- an atom installed but not asserted can fail to "
            "install with no trace (the install loop swallows emerge "
            "failures behind `|| true`)",
        )
        self.assertGreaterEqual(len(asserted), 60,
                                "the curated set is ~67 atoms; a 13-atom "
                                "assertion is the defect this locks")

    def test_verify_uses_the_exact_qlist_match(self):
        """`qlist -I` alone is a PREFIX match and would match the wrong atom.

        portage-utils' qlist matches "CAT/PF" as a prefix when the
        pattern is not exact, so `qlist -I gnome-base/gnome-shell` also
        matches gnome-base/gnome-shell-extension-desktop-icons-ng. The
        exact flag (-e) is what makes the check mean "this atom".
        """
        script = self._verify_script()
        self.assertIn("qlist -I -e", script,
                      "the curated-atom gate must use `qlist -I -e` -- "
                      "without -e, qlist prefix-matches and the gate is "
                      "not about the atom it names")
        self.assertNotRegex(
            script, r"qlist -I (?!-e)",
            "a non-exact qlist -I in the verify block can be satisfied by a "
            "different package that merely shares the prefix",
        )

    def test_verify_gates_the_mesa_fork_gl_driver(self):
        """No libgallium-*.so means no GL driver -- that must fail."""
        script = self._verify_script()
        self.assertIn("libgallium-", script,
                      "the verify block must check for the fork's GL driver")
        self.assertIn("MESA-ICD-VERIFIED", script,
                      "the mesa/ICD gate must emit its own marker so a "
                      "failure is attributable")

    def test_verify_gates_the_vulkan_icd_manifests(self):
        """One arch-suffixed ICD per driver, no bare twin, resolvable path.

        The bare-name twin is the duplicate-venus scar: apt's
        mesa-vulkan-drivers ships `virtio_icd.json` while the fork
        installs `virtio_icd.aarch64.json`, and both register the same
        library -- vulkaninfo then enumerates the same physical device
        twice (3 GPUs).
        """
        script = self._verify_script()
        for driver in ("libvulkan_lvp.so", "libvulkan_virtio.so"):
            self.assertIn(driver, script,
                          f"{driver} must be gated -- a new Vulkan driver "
                          "cannot be added without a registration check")
        self.assertRegex(script, r"ICD_ARCH_N",
                         "the gate must count the arch-suffixed manifests")
        self.assertRegex(script, r"ICD_BARE_N",
                         "the gate must count the bare (non-suffixed) twins")
        self.assertRegex(script, r'library_path',
                         "the gate must read the manifest's library_path")
        self.assertRegex(script, r"/\*\)",
                         "the gate must reject a relative library_path")

    def test_the_icd_gate_rejects_a_duplicate_bare_manifest(self):
        """Run the REAL gate fragment against synthetic fixtures.

        A textual assertion can only prove the gate MENTIONS the
        duplicate case. This executes the fragment that the rendered
        template actually ships, in a sandbox, against seven fixture
        layouts -- including the duplicate-venus layout that this gate
        exists to catch (a bare `lvp_icd.json` next to the fork's
        `lvp_icd.aarch64.json`).
        """
        import json
        import subprocess

        script = self._verify_script()
        start = script.find("for ICD_LIB in")
        end = script.find("; emit 'MESA-ICD-VERIFIED", start)
        self.assertGreater(start, 0, "the ICD gate fragment must be present")
        self.assertGreater(end, start,
                           "the ICD gate must be followed by its marker")
        fragment = script[start:end]

        # The fragment reads absolute paths; run it with a rewritten copy
        # so the fixtures live in a temp dir. The gate's own semantics
        # (counts, absolute-path check, .so existence) are untouched.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            icd_dir = base / "icd.d"
            lib_dir = base / "lib"
            icd_dir.mkdir()
            lib_dir.mkdir()

            def manifest(name: str, lib: str) -> str:
                return json.dumps({
                    "file_format_version": "1.0.0",
                    "ICD": {"library_path": lib, "api_version": "1.3.0"},
                })

            lvp_so = lib_dir / "libvulkan_lvp.so"
            virt_so = lib_dir / "libvulkan_virtio.so"
            lvp_so.write_bytes(b"")
            virt_so.write_bytes(b"")

            def run_gate() -> str:
                # The gate scans a list of candidate icd.d directories.
                # Point the first one at the fixture and the others at
                # paths that do not exist, so exactly one directory is
                # in play.
                body = fragment.replace(
                    "for ICD_DIR in /usr/share/vulkan/icd.d "
                    "/usr/lib64/vulkan/icd.d /usr/lib/vulkan/icd.d",
                    f"for ICD_DIR in {icd_dir} /nonexistent-a /nonexistent-b")
                body = body.replace('"/usr/lib64/$ICD_SO"',
                                    f'"{lib_dir}/$ICD_SO"')
                body = body.replace('"/usr/lib/$ICD_SO"',
                                    f'"{lib_dir}/$ICD_SO"')
                # The fragment calls the verify block's own emitfail
                # helper; stub it so the gate's DECISION is what runs.
                preamble = ("emitfail() { echo \"FAIL: $1\" ; exit 1 ; } ; "
                            "emit() { echo \"$1\" ; } ; ")
                proc = subprocess.run(
                    ["/bin/sh", "-c", preamble + body],
                    capture_output=True, text=True,
                    timeout=60, check=False)
                return proc.stdout + proc.stderr

            def reset() -> None:
                for f in icd_dir.iterdir():
                    f.unlink()

            # 1. the healthy layout: one arch-suffixed manifest per driver
            reset()
            (icd_dir / "lvp_icd.aarch64.json").write_text(
                manifest("lvp", str(lvp_so)))
            (icd_dir / "virtio_icd.aarch64.json").write_text(
                manifest("virtio", str(virt_so)))
            out = run_gate()
            self.assertNotIn("FAIL", out,
                             f"the healthy layout must pass the gate: {out}")

            # 2. the duplicate-venus layout: a bare twin next to the
            #    arch-suffixed manifest -> the loader opens the device twice
            reset()
            (icd_dir / "lvp_icd.aarch64.json").write_text(
                manifest("lvp", str(lvp_so)))
            (icd_dir / "lvp_icd.json").write_text(
                manifest("lvp", str(lvp_so)))
            (icd_dir / "virtio_icd.aarch64.json").write_text(
                manifest("virtio", str(virt_so)))
            out = run_gate()
            self.assertIn("FAIL", out,
                          f"a bare ICD twin must fail the gate: {out}")

            # 3. a driver with no manifest at all
            reset()
            (icd_dir / "lvp_icd.aarch64.json").write_text(
                manifest("lvp", str(lvp_so)))
            out = run_gate()
            self.assertIn("FAIL", out,
                          f"a missing ICD manifest must fail the gate: {out}")

            # 4. a relative library_path resolves against the wrong library
            reset()
            (icd_dir / "lvp_icd.aarch64.json").write_text(
                manifest("lvp", "libvulkan_lvp.so"))
            (icd_dir / "virtio_icd.aarch64.json").write_text(
                manifest("virtio", str(virt_so)))
            out = run_gate()
            self.assertIn("FAIL", out,
                          f"a relative library_path must fail the gate: {out}")

            # 5. two arch-suffixed manifests for one driver
            reset()
            (icd_dir / "lvp_icd.aarch64.json").write_text(
                manifest("lvp", str(lvp_so)))
            (icd_dir / "lvp_icd.aarch64.json.2.json").write_text(
                manifest("lvp", str(lvp_so)))
            (icd_dir / "virtio_icd.aarch64.json").write_text(
                manifest("virtio", str(virt_so)))
            out = run_gate()
            self.assertIn("FAIL", out,
                          f"two manifests for one driver must fail the gate: "
                          f"{out}")

            # 6. an empty icd.d -> no literal-glob crash, a clean FAIL
            reset()
            out = run_gate()
            self.assertIn("FAIL", out,
                          f"an empty icd.d must fail the gate: {out}")
            self.assertNotIn("No such file or directory", out,
                             "an unmatched glob must not leak a shell error")

            # 7. a manifest pointing at a .so that does not exist
            reset()
            (icd_dir / "lvp_icd.aarch64.json").write_text(
                manifest("lvp", str(lib_dir / "libvulkan_lvp.so")))
            (icd_dir / "virtio_icd.aarch64.json").write_text(
                manifest("virtio", str(virt_so)))
            lvp_so.unlink()
            out = run_gate()
            self.assertIn("FAIL", out,
                          f"a manifest pointing at a missing .so must fail: "
                          f"{out}")

    def test_the_curated_atom_gate_fails_on_a_missing_atom(self):
        """Run the REAL atom loop against a fake qlist.

        The gate's whole point is that a curated atom which failed to
        install must FAIL the build. The install loop swallows emerge
        failures behind `|| true`, so this gate is the only thing that
        can see them -- which means the gate itself must be proven to
        fail closed, not merely to mention the atoms.
        """
        import subprocess

        script = self._verify_script()
        start = script.find("for ATOM in")
        end = script.find("; command -v snap", start)
        self.assertGreater(start, 0, "the atom loop must be present")
        self.assertGreater(end, start,
                           "the atom loop must be followed by the next gate")
        fragment = script[start:end]

        m = re.search(r"for ATOM in ([^;]+); do", fragment)
        self.assertIsNotNone(m)
        atoms = m.group(1).split()
        self.assertGreaterEqual(len(atoms), 60)

        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            # A fake qlist that knows only the atoms in $INSTALLED.
            qlist = fake_bin / "qlist"
            qlist.write_text(
                "#!/bin/sh\n"
                "# args: -I -e <atom>\n"
                "for a in \"$@\" ; do\n"
                "  case \"$a\" in\n"
                "    -I|-e) continue ;;\n"
                "  esac\n"
                "  case \" $INSTALLED \" in\n"
                "    *\" $a \"*) exit 0 ;;\n"
                "  esac\n"
                "done\n"
                "exit 1\n"
            )
            qlist.chmod(0o755)

            def run(installed: str) -> str:
                env = dict(os.environ)
                env["PATH"] = f"{fake_bin}:{env['PATH']}"
                env["INSTALLED"] = installed
                preamble = ("emitfail() { echo \"FAIL: $1\" ; exit 1 ; } ; "
                            "emit() { echo \"$1\" ; } ; ")
                proc = subprocess.run(
                    ["/bin/sh", "-c", preamble + fragment],
                    capture_output=True, text=True, env=env,
                    timeout=60, check=False)
                return proc.stdout + proc.stderr

            # every atom present -> the loop passes
            out = run(" ".join(atoms))
            self.assertNotIn("FAIL", out,
                             f"a fully installed app set must pass: {out}")

            # one atom missing -> the loop must fail and name it
            missing = atoms[7]
            out = run(" ".join(a for a in atoms if a != missing))
            self.assertIn("FAIL", out,
                          f"a missing atom must fail the gate: {out}")
            self.assertIn(missing, out,
                          f"the failure must name the missing atom: {out}")

    def test_gentoo_verify_contains_no_vulkaninfo_invocation(self):
        """vulkaninfo touches a display -- a verify block must not run it."""
        script = self._verify_script()
        self.assertNotRegex(
            script, r"(^|[;&|]\s*)vulkaninfo\s",
            "vulkaninfo touches a display -- a verify block must assert the "
            "loader-visible files, not run it",
        )


# --------------------------------------------------------------------------
# M3 -- the simulate gate must not ship the REAL installer.
# --------------------------------------------------------------------------
class TestGentooSimulateDoesNotShipTheRealInstaller(unittest.TestCase):
    """The simulate VM was racing the installer it was supposed to replace.

    The simulate user-data shipped a byte-identical copy of
    gentoo-install.sh, which contains a live
    `emerge --getbinpkg --verbose --update --deep --newuse --with-bdeps=y
    gnome-base/gnome` and a per-atom emerge loop. The simulate gate is
    supposed to validate the PACKAGE RESOLUTION quickly, but the guest was
    simultaneously running the full install, so the simulate VM did not
    model anything -- it raced a multi-hour emerge.
    """

    def _render(self, simulate_only: bool) -> str:
        ctx = render_context("gentoo")
        ctx["simulate_only"] = simulate_only
        return get_jinja_env(REPO / "templates").get_template(
            "gentoo.j2").render(**ctx)

    def test_simulate_render_has_no_real_install_script(self):
        import yaml
        doc = yaml.safe_load(self._render(True))
        paths = [str(wf.get("path")) for wf in doc.get("write_files") or []
                 if isinstance(wf, dict)]
        self.assertNotIn("/usr/local/sbin/gentoo-install.sh", paths,
                         "the simulate user-data must not ship the real "
                         "installer -- it would emerge the whole curated "
                         "set while the simulate gate is supposed to only "
                         "resolve it")
        self.assertNotIn("/etc/systemd/system/gentoo-install.service", paths,
                         "the simulate user-data must not ship the install "
                         "unit")

    def test_simulate_bootcmd_does_not_dispatch_the_installer(self):
        import yaml
        doc = yaml.safe_load(self._render(True))
        for entry in doc.get("bootcmd") or []:
            script = " ".join(str(x) for x in entry)
            self.assertNotIn("gentoo-install.service", script,
                             "a bootcmd entry must not enable the real "
                             "installer in a simulate VM")

    def test_real_render_still_ships_the_installer(self):
        """The gate must not have been 'fixed' by deleting the real path."""
        import yaml
        doc = yaml.safe_load(self._render(False))
        paths = [str(wf.get("path")) for wf in doc.get("write_files") or []
                 if isinstance(wf, dict)]
        self.assertIn("/usr/local/sbin/gentoo-install.sh", paths,
                      "the REAL build must still ship the installer")

    def test_simulate_keeps_the_early_ssh_unit(self):
        """The simulate VM must stay SSH-reachable for the fleet's marker grep.

        The early-ssh.service exists so the guest answers SSH before
        cloud-init's blocking stages finish. A simulate VM that never
        answers SSH cannot be marker-grepped, so the simulate gate would
        fail for the wrong reason.
        """
        import yaml
        doc = yaml.safe_load(self._render(True))
        paths = [str(wf.get("path")) for wf in doc.get("write_files") or []
                 if isinstance(wf, dict)]
        self.assertIn("/etc/systemd/system/early-ssh.service", paths,
                      "the simulate VM must keep the early SSH unit")


# --------------------------------------------------------------------------
# M1 -- the single-token checksum fallback must reject a non-hex token.
# --------------------------------------------------------------------------
class TestVerifyHashSignedSumsFile(unittest.TestCase):
    """Gentoo's .sha256 file is a PGP-signed message.

    The signed file contains, below the digest line, a signature block
    with SEVEN single tokens of exactly 64 characters (the base64 of the
    signature). The old fallback branch accepted a single token on
    LENGTH alone, so it bound a signature line as the expected digest
    and the build died with a bogus "Checksum mismatch (sha256)!".

    The name-matched branch already tested hexness; the fallback must
    apply the same test.
    """

    # A real PGP armored signature block: base64 lines of exactly 64
    # characters, none of them hex. The length is what makes the old
    # length-only fallback bind one of them. (Letters G-Z are used
    # precisely because A-F would be hex digits and would pass the
    # hexness test for the wrong reason.)
    SIGNATURE_TOKENS = [
        "iQIzBAEBCgAdFiEE7" + "x" * 47,
        "G" * 64,
        "H" * 64,
        "J" * 64,
        "K" * 64,
        "R" * 64,
        "T" * 64,
        "W" * 64,
    ]

    SIGNED_SUMS = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n"
        "\n"
        "{digest}  {name}\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "\n"
        "{signature}"
        "=uAYW\n"
        "-----END PGP SIGNATURE-----\n"
    )

    def _signed_sums(self, digest: str, name: str,
                     signature_first: bool = False) -> str:
        signature = "".join(f"{tok}\n" for tok in self.SIGNATURE_TOKENS)
        if not signature_first:
            return self.SIGNED_SUMS.format(digest=digest, name=name,
                                           signature=signature)
        # The signature block comes FIRST -- which is the layout that
        # makes the fallback bind a signature token before the real
        # digest line is ever read.
        return (
            "-----BEGIN PGP SIGNED MESSAGE-----\n"
            "Hash: SHA256\n"
            "\n"
            "-----BEGIN PGP SIGNATURE-----\n\n"
            f"{signature}"
            "=uAYW\n"
            "-----END PGP SIGNATURE-----\n"
            f"{digest}  {name}\n"
        )

    def _verify_with(self, sums_text: str, image: Path) -> None:
        class _FakeResp:
            def __init__(self, data: bytes):
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(
            download, "_urlopen",
            lambda *a, **k: _FakeResp(sums_text.encode()),
        ):
            download.verify_hash(image, alg="sha256",
                                sums_url="https://m/gentoo.sha256")

    def test_signed_sums_file_verifies_the_real_digest(self):
        """A signed sums file must still verify the image it names."""
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "gentoo.img"
            img.write_bytes(b"gentoo-image-bytes")
            digest = hashlib.sha256(img.read_bytes()).hexdigest()
            self._verify_with(self._signed_sums(digest, img.name), img)

    def test_the_signature_tokens_are_64_chars_and_not_hex(self):
        """Guard the fixture: the tokens must be length-matching but
        non-hex -- exactly what breaks a length-only check."""
        for token in self.SIGNATURE_TOKENS:
            self.assertEqual(len(token), 64)
            self.assertFalse(
                all(c in "0123456789abcdef" for c in token.lower()),
                f"{token!r} must not be hex -- the fixture is what makes "
                "the test discriminate",
            )

    def test_single_token_fallback_rejects_a_non_hex_token(self):
        """A sums file with ONLY a non-hex 64-char token must fail closed.

        This is the branch that was broken: the file has no
        name-matched entry, so the fallback ran, and the fallback used
        to accept any 64-character token.

        The distinction that matters is WHICH failure is reported. The
        broken fallback bound the token as the expected digest and then
        reported "Checksum mismatch (sha256)!" -- a claim that the
        download is corrupt, sending the operator to inspect the mirror
        instead of the checksum file. The fixed behaviour is to report
        that no usable entry was found at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "gentoo.img"
            img.write_bytes(b"gentoo-image-bytes")
            bogus = "G" * 64
            with self.assertRaises(RuntimeError) as ctx:
                self._verify_with(f"{bogus}\n", img)
            self.assertNotIn(
                "Checksum mismatch", str(ctx.exception),
                "a non-hex token was bound as the expected digest -- the "
                "build would report a bogus checksum mismatch on a file "
                "whose checksum may be perfectly fine",
            )
            self.assertIn(
                "No SHA256 entry", str(ctx.exception),
                "the fallback must fail closed with 'no usable entry'",
            )

    def test_signature_before_the_digest_is_not_bound_as_the_digest(self):
        """The live failure: a signature token seen FIRST wins the race.

        The sums parser walks the file top to bottom and stops at the
        first usable digest. A PGP signature block that precedes the
        digest line contributes single 64-character tokens; a
        length-only fallback binds one of them as `expected` and breaks
        out of the loop BEFORE the real digest line is ever reached.
        The build then dies with a checksum mismatch on a correctly
        downloaded image whose checksum is in the very same file.

        With the hexness test in place the signature tokens are skipped
        and the real digest -- two lines further down -- is found, so
        the verification SUCCEEDS.
        """
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "gentoo.img"
            img.write_bytes(b"gentoo-image-bytes")
            digest = hashlib.sha256(img.read_bytes()).hexdigest()
            sums = self._signed_sums(digest, img.name, signature_first=True)
            self.assertLess(
                sums.index("-----BEGIN PGP SIGNATURE-----"),
                sums.index(digest),
                "the fixture must put the signature block first",
            )
            try:
                self._verify_with(sums, img)
            except RuntimeError as exc:
                self.fail(
                    f"a signed sums file with the signature block first must "
                    f"still verify, but the verification failed: {exc}"
                )


# --------------------------------------------------------------------------
# L1 -- the seed ISO carries the cleartext passwords; it must not be
#      world-readable on the host.
# --------------------------------------------------------------------------
class TestSeedIsoIsNotWorldReadable(unittest.TestCase):
    """The NoCloud seed ISO contains the cleartext user+root passwords.

    install-info.txt and the SSH key are created 0600, but the ISO was
    written with the default umask and landed 0644 -- next to the qcow2
    that holds the same secrets.
    """

    def test_build_seed_iso_leaves_the_iso_at_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.iso"
            templates.build_seed_iso(
                seed,
                "#cloud-config\npassword: hunter2\n",
                "instance-id: i\n",
                REPO / "templates",
            )
            mode = seed.stat().st_mode & 0o777
            self.assertEqual(
                mode, 0o600,
                f"the seed ISO carries the cleartext passwords and must not "
                f"be world-readable (mode is {mode:04o})",
            )

    def test_seed_iso_really_contains_the_password(self):
        """Guard the fixture: the ISO must actually carry the secret."""
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.iso"
            templates.build_seed_iso(
                seed,
                "#cloud-config\npassword: hunter2\n",
                "instance-id: i\n",
                REPO / "templates",
            )
            raw = seed.read_bytes()
            self.assertIn(b"hunter2", raw,
                          "the fixture is wrong: the ISO should contain the "
                          "cleartext password")


# --------------------------------------------------------------------------
# L2 -- no version-specific python3.X-venv pin.
# --------------------------------------------------------------------------
class TestUbuntuPythonVenvIsNotVersionPinned(unittest.TestCase):
    """`python3.14-venv` only exists in the releases that ship Python 3.14.

    The image resolver tracks the NEWEST supported LTS, so a version pin
    is a landmine: the moment the resolver moves to an LTS with a
    different Python, the package does not exist and the build dies at
    package install. The version-independent `python3-venv` is present
    in every release (it is a transitional package that depends on the
    release's own python3.X-venv).
    """

    def _ubuntu(self) -> str:
        return get_jinja_env(REPO / "templates").get_template(
            "ubuntu.j2").render(**render_context("ubuntu-lts"))

    def test_ubuntu_does_not_pin_a_version_specific_venv(self):
        rendered = self._ubuntu()
        pinned = re.findall(r"python3\.\d+-venv", rendered)
        self.assertEqual(
            pinned, [],
            "the venv package must not be pinned to a Python version -- the "
            "image resolver tracks the newest LTS, whose Python version "
            "changes independently of this repo",
        )

    def test_ubuntu_uses_the_version_independent_venv(self):
        self.assertIn("python3-venv", self._ubuntu(),
                      "the version-independent python3-venv must be the "
                      "package that gets installed")

    def test_audit_category_does_not_list_a_version_pinned_venv(self):
        import runpy
        ns = runpy.run_path(str(REPO / "scripts" / "audit-packages.py"),
                            run_name="audit_not_main")
        tokens = ns["CATEGORIES"].get("Python pip") or []
        pinned = [t for t in tokens if re.match(r"python3\.\d+-venv$", t)]
        self.assertEqual(
            pinned, [],
            "the audit must not accept a version-pinned venv as a "
            "satisfied category -- it would make the gate green for a "
            "package the next LTS may not have",
        )


# --------------------------------------------------------------------------
# L4 -- linux_vm/monitor.py had no test coverage at all.
# --------------------------------------------------------------------------
class TestMonitorConsoleScanner(unittest.TestCase):
    """The monitor's incremental console scanner is the one part of
    linux_vm/ that no test touched.

    It is also the part with the most subtle behaviour: it reads an
    append-only file in chunks with an overlap, and it must not lose a
    marker that straddles a chunk boundary, nor keep stale phase state
    when a new build truncates the log.
    """

    def _scanner(self, path: Path, overlap: int = 8192):
        from linux_vm.monitor import _ConsoleScanner
        return _ConsoleScanner(path, overlap=overlap)

    def test_missing_file_is_not_fatal(self):
        """A console.log that does not exist yet must report False, not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            scanner = self._scanner(Path(tmp) / "console.log")
            self.assertFalse(scanner.scan())

    def test_phases_are_detected_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            log.write_text("Linux version 6.8.0\n")
            scanner = self._scanner(log)
            self.assertTrue(scanner.scan())
            self.assertTrue(scanner.saw_kernel)
            self.assertEqual(sum(scanner.phases), 0)

            with log.open("a") as fh:
                fh.write("cloud-init[123]: Cloud-init v. 25.1 running 'init-local'\n")
            self.assertTrue(scanner.scan())
            self.assertTrue(scanner.phases[0],
                            "the init-local phase must be detected once it "
                            "arrives")

    def test_a_marker_straddling_the_chunk_boundary_is_not_lost(self):
        """The overlap read is what makes this work.

        The scanner resumes at `offset - overlap` rather than at `offset`,
        so a marker whose head was in the previous chunk and whose tail
        arrives in the next one is still seen whole. Without the overlap
        the tail alone would not match the phase pattern and the phase
        would be reported as never reached.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            marker = ("cloud-init[1]: Cloud-init v. 25.1 running "
                      "'modules:final'")
            # First write ends with the HEAD of the marker.
            log.write_text("x" * 200 + marker[:40])
            scanner = self._scanner(log, overlap=64)
            self.assertTrue(scanner.scan())
            self.assertFalse(scanner.phases[3],
                             "the marker is incomplete -- it must not match yet")
            # The TAIL arrives in the next chunk.
            with log.open("a") as fh:
                fh.write(marker[40:])
            self.assertTrue(scanner.scan())
            self.assertTrue(
                scanner.phases[3],
                "a phase marker split across the read boundary must still be "
                "found -- that is what the overlap read is for",
            )

    def test_truncation_resets_the_state(self):
        """A new build reopens console.log from scratch.

        Keeping the old phase flags would make the monitor report
        'setup complete' for a VM that has not even booted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            log.write_text(
                "cloud-init[1]: Cloud-init v. 25.1 running 'init-local'\n")
            scanner = self._scanner(log)
            self.assertTrue(scanner.scan())
            self.assertTrue(scanner.phases[0])

            log.write_text("booting\n")  # truncated / new build
            self.assertTrue(scanner.scan())
            self.assertFalse(any(scanner.phases),
                             "a truncated log must clear the phase state")
            self.assertFalse(scanner.saw_kernel)

    def test_progress_bar_shape(self):
        from linux_vm.monitor import _draw_progress_bar
        bar = _draw_progress_bar(50, "half")
        self.assertIn("50%", bar)
        self.assertIn("#" * 20, bar)
        self.assertIn("-" * 20, bar)


class TestMonitorMain(unittest.TestCase):
    """monitor_main's argument handling and the missing-log exit code."""

    def test_missing_console_log_exits_1(self):
        from linux_vm.monitor import monitor_main
        with tempfile.TemporaryDirectory() as tmp:
            rc = monitor_main([str(Path(tmp) / "nope")])
            self.assertEqual(rc, 1,
                             "a missing console.log must be an error exit, "
                             "not a silent success")

    def test_once_mode_prints_a_snapshot_and_exits_0(self):
        from linux_vm.monitor import monitor_main
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            log.write_text("Linux version 6.8.0\n")
            rc = monitor_main([str(tmp), "--once"])
            self.assertEqual(rc, 0)

    def test_completed_cloud_init_exits_0(self):
        from linux_vm.monitor import monitor_main
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "console.log"
            log.write_text(
                "cloud-init[1]: Cloud-init v. 25.1 running 'init-local'\n"
                "cloud-init[1]: Cloud-init v. 25.1 running 'modules:final'\n"
                # The marker the template actually emits to the serial
                # console (templates/_base.j2 writes it to /dev/ttyS0).
                "GNOME setup complete.\n"
            )
            rc = monitor_main([str(tmp), "--once"])
            self.assertEqual(rc, 0)

    def test_corrupt_ssh_key_does_not_crash_monitor_ssh(self):
        from linux_vm.monitor import _try_ssh_with_key
        with tempfile.TemporaryDirectory() as tmp:
            bad_key = Path(tmp) / "ssh_key"
            bad_key.write_text("NOT A VALID PRIVATE KEY", encoding="utf-8")
            res = _try_ssh_with_key(Path(tmp), "user")
            self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
