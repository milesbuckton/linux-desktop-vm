"""Unit tests for the pure host-side logic (no network, no VMs).

Run from repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import lzma
import re
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

from linux_vm.config import DISTRO_DEFAULTS, VMConfig, filename_from_url
from linux_vm.host import (
    guest_arch_for_host,
    recommended_memory_mb,
    recommended_vcpus,
)
from linux_vm import download, log, qemu

class TestRecommendedResources(unittest.TestCase):
    def test_vcpus_clamped_low(self):
        self.assertEqual(recommended_vcpus(2), 2)
        self.assertEqual(recommended_vcpus(4), 2)

    def test_vcpus_halved_and_capped(self):
        self.assertEqual(recommended_vcpus(18), 8)   # 9 -> cap 8
        self.assertEqual(recommended_vcpus(10), 5)

    def test_vcpus_unknown_host(self):
        with mock.patch("linux_vm.host.physical_cpu_count", return_value=None):
            self.assertEqual(recommended_vcpus(None), 4)

    def test_memory_clamps(self):
        self.assertEqual(recommended_memory_mb(8192), 8192)     # floor wins over halving
        self.assertEqual(recommended_memory_mb(4096), 8192)     # raised to floor
        self.assertEqual(recommended_memory_mb(16384), 8192)    # halved
        self.assertEqual(recommended_memory_mb(32768), 16384)   # halved
        self.assertEqual(recommended_memory_mb(65536), 32768)   # cap
        self.assertEqual(recommended_memory_mb(131072), 32768)  # capped

    def test_memory_unknown_host(self):
        with mock.patch("linux_vm.host.host_memory_mb", return_value=None):
            self.assertEqual(recommended_memory_mb(None), 16384)

class TestGuestArch(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(guest_arch_for_host("arm64"), "aarch64")
        self.assertEqual(guest_arch_for_host("x86_64"), "x86_64")
        self.assertEqual(guest_arch_for_host("AMD64"), "x86_64")

    def test_unsupported_raises(self):
        with self.assertRaises(ValueError):
            guest_arch_for_host("sparc")

class TestFilenameFromUrl(unittest.TestCase):
    def test_last_segment(self):
        self.assertEqual(
            filename_from_url("https://mirror.example/dir/noble-server-cloudimg-arm64.img"),
            "noble-server-cloudimg-arm64.img",
        )

    def test_query_string_ignored(self):
        self.assertEqual(filename_from_url("https://m.example/a.img?tok=1"), "a.img")

class TestVerifyHash(unittest.TestCase):
    def _make_image(self, tmp: str, payload: bytes = b"image-bytes") -> Path:
        p = Path(tmp) / "test-image.img"
        p.write_bytes(payload)
        return p

    def test_hex_digest_match(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            img = self._make_image(tmp)
            expected = hashlib.sha256(img.read_bytes()).hexdigest()
            download.verify_hash(img, alg="sha256", hex_digest=expected)

    def test_hex_digest_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._make_image(tmp)
            with self.assertRaises(RuntimeError):
                download.verify_hash(img, alg="sha256", hex_digest="0" * 64)

    def _sums_lookup(self, sums_text: str, image: Path):
        """Drive verify_hash()'s sums-file branch with a fake _urlopen."""
        class _FakeResp(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        with mock.patch.object(
            download, "_urlopen",
            lambda *a, **k: _FakeResp(sums_text.encode()),
        ):
            download.verify_hash(image, alg="sha256", sums_url="https://m/SUMS")

    def test_sums_binary_mode_star(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            img = self._make_image(tmp)
            digest = hashlib.sha256(img.read_bytes()).hexdigest()
            # sha256sum --check binary-mode format: "<hex> *<name>"
            self._sums_lookup(f"{digest} *{img.name}\n", img)

    def test_sums_with_path_prefix(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            img = self._make_image(tmp, b"other-payload")
            digest = hashlib.sha256(img.read_bytes()).hexdigest()
            self._sums_lookup(f"{digest}  ./sub/dir/{img.name}\n", img)

    def test_sums_substring_name_must_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "cloudimg.img"
            img.write_bytes(b"x")
            # A sums entry for a DIFFERENT image whose name merely CONTAINS
            # ours must not be picked up: with no matching entry the lookup
            # must fail closed (RuntimeError), not bind the wrong digest.
            bogus = "f" * 64
            with self.assertRaises(RuntimeError):
                self._sums_lookup(f"{bogus}  not-{img.name}\n", img)

    def test_sums_similar_name_ignored_correct_entry_used(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "cloudimg.img"
            img.write_bytes(b"payload")
            digest = hashlib.sha256(img.read_bytes()).hexdigest()
            # A similarly-named entry listed BEFORE ours must be skipped;
            # the correct entry's digest must be the one bound.
            self._sums_lookup(
                f"{'e' * 64}  not-{img.name}\n{digest}  {img.name}\n", img
            )

    def test_sums_missing_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = self._make_image(tmp)
            with self.assertRaises(RuntimeError):
                self._sums_lookup("aaaa  some-other-file.img\n", img)

class TestExtractArchiveSafely(unittest.TestCase):
    def _xz_tar(self, members: dict[str, bytes]) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tf:
            for name, data in members.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                import io as _io
                tf.addfile(ti, _io.BytesIO(data))
        return lzma.compress(raw.getvalue())

    def test_path_traversal_rejected(self):
        from linux_vm.orchestrate import _extract_archive_safely
        blob = self._xz_tar({"../evil.txt": b"pwned"})
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "img.tar.xz"
            archive.write_bytes(blob)
            dest = Path(tmp) / "out"
            dest.mkdir()
            with self.assertRaises(RuntimeError):
                _extract_archive_safely(archive, dest)
            self.assertFalse((dest.parent / "evil.txt").exists())

    def test_symlink_write_through_rejected(self):
        from linux_vm.orchestrate import _extract_archive_safely
        # A benign-named symlink member followed by a member written THROUGH
        # it must be rejected: the name check alone passes both, but the
        # pair escapes target_dir. Regression guard for the extraction
        # hardening (symlink/hardlink members are refused outright).
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tf:
            link = tarfile.TarInfo("escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc"
            tf.addfile(link)
            payload = tarfile.TarInfo("escape/pwned.txt")
            payload.size = len(b"pwned")
            tf.addfile(payload, io.BytesIO(b"pwned"))
        blob = lzma.compress(raw.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "img.tar.xz"
            archive.write_bytes(blob)
            dest = Path(tmp) / "out"
            dest.mkdir()
            with self.assertRaises(RuntimeError):
                _extract_archive_safely(archive, dest)
            self.assertFalse((dest / "escape").exists())

    def test_safe_members_extracted(self):
        from linux_vm.orchestrate import _extract_archive_safely
        blob = self._xz_tar({"disk.raw": b"diskdata"})
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "img.tar.xz"
            archive.write_bytes(blob)
            dest = Path(tmp) / "out"
            dest.mkdir()
            _extract_archive_safely(archive, dest)
            self.assertEqual((dest / "disk.raw").read_bytes(), b"diskdata")

class TestRenderLauncher(unittest.TestCase):
    def _cfg(self, target: Path, ssh_port=None) -> VMConfig:
        return VMConfig(
            vm_name="Test VM",
            hostname="test-vm",
            username="tester",
            password="pw",
            root_password="root",
            vcpus=4,
            memory_mb=8192,
            disk_gb=80,
            timezone="UTC",
            target_dir=target,
            ssh_port=ssh_port,
        )

    def _tools(self, tmp: str) -> qemu.QemuTools:
        # Nonexistent qemu-system makes every _qemu_supports probe fail-open
        # to True (the documented conservative default), so no real QEMU is
        # needed. aarch64 hard-errors without OVMF firmware, so point the
        # tool paths at tiny real files. The OVMF names below MUST match
        # the discovery fallback order in linux_vm.qemu
        # (edk2-aarch64-code.fd + edk2-arm-vars.fd for aarch64) -- if the
        # firmware names there change, update here too.
        ovmf_code = Path(tmp) / "edk2-aarch64-code.fd"
        ovmf_vars = Path(tmp) / "edk2-arm-vars.fd"
        ovmf_code.write_bytes(b"code")
        ovmf_vars.write_bytes(b"vars")
        return qemu.QemuTools(qemu_system=Path("/nonexistent/qemu-system-aarch64"),
                              guest_arch="aarch64",
                              ovmf_code=ovmf_code,
                              ovmf_vars=ovmf_vars)

    def _render(self, tmp: str, ssh_port=None) -> str:
        cfg = self._cfg(Path(tmp), ssh_port)
        with mock.patch.object(qemu, "_ensure_qemu_app", lambda q: q):
            return qemu.render_launcher(cfg, "efi", self._tools(tmp))

    def test_port_placeholder_injected_when_no_ssh_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._render(tmp)
            self.assertIn("$sshFwdPort", script)
            self.assertIn("hostfwd=tcp:127.0.0.1:", script)
            self.assertNotIn(qemu._SSH_PORT_PLACEHOLDER, script)

    def test_explicit_port_used_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._render(tmp, ssh_port=2300)
            self.assertIn("tcp:127.0.0.1:2300-:22", script)
            self.assertNotIn("sshFwdPort", script)

    def test_args_single_quoted_and_execd(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._render(tmp)
            lines = script.splitlines()
            self.assertEqual(lines[0], "#!/usr/bin/env bash")
            self.assertTrue(any(l == "exec \\" for l in lines))
            # Every argv line is single-quoted shell
            arg_lines = [l for l in lines[lines.index("exec \\") + 1:] if l.strip()]
            for l in arg_lines:
                self.assertTrue(l.strip().startswith("'"), f"unquoted argv line: {l}")

    def test_seed_attached_virtio_on_aarch64(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._render(tmp)
            self.assertIn("virtio-blk-pci,drive=cd0", script)
            self.assertNotIn("ide-cd", script)

class TestLogColour(unittest.TestCase):
    def test_log_always_uses_ansi(self):
        """log() always emits ANSI codes (colour is controlled by the caller's
        terminal, not by the log() function). Verify it produces output."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            log.log("hello", "ok")
        out = buf.getvalue()
        self.assertIn("[ ok ]", out)

class TestMarkerContract(unittest.TestCase):
    def test_marker_constant_matches_template_macro(self):
        macro = (REPO / "templates" / "_macros.j2").read_text(encoding="utf-8")
        from linux_vm.fleet.constants import VERIFY_OK_MARKER
        self.assertIn(VERIFY_OK_MARKER, macro)

class TestGnomeExtUrlWiring(unittest.TestCase):
    """Guard the orchestrator <-> template contract for the host-served
    GNOME extension URL.

    Regression: the template read GNOME_EXT_BASE_URL while the orchestrator
    rendered GNOME_EXT_URL, so the fleet's host-served URL silently rendered
    empty and guests always fell back to GitHub. default('') hid the
    mismatch from every gate (StrictUndefined never fired).
    """

    def _render(self, **ctx):
        from linux_vm.templates import get_jinja_env
        env = get_jinja_env(REPO / "templates")
        return env.get_template("_gs_extensions_common.j2").render(**ctx)

    def test_fleet_url_lands_in_rendered_runcmd(self):
        url = "http://10.0.2.2:8753"
        out = self._render(GNOME_EXT_URL=url)
        self.assertIn(url, out)

    def test_unset_renders_empty_without_error(self):
        # StrictUndefined + default(''): an unset variable must render the
        # GitHub-fallback path, not raise -- and must not leak the variable
        # name into the shell payload.
        out = self._render()
        self.assertNotIn("GNOME_EXT", out)

class TestPlainPasswordOnUserEntry(unittest.TestCase):
    """Guard the `degraded done` fix.

    Regression: `_base.j2` set `lock_passwd: false` but only supplied the
    password via the top-level `chpasswd:` block. cloud-init's
    `distros/__init__.py` checks `lock_passwd:false` against
    `passwd`/`plain_text_passwd`/`hashed_passwd` **on the user entry**, finds
    none, and logs a WARN -- and `cmd/status.py` marks the whole run DEGRADED
    on any WARN. So every successful Ubuntu build reported `degraded done`.

    The password must therefore be on the user entry itself.
    """

    def _render(self, template="ubuntu.j2", **ctx):
        # Use the shared render_context() (single source of truth with
        # lint/audit/runtime) rather than a hand-rolled dict -- a mismatch
        # there is exactly the drift AGENTS.md warns about.
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        distro = template.replace(".j2", "")
        base = render_context(distro)
        base.update(ctx)
        env = get_jinja_env(REPO / "templates")
        return env.get_template(template).render(**base)

    def _users_entry(self, rendered: str) -> dict:
        import yaml
        return yaml.safe_load(rendered)["users"][0]

    def test_plain_text_passwd_is_a_sibling_of_lock_passwd(self):
        entry = self._users_entry(self._render())
        self.assertIn("plain_text_passwd", entry)
        self.assertIn("lock_passwd", entry)
        self.assertFalse(entry["lock_passwd"])
        # It must sit on the user entry, not only in chpasswd:, because that
        # is the exact place create_user() looks.
        self.assertEqual(entry["plain_text_passwd"], "x")

    def test_chpasswd_still_sets_both_passwords(self):
        import yaml
        cfg = yaml.safe_load(self._render())
        names = [u["name"] for u in cfg["chpasswd"]["users"]]
        self.assertEqual(names, ["testuser", "root"])

    def test_both_templates_carry_the_fix(self):
        for template in ("ubuntu.j2", "gentoo.j2"):
            with self.subTest(template=template):
                entry = self._users_entry(self._render(template=template))
                self.assertIn("plain_text_passwd", entry)

    def test_password_with_yaml_metachars_still_parses(self):
        # PASSWORD is user-overridable via --password, so it must survive
        # every value a plain (unquoted) YAML scalar would mangle. These are
        # not hypothetical: `1234` -> int and `yes` -> bool both fail
        # cloud-init's schema with "not of type 'string'", which the guest's
        # own `cloud-init schema --config-file` rejects outright.
        import yaml
        for pw in ("ubuntu", "p@ss:word", "yes", "no", "1234", "1.5",
                   "a#b", "null", "true", "it's", 'a"b', "back\\slash",
                   "- leading dash", "[brackets]", "{curly}"):
            with self.subTest(pw=pw):
                cfg = yaml.safe_load(self._render(PASSWORD=pw))
                # str, not bool/int/float/None -- schema requires a string.
                self.assertIs(type(cfg["users"][0]["plain_text_passwd"]), str)
                self.assertEqual(cfg["users"][0]["plain_text_passwd"], pw)

    def test_chpasswd_passwords_round_trip_too(self):
        # The pre-existing chpasswd: entries had the identical bug; the fix
        # must cover them or `--password 1234` still fails schema validation.
        import yaml
        for pw in ("ubuntu", "1234", "yes", "it's"):
            with self.subTest(pw=pw):
                cfg = yaml.safe_load(self._render(PASSWORD=pw))
                for entry in cfg["chpasswd"]["users"]:
                    if entry["name"] == "testuser":
                        self.assertIs(type(entry["password"]), str)
                        self.assertEqual(entry["password"], pw)
                self.assertIs(type(cfg["users"][0]["plain_text_passwd"]), str)

class TestSnapshotCloudInitStatus(unittest.TestCase):
    """Guard the post-build status.json snapshot.

    `/var/lib/cloud/data/status.json` is rewritten on every boot, so the
    `recoverable_errors` behind a `degraded` verdict vanish as soon as the VM
    is re-booted to inspect it. The orchestrator must capture it while the
    guest is still up, and must not stay silent when it is non-empty.
    """

    STATUS = """{
  "v1": {
    "datasource": "DataSourceNoCloud [seed=/dev/vdb]",
    "init": {"errors": [], "finished": 5.58, "recoverable_errors": {}, "start": 5.36},
    "modules-config": {"errors": [], "recoverable_errors": {}, "finished": 9.38, "start": 9.31},
    "modules-final": {"errors": [], "recoverable_errors": {}, "finished": 21.95, "start": 21.86},
    "stage": null
  }
}"""

    DEGRADED = """{
  "v1": {
    "datasource": "DataSourceNoCloud [seed=/dev/vdb]",
    "init": {"errors": [], "recoverable_errors": {"WARNING": [
      "Not unlocking password for user ubuntu. 'lock_passwd: false' present in user-data but no 'passwd'/'plain_text_passwd'/'hashed_passwd' provided in user-data"
    ]}, "finished": 5.58, "start": 5.36},
    "stage": null
  }
}"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)
        self.wait_log = self.target / "ubuntu-lts.wait.log"
        self.wait_log.write_text("", encoding="utf-8")

    def _run(self, payload: str, rc: int = 0):
        from linux_vm.fleet import ssh as fleet_ssh
        with mock.patch.object(
            fleet_ssh, "run_with_hard_timeout", return_value=(rc, payload, "")
        ), mock.patch.object(fleet_ssh, "log_master") as master:
            fleet_ssh._snapshot_cloud_init_status(
                "127.0.0.1", 2222, "ubuntu", self.target / "ssh_key",
                self.wait_log, self.target,
            )
        return master

    def test_writes_raw_status_and_stays_quiet_when_clean(self):
        master = self._run(self.STATUS)
        dest = self.target / "cloud-init-status.json"
        self.assertTrue(dest.exists(), "raw status.json must be preserved")
        import json
        self.assertEqual(json.loads(dest.read_text())["v1"]["datasource"],
                         "DataSourceNoCloud [seed=/dev/vdb]")
        log = self.wait_log.read_text(encoding="utf-8")
        self.assertIn("cloud-init status snapshot", log)
        self.assertIn("errors=0", log)
        # A clean build must not cry wolf.
        master.assert_not_called()

    def test_surfaces_recoverable_errors_loudly(self):
        master = self._run(self.DEGRADED)
        log = self.wait_log.read_text(encoding="utf-8")
        # The evidence itself, line by line, so a post-mortem needs no VM.
        self.assertIn("WARNING: init: Not unlocking password for user ubuntu", log)
        self.assertIn("recoverable={WARNING:1}", log)
        # And it must be visible in the master log, not buried in a file.
        master.assert_called_once()
        self.assertIn("WARN:", master.call_args[0][0])
        self.assertIn("degraded", master.call_args[0][0])

    def test_strips_ssh_noise_before_json(self):
        payload = (
            "Warning: Permanently added '[127.0.0.1]:2222' (ED25519) to the list of known hosts.\n"
            + self.STATUS
        )
        master = self._run(payload)
        self.assertTrue((self.target / "cloud-init-status.json").exists())
        master.assert_not_called()

    def test_ssh_failure_never_raises(self):
        # Best-effort evidence capture: a dead guest must not mask the verdict.
        from linux_vm.fleet import ssh as fleet_ssh
        with mock.patch.object(
            fleet_ssh, "run_with_hard_timeout", return_value=(-99, "", "timeout")
        ):
            fleet_ssh._snapshot_cloud_init_status(
                "127.0.0.1", 2222, "ubuntu", self.target / "ssh_key",
                self.wait_log, self.target,
            )
        self.assertIn("no status.json captured", self.wait_log.read_text(encoding="utf-8"))
        self.assertFalse((self.target / "cloud-init-status.json").exists())

    def test_unparseable_json_is_reported_not_treated_as_clean(self):
        """An unreadable status must never be summarised as a clean build.

        This test used to pin the bug: it asserted `{not json` produced
        "cloud-init status snapshot" AND "errors=0", i.e. that a parse failure
        fell through to v1={} and was reported with the same summary as a
        perfectly healthy guest -- and that garbage bytes overwrote whatever
        good snapshot was on disk. Unreadable must now read as UNKNOWN.
        """
        self._run("{not json")
        log = self.wait_log.read_text(encoding="utf-8")
        self.assertIn("UNPARSEABLE", log)
        self.assertIn("status UNKNOWN", log)
        self.assertNotIn("errors=0", log)
        # Raw bytes kept for forensics, but never on the good path's filename.
        self.assertTrue((self.target / "cloud-init-status.json.unparsed").exists())
        self.assertFalse((self.target / "cloud-init-status.json").exists())

    def test_wrapper_snapshots_on_every_exit_path(self):
        """The public wrapper must snapshot even when the wait times out."""
        from linux_vm.fleet import ssh as fleet_ssh
        with mock.patch.object(
            fleet_ssh, "_ssh_wait_cloud_init", return_value=(-1, 999.0)
        ), mock.patch.object(
            fleet_ssh, "_snapshot_cloud_init_status"
        ) as snap:
            rc, elapsed = fleet_ssh.ssh_wait_cloud_init(
                "127.0.0.1", 2222, "ubuntu", self.target / "ssh_key",
                self.wait_log, 60, target_dir=self.target,
            )
        self.assertEqual((rc, elapsed), (-1, 999.0))
        snap.assert_called_once()

class TestDashToDockInstallGate(unittest.TestCase):
    """Guard the dash-to-dock install: multi-line metadata check + per-distro gate.

    Regression (found by booting the built VMs and reading their markers):
    `GS-EXT-SKIPPED: dash-to-dock ... does not declare GNOME NN support` was
    emitted on EVERY distro even for a release that declares 45-51 (incl.
    50/51). Two independent bugs made the check unable to match anything:

      1. grep is line-based, and upstream ships pretty-printed metadata.json
         (``"shell-version": [`` on one line, each version on its own), so the
         version could never appear on the same line as the key;
      2. the pattern was single-quoted, so bash never expanded ``$SVER`` --
         grep searched for a literal "$SVER", where "$" is an EOL anchor.

    Consequence: dconf enabled a dash-to-dock that was never installed. On
    Gentoo (no session-mode dock) the desktop shipped with NO dock at all;
    on Ubuntu the stock ubuntu-dock masked it.

    So the check is gated per distro: Ubuntu keeps its stock ubuntu-dock
    (force-enabled by /usr/share/gnome-shell/modes/ubuntu.json) and must
    neither install nor enable dash-to-dock, while Gentoo must do both.

    These tests EXECUTE the rendered shell condition against fixtures instead
    of pattern-matching template text -- static assertions of the old form
    would have passed while the check still could never match.
    """

    UPSTREAM_METADATA = (
        '{\n'
        '"shell-version": [\n'
        '    "45",\n    "46",\n    "47",\n    "48",\n    "49",\n    "50",\n    "51"\n'
        '],\n'
        '"uuid": "dash-to-dock@micxgx.gmail.com",\n'
        '"version": 109\n'
        '}\n'
    )

    def _render(self, template="gentoo.j2", **ctx):
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        distro = template.replace(".j2", "")
        base = render_context(distro)
        base.update(ctx)
        env = get_jinja_env(REPO / "templates")
        return env.get_template(template).render(**base)

    def _runcmd(self, rendered):
        import yaml
        return yaml.safe_load(rendered)["runcmd"]

    def _install_script(self, rendered):
        """Return the runcmd entry that installs dash-to-dock, or None."""
        for item in self._runcmd(rendered):
            if isinstance(item, list) and len(item) >= 3 and item[1] == "-c":
                if "install_ext micheleg/dash-to-dock" in item[2]:
                    return item[2]
        return None

    def test_install_log_redirect_follows_the_run_once_guard(self):
        """gentoo-install.sh must not truncate its log before skipping.

        Regression: `exec >/var/log/gentoo-install.log` ran before the
        done-marker guard, so every subsequent boot destroyed the build's
        install-time markers (MESA-FORK-OK, GLOBAL-ENABLE-OK, ...) -- the same
        evidence-loss class as the status.json scar.
        """
        import yaml
        doc = yaml.safe_load(self._render("gentoo.j2"))
        content = next(
            f["content"] for f in doc["write_files"]
            if f.get("path") == "/usr/local/sbin/gentoo-install.sh"
        )
        guard = content.find("if [ -e /var/lib/gentoo-install-done ]")
        redirect = content.find("exec >/var/log/gentoo-install.log")
        self.assertNotEqual(guard, -1)
        self.assertNotEqual(redirect, -1)
        self.assertLess(guard, redirect, "run-once guard must precede the log redirect")
        # A real (re)install run must preserve the previous attempt's log.
        self.assertIn("gentoo-install.log.prev", content)

    def test_check_matches_declared_versions(self):
        cond = self._check_condition(self._install_script(self._render()))
        for sver in ("45", "49", "50", "51"):
            self.assertEqual(
                self._run_check(cond, self.UPSTREAM_METADATA, sver), "MATCH",
                f"declared version {sver} must match",
            )

    def test_check_rejects_undeclared_and_prefix_versions(self):
        cond = self._check_condition(self._install_script(self._render()))
        # Not declared by the release at all.
        for sver in ("44", "99"):
            self.assertEqual(
                self._run_check(cond, self.UPSTREAM_METADATA, sver), "NOMATCH",
                f"undeclared version {sver} must not match",
            )
        # "5" must not prefix-match the declared "50" (and "60" must not match
        # a release whose list stops at 51).
        self.assertEqual(self._run_check(cond, self.UPSTREAM_METADATA, "5"), "NOMATCH")
        self.assertEqual(self._run_check(cond, self.UPSTREAM_METADATA, "60"), "NOMATCH")

    def test_minor_style_declarations_match(self):
        """A "50.1"-style entry still counts as declaring 50."""
        cond = self._check_condition(self._install_script(self._render()))
        metadata = '{\n"shell-version": ["49", "50.1"],\n"uuid": "x@y"\n}\n'
        self.assertEqual(self._run_check(cond, metadata, "50"), "MATCH")

    def test_release_that_really_lacks_the_version_is_rejected(self):
        cond = self._check_condition(self._install_script(self._render()))
        metadata = '{\n"shell-version": ["45", "46"],\n"uuid": "x@y"\n}\n'
        self.assertEqual(self._run_check(cond, metadata, "50"), "NOMATCH")

    def _dconf_script(self, rendered):
        """Return the runcmd entry that writes 05-extensions (post-YAML text)."""
        for item in self._runcmd(rendered):
            if isinstance(item, list) and len(item) >= 3 and item[1] == "-c":
                if "05-extensions" in item[2]:
                    return item[2]
        return None

    def _enabled_extensions(self, rendered):
        import re
        m = re.search(r"enabled-extensions=(\[[^\]]*\])", self._dconf_script(rendered))
        self.assertIsNotNone(m, "dconf entry must set enabled-extensions")
        return m.group(1)

    def test_gentoo_installs_and_enables(self):
        rendered = self._render("gentoo.j2")
        self.assertIsNotNone(self._install_script(rendered))
        self.assertIn("dash-to-dock@micxgx.gmail.com", self._enabled_extensions(rendered))
        self.assertIn("user-theme@gnome-shell-extensions.gcampax.github.com",
                      self._enabled_extensions(rendered))

    def test_ubuntu_skips_install_and_keeps_stock_dock(self):
        rendered = self._render("ubuntu.j2")
        self.assertIsNone(
            self._install_script(rendered),
            "Ubuntu must not install dash-to-dock (stock ubuntu-dock is "
            "session-mode enabled; installing both renders two docks)",
        )
        # Truthful marker so the skip is visible in cloud-init-output.log.
        self.assertIn("GS-EXT-SKIPPED: dash-to-dock@micxgx.gmail.com", rendered)
        # dconf must not enable an extension Ubuntu doesn't install; User
        # Themes (ships in gnome-shell-extensions) stays enabled.
        enabled = self._enabled_extensions(rendered)
        self.assertNotIn("dash-to-dock", enabled)
        self.assertIn("user-theme@gnome-shell-extensions.gcampax.github.com", enabled)

    def test_rendered_condition_is_exactly_the_working_form(self):
        """Pin the post-YAML program text -- this is where the bugs lived."""
        script = self._install_script(self._render())
        self.assertIsNotNone(script, "gentoo must render the install entry")
        expected = (
            r'''tr -d '\n' < "$STAGE/$uuid/metadata.json" | grep -qE '''
            r'''"\"shell-version\"[^]]*\"$SVER([.][0-9]+)?\""'''
        )
        self.assertEqual(self._check_condition(script), expected)

    def _check_condition(self, script):
        import re
        return re.search(r"if (tr -d .*?); then", script, re.S).group(1)

    def _run_check(self, condition, metadata, sver):
        import subprocess
        stage = Path(tempfile.mkdtemp())
        uuid = "dash-to-dock@micxgx.gmail.com"
        (stage / uuid).mkdir()
        (stage / uuid / "metadata.json").write_text(metadata, encoding="utf-8")
        script = (
            f"SVER={sver}\nSTAGE={stage}\nuuid={uuid}\n"
            f"if {condition}; then echo MATCH; else echo NOMATCH; fi"
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

class TestVerifyGateVeto(unittest.TestCase):
    """A failed earlier verify gate must be able to veto the success marker.

    Regression, measured on a real build: ubuntu-lts ran its firefox/app-set
    gates successfully, then hit `VERIFY-FAIL: Yaru sound-theme gschema override
    missing` (the grep looked for `theme-name='Yaru'` while the shipped
    10_ubuntu-settings.gschema.override writes `theme-name = 'Yaru'`) and
    exited 1 -- and the very next runcmd entry still printed
    `VERIFY-OK: all required components present`, which _check_success_marker
    treats as a SUCCESS. Because cloud-init runs each runcmd entry
    independently, an earlier entry's exit status cannot stop the ones after
    it, so the marker is the only contract. Any failure path must therefore
    leave evidence on disk that the success marker refuses to print over.

    The test asserts the mechanism itself (the veto file), not the text of a
    specific gate: if someone adds a third Ubuntu gate tomorrow, what has to
    hold is that its failure cannot coexist with VERIFY-OK.
    """

    def _render(self, template="ubuntu.j2"):
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        env = get_jinja_env(REPO / "templates")
        return env.get_template(template).render(**render_context(template[:-3]))

    def _scripts(self, rendered):
        import yaml
        doc = yaml.safe_load(rendered)
        out = []
        for entry in doc.get("runcmd") or []:
            if isinstance(entry, list) and len(entry) >= 3 and entry[1] == "-c":
                out.append(entry[2])
        return out

    def _veto_path(self):
        return "verify-gate-fail"

    def test_success_macro_checks_the_veto_file_first(self):
        """_macros.j2 must refuse to print its marker when the veto exists."""
        for template in ("ubuntu.j2", "gentoo.j2"):
            with self.subTest(template=template):
                scripts = self._scripts(self._render(template))
                self.assertTrue(
                    any("VERIFY-OK: all required components present" in sc for sc in scripts),
                    f"{template} must emit the success marker",
                )
                self.assertTrue(
                    any(self._veto_path() in sc for sc in scripts),
                    f"{template} must consult the veto file",
                )

    def test_every_failure_path_leaves_the_veto_file(self):
        """Each fail()/emitfail() helper must stamp the file, not just exit.

        A bare `exit 1` is invisible to the next runcmd entry; the file is
        what carries the verdict across the entry boundary.
        """
        import re
        for template in ("ubuntu.j2", "gentoo.j2"):
            scripts = self._scripts(self._render(template))
            helpers = set()
            for sc in scripts:
                for m in re.finditer(r"(?:fail|emitfail)\(\) \{", sc):
                    helpers.add(m.group(0))
                for m in re.finditer(r"(?:fail|emitfail)='", sc):
                    helpers.add(m.group(0))
            has_failures = any("VERIFY-FAIL:" in sc for sc in scripts)
            if not has_failures:
                continue
            self.assertTrue(helpers, f"{template} must define a failure helper")
            # At least one helper definition body must write the veto file.
            self.assertTrue(
                any(self._veto_path() in sc for sc in scripts
                    if "VERIFY-FAIL:" in sc),
                f"{template} has VERIFY-FAIL paths that never write the veto file",
            )

    def test_ubuntu_sound_theme_gate_tolerates_whitespace(self):
        """The gschema override writes `theme-name = 'Yaru'`, not `theme-name='Yaru'`.

        Exact-text grep reported "missing override" on a perfectly good
        Ubuntu build (26.04). The gate must tolerate whitespace around '='
        and must never hard-fail on the override's phrasing.
        """
        rendered = self._render("ubuntu.j2")
        gates = [sc for sc in self._scripts(rendered)
                 if "SOUNDTHEME" in sc or "yaru-theme-sound" in sc]
        self.assertTrue(gates, "ubuntu.j2 must carry a sound-theme gate")
        text = "\n".join(gates)
        self.assertNotIn("theme-name='Yaru'", text,
                         "the old no-whitespace pattern never matches the shipped override")
        self.assertIn("theme-name[[:space:]]*=", text)
        # The authoritative assertion is the effective dconf value, so a
        # differently-phrased override stays a warning rather than a failure.
        self.assertNotIn("gschema override missing", text)
        self.assertTrue(
            any("SOUNDTHEME-WARN" in sc for sc in gates),
            "a phrasing mismatch must warn, not fail the build",
        )

def _load_lint_python():
    """Import scripts/lint-python.py (hyphenated filename) as a module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lint_python", REPO / "scripts" / "lint-python.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class TestPylintGate(unittest.TestCase):
    """scripts/lint-python.py -- fleet Gate 0c and the CI changed-files check.

    The invariant worth protecting: the gate must never report success
    without having checked something. Every fallback path therefore lands
    on the full tree, not on an empty file list.
    """

    def _temp_repo(self):
        import subprocess
        root = Path(tempfile.mkdtemp(prefix="lint-python-"))
        for cmd in (["git", "init", "-q"],
                    ["git", "config", "user.email", "t@example.com"],
                    ["git", "config", "user.name", "test"]):
            subprocess.run(cmd, cwd=str(root), check=True, capture_output=True)
        return root

    def _commit(self, root, relpath, content):
        import subprocess
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", f"add {relpath}"],
                       cwd=str(root), check=True, capture_output=True)

    def test_unresolvable_ref_falls_back_to_the_full_tree(self):
        """A force-push / root commit / first-push zero sha must not lint 0 files."""
        mod = _load_lint_python()
        with contextlib.redirect_stdout(io.StringIO()):
            files, scope = mod.changed_files("0" * 40)
        self.assertIn("full tree", scope)
        self.assertEqual(files, mod.tracked_files())
        self.assertGreater(len(files), 0, "fallback must cover real files")

    def test_changed_scope_is_ref_to_worktree_plus_untracked(self):
        """CI's scope: modified files AND new untracked ones, never deletions."""
        mod = _load_lint_python()
        root = self._temp_repo()
        self._commit(root, "a.py", "x = 1\n")
        self._commit(root, "gone.py", "z = 0\n")
        self._commit(root, "notes.md", "docs\n")
        (root / "a.py").write_text("x = 2  # modified\n", encoding="utf-8")
        (root / "b.py").write_text("y = 3  # brand new, untracked\n", encoding="utf-8")
        (root / "gone.py").unlink()
        with mock.patch.object(mod, "REPO", root):
            files, scope = mod.changed_files("HEAD")
        self.assertEqual(files, ["a.py", "b.py"],
                         "deleted files must be excluded (pylint cannot read them)")
        self.assertIn("changed since HEAD", scope)

    def test_missing_pylint_is_a_hard_error_not_a_silent_pass(self):
        import subprocess
        mod = _load_lint_python()
        fake = subprocess.CompletedProcess(
            ["python", "-m", "pylint"], returncode=1,
            stdout="", stderr="No module named pylint")
        with mock.patch.object(mod.subprocess, "run", return_value=fake), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(Path, "exists", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                mod.find_pylint()
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_passes_on_a_docs_only_change_without_invoking_pylint(self):
        """`--changed-since` over an empty change set exits 0 with a marker.

        The exact shape CI runs for a docs-only push, and the reason the
        script resolves pylint only after collecting the scope.
        """
        import subprocess
        import sys
        root = self._temp_repo()
        self._commit(root, "README.md", "# docs only\n")
        self._commit(root, "scripts/lint-python.py",
                     (REPO / "scripts" / "lint-python.py").read_text(encoding="utf-8"))
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "lint-python.py"),
             "--changed-since", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PYLINT-OK: no Python files in scope", proc.stdout)

    def test_fleet_exposes_the_no_pylint_flag(self):
        """Gate 0c is wired: --no-pylint exists and --help still parses."""
        import subprocess
        import sys
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "build-fleet-sequential.py"),
             "--help"],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--no-pylint", proc.stdout)
        self.assertIn("Gate 0c", proc.stdout)

if __name__ == "__main__":
    unittest.main()

class TestCrossDistroUxParity(unittest.TestCase):
    """Titlebar buttons + dock geometry must match Ubuntu's :ubuntu variants.

    Reported by the user against the built VMs:
      1. "The minimize and maximise in Tweaks is enabled by default for
         Ubuntu, but not for Gentoo."
      2. "the dash to dock for Ubuntu is docked to the left whereas Gentoo is
         on the bottom."

    Root cause (probed on the Ubuntu VM, Ubuntu 26.04 / GNOME Shell 50.1):
    both are gsettings defaults that Ubuntu ships as *schema variants*
    (/usr/share/glib-2.0/schemas/10_ubuntu-settings.gschema.override section
    [org.gnome.desktop.wm.preferences:ubuntu] with
    button-layout = ':minimize,maximize,close', and
    10_ubuntu-dock.gschema.override [org.gnome.shell.extensions.dash-to-dock:ubuntu]
    with dock-position='LEFT', dock-fixed=true, extend-height=true, ...).
    Gentoo's session selects no variant, so every stock default applied
    ('appmenu:close' -> no Minimize/Maximize; 'BOTTOM' -> bottom dock).

    The fix writes the SAME values into the system-wide dconf db
    (/etc/dconf/db/local.d), which beats schema defaults on every session:
    button-layout in 03-workspaces for BOTH distros (identical bytes to
    Ubuntu's own variant on Ubuntu), and a new 07-dock keyfile for Gentoo
    only -- gated on the same install_dash_to_dock var that gates the
    extension install so "installs" and "configures" cannot drift apart.

    These tests assert the rendered YAML's dconf write path. The values
    themselves were verified separately against the dash-to-dock v110
    gschema.xml installed on the built Gentoo VM: every enum nick
    (LEFT / ALL_WINDOWS / DOTS / switch-workspace / focus-or-appspread /
    launch / minimize) exists in that schema, so none can silently fall back
    to a default -- which is exactly how this class of bug hides (dconf does
    not validate; a bad nick reads back as the schema default).
    """

    # Ubuntu's dock variant, verbatim (read off the Ubuntu VM).
    UBUNTU_DOCK_VARIANT = {
        "dock-position": "'LEFT'",
        "dock-fixed": "true",
        "intellihide": "true",
        "intellihide-mode": "'ALL_WINDOWS'",
        "extend-height": "true",
        "icon-size-fixed": "true",
        "custom-theme-shrink": "true",
        "running-indicator-style": "'DOTS'",
        "scroll-action": "'switch-workspace'",
        "click-action": "'focus-or-appspread'",
        "shift-click-action": "'launch'",
        "middle-click-action": "'launch'",
        "shift-middle-click-action": "'minimize'",
        "show-mounts-only-mounted": "false",
        "show-mounts-network": "true",
        "disable-overview-on-startup": "true",
    }

    def _render(self, template, **ctx):
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        distro = template.replace(".j2", "")
        base = render_context(distro)
        base.update(ctx)
        env = get_jinja_env(REPO / "templates")
        return env.get_template(template).render(**base)

    def _runcmd(self, rendered):
        import yaml
        return yaml.safe_load(rendered)["runcmd"]

    def _keyfile_writes(self, rendered, keyfile):
        """Parse `key=...` lines out of the runcmd entry writing `keyfile`."""
        import re
        for item in self._runcmd(rendered):
            if isinstance(item, list) and len(item) >= 3 and item[1] == "-c":
                if f"/etc/dconf/db/local.d/{keyfile}" in item[2]:
                    # The entry is one shell printf; take everything up to the
                    # redirect so we only see this keyfile's content.
                    body = item[2]
                    body = body[: body.index(f"> /etc/dconf/db/local.d/{keyfile}")]
                    body = body[body.index("printf '") + len("printf '"):]
                    return dict(re.findall(r"([a-z0-9-]+)=(\"[^\"]*\"|[0-9]+|true|false)\n", body.replace("\\n", "\n")))
        return None

    def test_button_layout_minimize_and_maximize_on_both_distros(self):
        for template in ("ubuntu.j2", "gentoo.j2"):
            writes = self._keyfile_writes(self._render(template), "03-workspaces")
            self.assertIsNotNone(writes, f"{template} must write 03-workspaces")
            self.assertEqual(
                writes.get("button-layout"), '":minimize,maximize,close"',
                f"{template} must set button-layout to Ubuntu's variant value "
                "(stock 'appmenu:close' ships no Minimize/Maximize button)",
            )
            self.assertEqual(writes.get("num-workspaces"), "1",
                             f"{template} must keep the single-workspace setting")

    def test_gentoo_writes_ubuntu_parity_dock_keyfile(self):
        writes = self._keyfile_writes(self._render("gentoo.j2"), "07-dock")
        self.assertIsNotNone(writes, "gentoo.j2 must write the 07-dock keyfile")
        normalized = {k: v.strip('"').strip("'") for k, v in writes.items()}
        for key, value in self.UBUNTU_DOCK_VARIANT.items():
            self.assertIn(key, writes, f"07-dock is missing {key}")
            self.assertEqual(
                normalized[key], value.strip("'"),
                f"07-dock {key} must equal Ubuntu's variant value {value}",
            )
        # Explicitly pinned (already default-true in dash-to-dock v110) so a
        # future upstream default flip cannot silently change the dock.
        self.assertEqual(normalized["show-mounts"], "true")
        self.assertEqual(normalized["show-trash"], "true")
        self.assertEqual(normalized["dash-max-icon-size"], "48")

    def test_ubuntu_does_not_write_dock_keyfile(self):
        """Ubuntu keeps its stock ubuntu-dock and its own :ubuntu variant.

        Writing 07-dock there too would configure an extension Ubuntu does
        not even enable (dash-to-dock), and the values are already Ubuntu's
        defaults -- the file must not render at all.
        """
        script = None
        for item in self._keyfile_writes(self._render("ubuntu.j2"), "07-dock") or []:
            script = item
        self.assertIsNone(script, "ubuntu.j2 must not render a 07-dock write")
        for item in self._runcmd(self._render("ubuntu.j2")):
            if isinstance(item, list) and len(item) >= 3 and item[1] == "-c":
                self.assertNotIn("07-dock", item[2],
                                 "ubuntu.j2 must not reference 07-dock anywhere in runcmd")

    def test_dock_keyfile_gate_matches_extension_install_gate(self):
        """The dock settings gate must use the same var as the install gate.

        Regression guard: gating the extension install on
        install_dash_to_dock but the configuration on (say) a hardcoded
        distro check is how "installs" and "enables" drifted apart before
        (dconf enabled an extension that was never installed).

        Ubuntu (which pins the var false at its top level) must render no
        07-dock at all, and the shared include must place the write behind
        the SAME `{% if install_dash_to_dock | default(true) %}` guard as the
        enabled-extensions entry, so the two can never disagree.

        NOTE: rendering gentoo.j2 with install_dash_to_dock=False through the
        context does NOT switch the dock off, and that is correct Jinja
        behaviour rather than a bug: a top-level `{% set %}` in a child
        template always overrides a context value (verified against a minimal
        base/child pair). gentoo.j2 deliberately pins the var, so the
        meaningful assertions are "var false -> no 07-dock" (Ubuntu) plus
        "the same guard covers both sites" (so both move together if the
        gate is ever renamed).
        """
        import re
        import yaml
        doc = yaml.safe_load(self._render("ubuntu.j2"))
        self.assertFalse(
            any(
                isinstance(item, list) and len(item) >= 3 and item[1] == "-c"
                and "07-dock" in item[2]
                for item in doc["runcmd"]
            ),
            "install_dash_to_dock=false (Ubuntu) must render no 07-dock write",
        )
        shared = (REPO / "templates" / "_dconf_common.j2").read_text(encoding="utf-8")
        guards = re.findall(r"\{%\s*if install_dash_to_dock \| default\(true\)\s*%\}", shared)
        self.assertEqual(
            len(guards), 2,
            f"_dconf_common.j2 must gate BOTH the enabled-extensions entry and "
            f"the 07-dock write on the same var (found {len(guards)})",
        )
        gs = (REPO / "templates" / "_gs_extensions_common.j2").read_text(encoding="utf-8")
        self.assertIn(
            "{% if install_dash_to_dock | default(true) %}",
            gs,
            "_gs_extensions_common.j2 must gate the install on the same var",
        )


class TestGlmark2WaylandBuild(unittest.TestCase):
    """Gentoo's glmark2 must actually be BUILT, and be a real Wayland binary.

    Reported by the user against the built Gentoo VM:
       3. "One other thing I just remembered. Glmark2-Wayland doesn't work."

    Root cause: three stacked bugs in the GLMARK2 section of
    gentoo-install.sh (templates/gentoo.j2), any one of which alone
    delivered no benchmark:

      a. the dep list emerged `x11-libs/glfw`, a CATEGORY THAT DOES NOT
         EXIST in the Gentoo tree (glfw lives in media-libs/glfw, 3.4,
         Masked). Portage aborted with "there are no ebuilds to satisfy
         x11-libs/glfw", GLMARK2-DEPS stayed 0, GLMARK2-SKIP printed and
         the build never ran. Verified in the guest:
         `emerge --search glfw` -> media-libs/glfw only.

      b. `-Dflavors=wayland,...` is not a valid meson choice. Upstream
         meson_options.txt offers wayland-gl, wayland-glesv2, x11-gl,
         x11-glesv2, x11-gl-egl, drm-*, gbm-*, null-*, dispmanx-glesv2,
         win32-* -- no bare 'wayland', so meson exits "option "flavors"
         may not be set to value 'wayland'".

      c. `ln -sf glmark2 /usr/local/bin/glmark2-wayland` pointed the
         Wayland binary at the x11-gl one, which cannot open a display in
         a Wayland-only session.

    And the deps are in fact FREE: for these flavors meson.build declares
    only wayland-client, wayland-cursor, wayland-egl,
    wayland-protocols >=1.12, wayland-scanner, x11, libjpeg and libpng --
    all already present on the built guest (pkg-config probes: 1.26.0,
    1.26.0, 18.1.0, 1.49, 1.8.13; /usr/bin/wayland-scanner;
    /usr/include/jpeglib.h + /usr/include/png.h). meson/ninja come from
    _gentoo_pkgs (dev-build/*). So NO package has to be emerged for
    glmark2, and no atom appears in _gentoo_pkgs or scripts/audit-packages.py.

    Verified end-to-end on the built Gentoo VM as the greeter user: meson
    + ninja + `ninja install` produces glmark2, glmark2-es2,
    glmark2-wayland and glmark2-es2-wayland (Ubuntu's exact naming) plus
    the data tree under /usr/local/share/glmark2, and
    `timeout 25 glmark2-wayland --validate` reports
    "Validation: Success" per scene with rc=0 on the greeter's socket.
    """

    # Exactly the names upstream meson's flavor map emits (src/meson.build
    # lines 340-342 and their Wayland twins). Pinned so a rename upstream
    # cannot silently re-break naming parity with Ubuntu's apt package.
    EXPECTED_BINARIES = ("glmark2", "glmark2-es2",
                         "glmark2-wayland", "glmark2-es2-wayland")

    VALID_FLAVORS = ("wayland-gl", "wayland-glesv2", "x11-gl", "x11-glesv2")

    def _gentoo_install_script(self) -> str:
        """The rendered gentoo-install.sh write_files content."""
        import yaml
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context

        rendered = get_jinja_env(REPO / "templates").get_template(
            "gentoo.j2").render(**render_context("gentoo"))
        content = ""
        for wf in yaml.safe_load(rendered).get("write_files") or []:
            if isinstance(wf, dict) and str(wf.get("path", "")).endswith(
                    "gentoo-install.sh"):
                content = str(wf.get("content") or "")
        self.assertTrue(content, "templates/gentoo.j2 must write gentoo-install.sh")
        return content

    def _glmark2_section(self) -> str:
        """The GLMARK2 section, COMMENTS STRIPPED.

        The section's prose deliberately names the three bugs it replaced
        (a bad atom, a bad -Dflavors value, a misleading symlink), so an
        assertion that searched raw text would match its own explanation
        and pass on a regression. Tests therefore read the code a growing
        build ever boots: every line whose first non-space character is
        '#' removed.
        """
        script = self._gentoo_install_script()
        start = script.index("# ---- GL benchmark")
        self.assertGreater(start, 0)
        raw = script[start:script.index("# ---- Chrome", start)]
        return "\n".join(line for line in raw.split("\n")
                         if not line.lstrip().startswith("#"))

    def test_no_nonexistent_glfw_atom_is_emerged(self):
        section = self._glmark2_section()
        self.assertNotIn("x11-libs/glfw", section,
                         "x11-libs/glfw does not exist in the Gentoo tree "
                         "(glfw is media-libs/glfw) -- emerging it aborted the "
                         "whole dep step and glmark2 was never built")
        # Nothing at all should be emerged for glmark2: every real dep is
        # already in the GNOME stack, so an emerge here is a lie about a
        # dependency that does not exist.
        self.assertNotIn("emerge ", section,
                         "glmark2 needs NO emerge (wayland/x11/jpeg/png all "
                         "ship with the GNOME stack)")

    def test_meson_flavors_are_valid_upstream_choices(self):
        import re
        section = self._glmark2_section()
        match = re.search(r"-Dflavors=([\w,.-]+)", section)
        self.assertIsNotNone(match, "GLMARK2 section must configure meson flavors")
        for flavor in match.group(1).split(","):
            self.assertIn(flavor, self.VALID_FLAVORS,
                          f"-Dflavors={flavor} is not an upstream meson "
                          "choice; meson exits with "
                          'option "flavors" may not be set to value')

    def test_build_installs_the_data_tree_not_just_the_binaries(self):
        section = self._glmark2_section()
        # `ninja install` (not `install -Dm755 ... src/glmark2`) is what
        # puts the data tree under /usr/local/share/glmark2. Without it
        # every run dies on "Failed to open
        # /usr/local/share/glmark2/shaders/terrain-texture.vert" + core dump.
        # The install target must appear as its OWN ninja invocation on a
        # continuation line of the same `if meson setup ... && ninja ...`
        # chain that follows the meson setup, i.e.:
        #     ninja -C "$GLMARK2_DIR/builddir" install >>$MESA_LOG 2>&1
        # Build-only (no install target) leaves the data tree behind and
        # every run dies on
        # "Failed to open /usr/local/share/glmark2/shaders/terrain-texture.vert".
        meson_chain = re.search(
            r"meson setup[^\n]*(?:\\\n[^\n]*)*?; then", section)
        self.assertIsNotNone(
            meson_chain,
            "the GLMARK2 section must still have its meson/ninja if-chain")
        chain = meson_chain.group(0)
        self.assertRegex(
            chain, r"ninja\s+-C\s+\S+\s+install\b",
            "the meson/ninja chain must include a `ninja ... install` step: "
            "ninja install (NOT `install -Dm755 src/glmark2`) is what puts "
            "the data tree under /usr/local/share/glmark2; the build-only "
            "variant core-dumps on the first shader")
        self.assertNotIn("install -Dm755", section,
                         "installing a bare src/glmark2 ships no data tree")

    def test_no_symlink_masquerades_as_the_wayland_binary(self):
        section = self._glmark2_section()
        self.assertNotIn("ln -sf", section,
                         "glmark2-wayland must be a real Wayland build, never a "
                         "symlink to the x11-gl binary (which cannot open a "
                         "display on a Wayland-only session)")

    def test_all_four_binaries_are_verified(self):
        section = self._glmark2_section()
        for binary in self.EXPECTED_BINARIES:
            self.assertIn(binary, section,
                          f"{binary} must be produced/checked: Ubuntu installs "
                          "glmark2-es2-wayland by name, so Gentoo needs that "
                          "exact path for naming parity")
        # ...and all four in the SAME verification loop, so a future flavour
        # rename cannot silently drop one from the post-build check.
        loop = re.search(r"for b in ([^;]+); do", section)
        self.assertIsNotNone(loop, "the post-build check must iterate the binaries")
        for binary in self.EXPECTED_BINARIES:
            self.assertIn(binary, loop.group(1),
                          f"{binary} must be in the /usr/local/bin existence "
                          "loop, not merely mentioned in a message")

    def test_gentoo_pkgs_contains_no_glmark2_dep_atom(self):
        """The dep atoms must be gone from _gentoo_pkgs AND the PKGS= twin.

        The gentoo.j2:927 hand-maintained `PKGS="..."` list must stay in
        sync with `{% set _gentoo_pkgs = [...] %}` -- the simulate/real
        parity gate exists for exactly this, but only if the atoms are
        actually removed from both places.
        """
        from linux_vm.config import DISTRO_TEMPLATE
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context

        rendered = get_jinja_env(REPO / "templates").get_template(
            DISTRO_TEMPLATE["gentoo"]).render(**render_context("gentoo"))
        import yaml
        for wf in yaml.safe_load(rendered).get("write_files") or []:
            content = str(wf.get("content") or "")
            for atom in ("x11-libs/glfw", "dev-libs/fribidi", "x11-libs/libdrm"):
                # Comments may still mention the bug, but no written file
                # (simulate target list, PKGS= list) may still EMERGE it.
                self.assertNotRegex(
                    content, rf"(?m)^\s*{re.escape(atom)}\s*$",
                    f"{atom} must not appear as an emerge-able line in any "
                    "written file")

    def test_audit_category_cannot_match_a_phantom_atom(self):
        """scripts/audit-packages.py's GL benchmark row must be honest.

        The row used to list `x11-libs/glfw` as the Gentoo side of the pair,
        so the audit reported AUDIT-OK while glmark2 was never built -- a
        false positive is worse than a missing category. Nothing is
        attributable now (dep-free source build), so Gentoo has no entry in
        REQUIRED for it; only the Ubuntu side stays.
        """
        import runpy
        ns = runpy.run_path(str(REPO / "scripts" / "audit-packages.py"),
                            run_name="audit_not_main")
        categories = ns["CATEGORIES"]
        self.assertIn("GL benchmark", categories)
        ubuntu_tokens, _ = categories["GL benchmark"], None
        for token in ("glmark2-es2-wayland", "glmark2-wayland"):
            self.assertIn(token, ubuntu_tokens,
                          f"{token} must stay in the GL benchmark row (Ubuntu)")
        self.assertNotIn("x11-libs/glfw", ubuntu_tokens,
                         "x11-libs/glfw must NEVER be back: it does not exist, "
                         "so it is a permanent false positive")
        # Gentoo's REQUIRED entry must not demand an atom that is not in the
        # category row, which would fail Gate 0b on every run.
        for cat in ns["REQUIRED"]["gentoo"]:
            self.assertIn(cat, categories)


class TestUbuntuSwrastVulkanAndGlmark2(unittest.TestCase):
    """The two Ubuntu snags from the post-snag-list run, locked in.

    Reported by the user against the built ubuntu-lts VM:
       1. Ubuntu has no `glmark2-wayland`, so running it makes the shell
          offer to install the apt package.
       2. `vulkaninfo --summary` lists GPU0 venus only -- the llvmpipe
          software Vulkan device the distro normally ships is gone, and
          Gentoo lists GPU1 llvmpipe as well.

    Both were proven from the built image itself (offline debugfs of the
    converted root fs plus /var/lib/dpkg/status):

    glmark2: Ubuntu's apt package ships ONLY /usr/bin/glmark2-es2-wayland
    (glmark2-es2-wayland.list has one binary line). Ubuntu simply splits the
    per-platform binaries into separate packages, so `glmark2-wayland` is its
    own package and was never in `packages:`.

    Vulkan: the fork build ran
      -Dgallium-drivers=virgl -Dllvm=disabled -Dvulkan-drivers=virtio
    i.e. NO swrast, so no libvulkan_lvp.so was ever produced, and the block
    ALSO ran `rm -f /usr/share/vulkan/icd.d/lvp_icd.json`, deleting the ICD
    the distro's mesa-vulkan-drivers had installed. libvulkan_lvp.so from the
    distro package (26.0.8) was still on disk but unregistered, and venus
    enumerated anyway because mesa's own install had written the
    arch-suffixed virtio_icd.aarch64.json that the rm glob did not match.

    Regression (reported later against the same VM): fixing the lavapipe half
    left the VIRTIO half of the same bug live. apt ships a bare-name
    /usr/share/vulkan/icd.d/virtio_icd.json whose library_path is a bare
    'libvulkan_virtio.so' (same file the fork overwrites), so with the fork's
    virtio_icd.aarch64.json also present the loader loaded libvulkan_virtio.so
    under TWO ICD handles and `vulkaninfo --summary` listed THREE GPUs --
    venus, venus (byte-identical), llvmpipe. Both bare-name manifests must go,
    and the count assertion must run per driver, not once for lavapipe only.
    """

    def _ubuntu(self):
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        env = get_jinja_env(REPO / "templates")
        return env.get_template("ubuntu.j2").render(**render_context("ubuntu-lts"))

    def _ubuntu_scripts(self):
        import yaml
        doc = yaml.safe_load(self._ubuntu())
        return [e[2] for e in doc.get("runcmd") or []
                if isinstance(e, list) and len(e) >= 3 and e[1] == "-c"]

    def _packages(self):
        import yaml
        doc = yaml.safe_load(self._ubuntu())
        return list(doc.get("packages") or [])

    def test_glmark2_wayland_is_an_explicit_ubuntu_package(self):
        """The apt split means `glmark2-wayland` is a separate package."""
        pkgs = self._packages()
        self.assertIn("glmark2-es2-wayland", pkgs,
                      "the original Wayland ES benchmark must stay")
        self.assertIn("glmark2-wayland", pkgs,
                      "Ubuntu's apt repo splits the glmark2 binaries per "
                      "platform; without this package the shell prompts to "
                      "install it on every glmark2-wayland invocation -- the "
                      "exact reported snag")

    def test_appset_gate_covers_both_glmark2_flavours(self):
        """A one-package gate would have passed while the snag was live."""
        scripts = self._ubuntu_scripts()
        gate = [s for s in scripts if "APPSET-VERIFIED" in s]
        self.assertTrue(gate, "ubuntu.j2 must keep the APPSET-VERIFIED gate")
        self.assertIn("glmark2-es2-wayland", gate[0])
        self.assertIn("glmark2-wayland", gate[0],
                      "the app-set gate must assert the package the snag "
                      "named, not just the one that was always installed")

    def test_fork_builds_swrast_and_llvmpipe(self):
        """The mesa fork must ship the software Vulkan device it deleted.

        Gentoo's MESA_ARGS already build swrast+llvmpipe; Ubuntu must match.
        -Dllvm is the llvmpipe JIT backend, so a disabled llvm makes
        `-Dgallium-drivers=llvmpipe` impossible.
        """
        scripts = self._ubuntu_scripts()
        mesa = [s for s in scripts if "meson setup builddir" in s]
        self.assertTrue(mesa, "ubuntu.j2 must configure a mesa builddir")
        block = mesa[0]
        self.assertIn("-Dgallium-drivers=virgl,llvmpipe", block)
        self.assertIn("-Dvulkan-drivers=swrast,virtio", block)
        self.assertIn("-Dllvm=enabled", block)
        self.assertNotIn("-Dllvm=disabled", block)

    def test_software_icd_is_asserted_not_deleted(self):
        """Only the DISTRO's bare-name lavapipe manifest may be removed.

        Building swrast is only half of it: the driver also has to be
        REGISTERED, and mesa's installer writes that JSON itself. The old
        code ran `rm -f .../lvp_icd.json` and deleted the software device's
        registration, which is why the distro's surviving libvulkan_lvp.so
        was invisible to the loader and `vulkaninfo --summary` listed only
        venus. Removing the apt copy is correct -- it is the one manifest
        the fork does NOT write (mesa writes lvp_icd.<arch>.json), so both
        would be live in the loader -- but it must be an exact bare name.
        """
        scripts = self._ubuntu_scripts()
        mesa = [s for s in scripts if "meson setup builddir" in s]
        self.assertTrue(mesa)
        block = mesa[0]
        # The only permitted removal is the distro's bare names, and never a
        # glob: 'lvp_icd*' would delete the fork's own lvp_icd.aarch64.json
        # and re-create the venus-only bug this block exists to fix.
        for line in block.split(";"):
            if "rm -f" in line and "vulkan/icd.d" in line:
                self.assertIn("lvp_icd.json", line,
                              "removal must name the distro's bare manifest")
                self.assertNotIn("lvp_icd*", line,
                                 "a glob would delete the fork's manifest too")
                self.assertNotIn("virtio_icd*", line,
                                 "a glob would delete the fork's manifest too")
        self.assertNotIn("rm -f /usr/share/vulkan/icd.d/*", block,
                         "never wipe the whole ICD directory")
        # The deletion must be followed by an assertion that exactly one
        # lavapipe registration survived, and that it is the arch-suffixed
        # fork one -- otherwise a future package refresh that re-creates the
        # apt manifest keeps two live entries with no build failure.
        self.assertIn("MESA-FORK-SWRAST-ICD", block,
                      "the fork block must prove the lavapipe ICD survived")
        self.assertIn("expected exactly 1 $ICD_DEV ICD registration", block,
                      "the stale-apt-manifest removal must be asserted")
        self.assertIn("libvulkan_lvp.so:lavapipe", block,
                      "lavapipe must be one of the asserted drivers")
        self.assertIn("libvulkan_virtio.so:venus", block,
                      "venus must be asserted too -- leaving it out is the "
                      "duplicate-venus bug (vulkaninfo listed 3 GPUs)")

    def test_icd_removal_is_exact_and_bare(self):
        """`rm` must not use a glob, and the count gate must be a real gate.

        A bare-name-only rm is the difference between removing the distro's
        stale manifest and removing the fork's freshly installed one. The
        assertion after it must also compare against 1, not 0 -- a gate
        written as `-ne 0` would pass with two registrations present.
        """
        scripts = self._ubuntu_scripts()
        mesa = [s for s in scripts if "meson setup builddir" in s]
        self.assertTrue(mesa)
        block = mesa[0]
        rm_lines = [
            l.strip() for l in block.replace(";", "\n").split("\n")
            if "rm -f" in l and "vulkan/icd.d" in l
        ]
        self.assertEqual(len(rm_lines), 1,
                         "exactly one ICD removal line is permitted in the "
                         "fork block")
        self.assertEqual(
            rm_lines[0],
            "rm -f /usr/share/vulkan/icd.d/lvp_icd.json "
            "/usr/share/vulkan/icd.d/virtio_icd.json",
            "the removal must name BOTH distro bare names -- dropping the "
            "virtio one is what left two live venus registrations")
        self.assertIn('-ne 1', block,
                      "the count gate must fail when the stale manifest survives")

    def test_icd_removal_behaves_correctly(self):
        """Execute the rm+count logic against sandboxed ICD directories.

        Text matching cannot prove the gate discriminates, so this runs the
        exact rendered shell with /usr/share/vulkan/icd.d pointed at a temp
        dir. Every driver the fork builds must end with exactly ONE
        arch-suffixed registration: the reported duplicate-venus GPU (three
        entries in `vulkaninfo --summary`) is this shape for virtio, and the
        earlier venus-only bug is the same shape for lavapipe.
        """
        import os
        import shutil
        import subprocess
        import tempfile
        scripts = self._ubuntu_scripts()
        mesa = [s for s in scripts if "meson setup builddir" in s]
        self.assertTrue(mesa)
        block = mesa[0]
        start = block.find("ldconfig;")
        end = block.find("if [ -f /usr/lib/aarch64-linux-gnu/libvulkan_lvp.so ]")
        self.assertNotEqual(start, -1, "ldconfig anchor missing")
        self.assertNotEqual(end, -1, "libvulkan_lvp.so guard missing")
        logic = block[start:end].replace("ldconfig;", "true;")
        sandbox = tempfile.mkdtemp()
        try:
            icd = os.path.join(sandbox, "icd.d")
            os.makedirs(icd)
            logic = logic.replace("/usr/share/vulkan/icd.d", icd)

            def fork(lib):
                return ('{"ICD": {"library_path": '
                        '"/usr/lib/aarch64-linux-gnu/%s"}}' % lib)

            def apt(lib):
                return '{"ICD": {"library_path": "%s"}}' % lib

            # Whichever driver is not under test always arrives already
            # converged, so a failure can only come from the tested one.
            CLEAN = (("lvp_icd.aarch64.json", fork("libvulkan_lvp.so")),
                     ("virtio_icd.aarch64.json", fork("libvulkan_virtio.so")))

            def populate(*entries):
                for f in os.listdir(icd):
                    os.remove(os.path.join(icd, f))
                for name, body in entries:
                    with open(os.path.join(icd, name), "w") as fh:
                        fh.write(body)

            def run(*entries):
                populate(*entries)
                return subprocess.run(["bash", "-c", logic],
                                      capture_output=True, text=True)

            # apt stale + fork for BOTH drivers: the rm must remove the bare
            # apt manifests and leave the fork's arch-suffixed registrations.
            r = run(("lvp_icd.json", apt("libvulkan_lvp.so")),
                    ("lvp_icd.aarch64.json", fork("libvulkan_lvp.so")),
                    ("virtio_icd.json", apt("libvulkan_virtio.so")),
                    ("virtio_icd.aarch64.json", fork("libvulkan_virtio.so")))
            self.assertEqual(r.returncode, 0,
                             "apt + fork manifests must converge on the fork's "
                             "files: " + r.stderr)
            self.assertEqual(sorted(os.listdir(icd)),
                             ["lvp_icd.aarch64.json", "virtio_icd.aarch64.json"],
                             "the fork's registrations must survive the rm")

            # fork only: nothing to delete, gate passes.
            r = run(*CLEAN)
            self.assertEqual(r.returncode, 0, r.stderr)

            # THE REPORTED BUG: the bare apt virtio manifest survives because
            # no fork file replaced it. Must FAIL -- otherwise the image ships
            # a second, byte-identical venus GPU.
            r = run(("lvp_icd.aarch64.json", fork("libvulkan_lvp.so")),
                    ("virtio_icd.json", apt("libvulkan_virtio.so")))
            self.assertNotEqual(r.returncode, 0,
                                "a surviving bare-named venus registration must "
                                "fail the build")
            self.assertIn("MESA-FORK-FAIL", r.stderr)
            self.assertIn("venus", r.stderr)

            # The lavapipe half of the same bug must still fail too.
            r = run(("lvp_icd.json", apt("libvulkan_lvp.so")), *CLEAN[1:])
            self.assertNotEqual(r.returncode, 0,
                                "a surviving bare-named lavapipe registration "
                                "must fail the build")
            self.assertIn("MESA-FORK-FAIL", r.stderr)

            # Two fork-styled venus registrations under different names both
            # survive the rm: FAIL, because the loader would then enumerate
            # venus twice in vulkaninfo -- the reported output.
            r = run(("virtio_icd.aarch64.json", fork("libvulkan_virtio.so")),
                    ("virtio_icd.other.json", fork("libvulkan_virtio.so")),
                    *CLEAN[:1])
            self.assertNotEqual(r.returncode, 0,
                                "two live venus entries must fail rather than "
                                "silently enumerating two GPUs in vulkaninfo")

            # A registration the rm does not name, and that is not the fork's
            # arch-suffixed file, is a bare name by another route: FAIL.
            r = run(("virtio_icd.arm64.json", fork("libvulkan_virtio.so")),
                    *CLEAN[:1])
            self.assertNotEqual(r.returncode, 0,
                                "only the fork's arch-suffixed manifest may be "
                                "the surviving registration")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)



    def test_verify_block_gates_the_software_vulkan_device(self):
        """`vulkaninfo --summary` showed venus only, so gate the two facts.

        Gate the loader-visible facts (the .so plus its ICD JSON), not a live
        `vulkaninfo` run: the marker already proves the build ran, and a
        display-touching tool has no place in a verify block.
        """
        scripts = self._ubuntu_scripts()
        gate = [s for s in scripts if "SWRAST-VULKAN-OK" in s]
        self.assertTrue(gate, "ubuntu.j2 must gate the software Vulkan device")
        gate = gate[0]
        self.assertIn("libvulkan_lvp.so", gate)
        self.assertIn("/usr/share/vulkan/icd.d", gate)
        self.assertIn("VERIFY-FAIL", gate,
                      "a missing swrast device must fail the verify block, "
                      "not warn")
        # Only its absence as a COMMAND matters: the gate's failure message
        # is allowed to name vulkaninfo as the symptom.
        self.assertNotRegex(
            gate, r"(^|[;&|]\s*)vulkaninfo\s",
            "vulkaninfo touches a display -- a verify block must assert the "
            "loader-visible files, not run it")

    def test_verify_block_rejects_a_double_registered_vulkan_device(self):
        """The verify gate must count registrations, not merely find one.

        The reported 3-GPU `vulkaninfo --summary` is exactly a second live
        manifest for the SAME driver library. A verify gate that only asserts
        "an ICD JSON exists for libvulkan_virtio.so" passes with two of them,
        so it has to compare the count against 1 -- for both drivers.
        """
        scripts = self._ubuntu_scripts()
        gate = [s for s in scripts if "SWRAST-VULKAN-OK" in s]
        self.assertTrue(gate)
        gate = gate[0]
        self.assertIn("libvulkan_virtio.so", gate,
                      "the venus driver must be gated too -- otherwise the "
                      "duplicate-venus image sails through verify")
        self.assertRegex(
            gate, r"for L in libvulkan_lvp\.so libvulkan_virtio\.so",
            "both drivers must be checked, so a new driver cannot be added "
            "without a registration count")
        self.assertRegex(gate, r"test \"?\$N\"? -eq 1",
                         "the per-driver count must be gated at exactly 1")


class TestGentooNmAppletAtom(unittest.TestCase):
    """Gentoo must install the NetworkManager systray applet with a REAL atom.

    Found offline on the built gentoo VM during the Ubuntu-snag follow-up:
    the install script ran

        emerge --getbinpkg --verbose net-misc/networkmanager-gnome \\
            2>&1 | tail -5 || true

    `net-misc/networkmanager-gnome` does not exist in the Gentoo tree, so
    portage failed with "there are no ebuilds to satisfy" and `|| true`
    swallowed it -- Gentoo shipped with NO systray connection editor while
    Ubuntu got one from its own network-manager-gnome package. The Ubuntu
    package name was transcribed straight into a Portage category.

    The correct atom is `gnome-extra/nm-applet` (upstream network-manager-
    applet, provides nm-applet/nm-connection-editor).

    Two things this class pins:
      1. the atom exists in BOTH the simulate target list (_gentoo_pkgs,
         auto-derives /etc/install-simulate-pkgs.txt) and the hand-maintained
         PKGS= twin, so Gate 3 validates it and the real install emerges it;
      2. the install step emits a truthful marker and FAILS the build when the
         binary is absent, so a future bad atom cannot be swallowed by `|| true`.
    """
    ATOM = "gnome-extra/nm-applet"
    BAD_ATOM = "net-misc/networkmanager-gnome"

    def _gentoo(self, simulate_only: bool = False) -> str:
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        ctx = render_context("gentoo")
        ctx["simulate_only"] = simulate_only
        return get_jinja_env(REPO / "templates").get_template(
            "gentoo.j2").render(**ctx)

    def test_nm_applet_is_in_the_gentoo_package_set(self):
        rendered = self._gentoo()
        self.assertIn(self.ATOM, rendered,
                      f"{self.ATOM} must be an explicit Gentoo package "
                      "(explicit over implicit, even if a meta pulls it)")

    def test_nm_applet_atom_present_in_simulate_target_list(self):
        """The simulate list is what Gate 3 resolves; it must carry the atom."""
        import yaml
        rendered = self._gentoo(simulate_only=True)
        target = ""
        for wf in yaml.safe_load(rendered).get("write_files") or []:
            if isinstance(wf, dict) and wf.get("path") == \
                    "/etc/install-simulate-pkgs.txt":
                target = str(wf.get("content") or "")
        self.assertTrue(target, "/etc/install-simulate-pkgs.txt missing")
        self.assertIn(self.ATOM, target.split(),
                      f"{self.ATOM} must be resolvable by the Gate 3 dry-run")
        self.assertNotIn(self.BAD_ATOM, target.split(),
                         f"{self.BAD_ATOM} does not exist in the Gentoo tree")

    def test_nm_applet_atom_present_in_pkgs_twin(self):
        """The real install's hand-maintained PKGS= must carry it too.

        PKGS= is a single unindented line of atoms, so matching is on the
        token (bounded by whitespace/quote), not on a line of its own.
        """
        script = self._gentoo_install_script()
        # Isolate the assignment so a comment naming the atom cannot pass.
        m = re.search(r'(?m)^PKGS="([^"]*)"$', script)
        self.assertIsNotNone(m, 'PKGS="..." assignment not found in the install script')
        atoms = m.group(1).split()
        self.assertIn(self.ATOM, atoms,
                      f"{self.ATOM} must be EMERGE-able from PKGS=")
        self.assertNotIn(self.BAD_ATOM, atoms,
                         f"{self.BAD_ATOM} does not exist and is a silent no-op")

    def _gentoo_install_script(self) -> str:
        import yaml
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        rendered = get_jinja_env(REPO / "templates").get_template(
            "gentoo.j2").render(**render_context("gentoo"))
        for wf in yaml.safe_load(rendered).get("write_files") or []:
            if isinstance(wf, dict) and str(wf.get("path", "")).endswith(
                    "gentoo-install.sh"):
                return str(wf.get("content") or "")
        return ""

    def test_nonexistent_atom_is_not_emerged(self):
        """The bad atom must not be EMERGE-able anywhere in the install script.

        Comments are allowed to name it (the scar has to stay documented so
        it is not re-introduced), but no command line may pass it to emerge.
        """
        script = self._gentoo_install_script()
        for line in script.splitlines():
            if "emerge" in line:
                self.assertNotIn(self.BAD_ATOM, line,
                                 f"{self.BAD_ATOM} is not a real Gentoo package "
                                 "and its failure was silently swallowed by "
                                 "`|| true`")

    def test_nm_applet_install_is_gated_not_swallowed(self):
        """The step must emit a marker and hard-fail when the binary is absent.

        Anything ending in `|| true` would re-create the original bug's
        signature: emerge fails, the pipeline returns 0, nothing complains.
        """
        script = self._gentoo_install_script()
        self.assertIn("NM-APPLET-OK", script,
                      "the step must emit NM-APPLET-OK on success")
        self.assertIn("NM-APPLET-FAIL", script,
                      "the step must emit NM-APPLET-FAIL on failure")
        self.assertIn("nm-connection-editor", script,
                      "the gate must assert the binary, not emerge's exit code")
        # No `|| true` on the applet emerge: a swallowed failure is what
        # shipped a Gentoo VM with no NM applet for several runs.
        for line in script.splitlines():
            if "nm-applet" in line and "emerge" in line:
                self.assertNotIn("|| true", line,
                                 "the nm-applet emerge must not be `|| true`: "
                                 "a bad atom would then fail silently again")

    def test_audit_row_cannot_be_satisfied_by_the_bad_atom(self):
        """The audit row must accept the real atom and reject the phantom one."""
        import runpy
        ns = runpy.run_path(str(REPO / "scripts" / "audit-packages.py"),
                            run_name="audit_not_main")
        tokens = ns["CATEGORIES"].get("NetworkManager") or []
        self.assertIn("net-misc/networkmanager", tokens,
                      "the NetworkManager daemon token must stay (Gentoo)")
        self.assertIn("gnome-extra/nm-applet", tokens,
                      "the applet token must be a candidate, so the audit "
                      "proves the applet is installed and not just the daemon")
        self.assertNotIn("net-misc/networkmanager-gnome", tokens,
                         "that atom does not exist, so listing it would be a "
                         "permanent false positive (the x11-libs/glfw scar)")


class TestGentooWaitBoundPair(unittest.TestCase):
    """Gentoo's wait is a COUPLED PAIR, and the pair -- not either number --
    is the invariant. linux_vm/config.py:71 (fleet cloud-init cap) and the
    runcmd install-wait loop in templates/gentoo.j2 (600 x 30 s) bound the same
    emerge from different start AND end instants:

      * guest loop:  starts when runcmd reaches the wait entry, ends at the
                     /var/lib/gentoo-install-done marker.
      * fleet cap:   starts at boot (linux_vm/fleet/orchestrator.py:85), ends
                     when _check_success_marker sees VERIFY-OK, which is
                     AFTER the loop plus GDM, dconf, app platforms, snaps and
                     the verify block.

    So the fleet clock starts earlier and stops later. An equal pair is
    therefore strictly too small, not merely tight: the fleet writes its failure
    verdict and shuts the VM down while the guest is still legitimately
    emerging, VERIFY-OK is never observed, and a healthy build is reported
    FAILED. That is precisely the scar that pushed the loop to 1680 x 30 s in
    the first place -- the original 5 h bound fired first, verify ran against a
    half-installed system (no gnome-software, no snapd) and failed with a
    misleading component MISS.
    """

    def _loop_seconds(self) -> int:
        """Iterations of the guest install-wait loop, in seconds."""
        from linux_vm.templates import get_jinja_env
        from linux_vm.test_context import render_context
        rendered = get_jinja_env(REPO / "templates").get_template(
            "gentoo.j2").render(**render_context("gentoo"))
        lines = [ln for ln in rendered.splitlines()
                 if "gentoo-install-done" in ln and "seq 1" in ln]
        self.assertEqual(
            len(lines), 1,
            "exactly one install-wait loop: the wait entry was renamed or "
            "duplicated, so this invariant would silently stop being checked")
        self.assertIn("sleep 30", lines[0],
                      "the loop sleeps 30 s per iteration -- a different sleep "
                      "invalidates every second computed from the iteration "
                      "count")
        match = re.search(r"seq 1 (\d+)", lines[0])
        self.assertIsNotNone(match, "the wait loop must be a bounded seq range")
        return int(match.group(1)) * 30

    def test_fleet_cap_exceeds_guest_loop_with_margin(self):
        loop_sec = self._loop_seconds()
        cap_sec = int(DISTRO_DEFAULTS["gentoo"]["cloud_init_wait_timeout_sec"])
        self.assertGreater(
            cap_sec, loop_sec,
            "linux_vm/config.py gentoo cloud_init_wait_timeout_sec must be "
            "STRICTLY greater than the guest wait loop: equal values abandon a "
            "healthy build, because the fleet clock starts earlier (at boot) "
            "and stops later (at VERIFY-OK, after GDM/dconf/snaps/verify)")
        # A positive-but-tiny margin is the same bug with extra steps: the
        # post-loop runcmd stages still need real time. 30 min is the floor.
        self.assertGreaterEqual(
            cap_sec - loop_sec, 1800,
            "the cap needs >=30 min of margin over the loop for the post-loop "
            "runcmd stages (GDM, dconf, app platforms, snaps, verify)")

    def test_guest_loop_sits_above_the_worst_measured_cloud_init(self):
        loop_sec = self._loop_seconds()
        # Worst gentoo cloud-init ever measured: 231.7 min = 13902 s.
        self.assertGreaterEqual(
            loop_sec, 14000,
            "the guest loop must exceed the WORST measured gentoo cloud-init "
            "(231.7 min), not the typical one -- the 5 h value is only safe "
            "while the webkit-gtk binpkg pin holds; see the wait-loop comment "
            "in templates/gentoo.j2")
