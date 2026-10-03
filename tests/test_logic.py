"""Unit tests for the pure host-side logic (no network, no VMs).

Run from repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import lzma
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

from linux_vm.config import VMConfig, filename_from_url
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

    def test_unparseable_json_does_not_raise(self):
        self._run("{not json")
        log = self.wait_log.read_text(encoding="utf-8")
        self.assertIn("cloud-init status snapshot", log)
        self.assertIn("errors=0", log)

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


if __name__ == "__main__":
    unittest.main()
