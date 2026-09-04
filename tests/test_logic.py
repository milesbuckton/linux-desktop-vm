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
        self.assertEqual(recommended_memory_mb(8192), 4096)     # halved (boundary, not below floor)
        self.assertEqual(recommended_memory_mb(4096), 8192)     # raised to floor
        self.assertEqual(recommended_memory_mb(65536), 32768)   # cap
        self.assertEqual(recommended_memory_mb(32768), 16384)   # halved

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
            # ours must not be picked up.
            bogus = "f" * 64
            with self.assertRaises(RuntimeError):
                self._sums_lookup(f"{bogus}  not-{img.name}\n", img)

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
        # tool paths at tiny real files.
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


if __name__ == "__main__":
    unittest.main()
