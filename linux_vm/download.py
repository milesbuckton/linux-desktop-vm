"""Cloud image download, hash verification, caching, and distro resolvers."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .config import DISTRO_ORDER, DISTRO_TEMPLATE
from . import host
from .log import log


def _urlopen(url, *, timeout: float = 20, method: Optional[str] = None):
    """urllib.request.urlopen wrapper that uses certifi's CA bundle when
    available, fixing SSL verification failures for sites that
    redirect to mirrors with certs not in the system store. Retries on
    transient TimeoutError / URLError.

    Non-HTTP URLErrors (DNS failures -- the documented macOS mDNSResponder
    flap, HISTORY #15) back off 4s/8s so a resolver flap that outlives a
    single attempt is ridden out here instead of bubbling up to the caller
    (L9)."""
    if isinstance(url, str) and method:
        url = urllib.request.Request(url, method=method)
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            # Read the SSL context via the host module at call time so a
            # late certifi install by ensure_certifi() is picked up (a plain
            # `from .host import _SSL_CONTEXT` would bind the stale value).
            if host._SSL_CONTEXT is not None:
                return urllib.request.urlopen(url, timeout=timeout, context=host._SSL_CONTEXT)
            return urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (429, 503) and attempt < 2:
                time.sleep(2 ** (attempt + 2))
                continue
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            last_exc = e
            if attempt < 2:
                time.sleep(2 ** (attempt + 2))
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------
# Distro registry
# --------------------------------------------------------------------------
UBUNTU_META_RELEASE_LTS = "https://changelogs.ubuntu.com/meta-release-lts"
UBUNTU_CLOUD_IMAGES_BASE = "https://cloud-images.ubuntu.com/"


@dataclasses.dataclass(frozen=True)
class ResolvedImage:
    name: str
    image_url: str
    hash_alg: str = "sha256"
    hash_hex: Optional[str] = None
    hash_url: Optional[str] = None


def _deb_arch_suffix(guest_arch: str) -> str:
    """Filename token used by Ubuntu mirrors (amd64 / arm64)."""
    return "arm64" if guest_arch == "aarch64" else "amd64"


@dataclasses.dataclass(frozen=True)
class DistroInfo:
    user_data_template: str
    resolve: Callable[[str], ResolvedImage]
    firmware: str = "efi"


# ---- Ubuntu --------------------------------------------------------------
def _discover_ubuntu(
    meta_url: str,
    version_pattern: str,
    error_label: str,
    arch_suffix: str,
) -> tuple[str, str]:
    with _urlopen(meta_url, timeout=20) as r:
        body = r.read().decode("utf-8", errors="replace")

    candidates: list[tuple[tuple[int, int], str, str]] = []
    for block in re.split(r"\n\s*\n", body):
        dist_m = re.search(r"^Dist:\s*(\S+)", block, re.MULTILINE)
        ver_m = re.search(version_pattern, block, re.MULTILINE)
        if not (dist_m and ver_m):
            continue
        parts = ver_m.group(1).split(".")
        if len(parts) < 2:
            continue
        major_minor = (int(parts[0]), int(parts[1]))
        codename = dist_m.group(1).lower()
        candidates.append((major_minor, codename, f"{parts[0]}.{parts[1]}"))

    if not candidates:
        raise RuntimeError(f"No {error_label} releases found in {meta_url}")

    candidates.sort(reverse=True)
    for _, codename, version in candidates:
        check_url = (
            f"{UBUNTU_CLOUD_IMAGES_BASE}{codename}/current/"
            f"{codename}-server-cloudimg-{arch_suffix}.img"
        )
        try:
            req = urllib.request.Request(check_url, method="HEAD")
            _urlopen(req, timeout=10).close()
            return codename, version
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
            raise

    raise RuntimeError(
        f"No {error_label} with a published {arch_suffix} cloud image found "
        f"(searched {len(candidates)} candidates)"
    )


def _discover_ubuntu_lts(arch_suffix: str) -> tuple[str, str]:
    return _discover_ubuntu(
        UBUNTU_META_RELEASE_LTS,
        r"^Version:\s*([\d.]+)\s*LTS",
        "Ubuntu LTS",
        arch_suffix,
    )


def _resolve_ubuntu_lts(guest_arch: str) -> ResolvedImage:
    arch_suffix = _deb_arch_suffix(guest_arch)
    codename, version = _discover_ubuntu_lts(arch_suffix)
    base_url = f"{UBUNTU_CLOUD_IMAGES_BASE}{codename}/current/"
    image_url = f"{base_url}{codename}-server-cloudimg-{arch_suffix}.img"
    return ResolvedImage(
        name=f"Ubuntu {version} LTS",
        image_url=image_url,
        hash_alg="sha256",
        hash_url=base_url + "SHA256SUMS",
    )


# ---- Gentoo --------------------------------------------------------------
GENTOO_CLOUD_BASE = "https://distfiles.gentoo.org/releases/"


def _gentoo_arch_dir(guest_arch: str) -> str:
    """Directory token used by Gentoo mirrors (amd64 / arm64)."""
    return "arm64" if guest_arch == "aarch64" else "amd64"


def _resolve_gentoo(guest_arch: str) -> ResolvedImage:
    arch_dir = _gentoo_arch_dir(guest_arch)
    listing_url = f"{GENTOO_CLOUD_BASE}{arch_dir}/autobuilds/current-di-{arch_dir}-cloudinit/"

    # Retry on transient network flaps.
    html = None
    last_err = None
    for attempt, t in enumerate((20, 40, 60), start=1):
        try:
            with _urlopen(listing_url, timeout=t) as r:
                html = r.read().decode("utf-8", errors="replace")
            break
        except (TimeoutError, socket.timeout) as e:
            last_err = e
            continue
    if html is None:
        raise last_err  # type: ignore[misc]

    # Find the latest cloud-init QCOW2 image.
    # Filenames look like: di-arm64-cloudinit-20260809T234555Z.qcow2
    pattern = rf'href="(di-{arch_dir}-cloudinit-[\dTZ]+\.qcow2)"'
    matches = re.findall(pattern, html)
    if not matches:
        raise RuntimeError(
            f"No di-{arch_dir}-cloudinit-*.qcow2 found at {listing_url}"
        )
    # Sort by timestamp descending to get the latest.
    matches.sort(reverse=True)
    image_filename = matches[0]
    image_url = listing_url + image_filename

    # The listing also has a .sha256 hash file for each image.
    sha_url = listing_url + image_filename + ".sha256"
    return ResolvedImage(
        name=f"Gentoo ({arch_dir})",
        image_url=image_url,
        hash_alg="sha256",
        hash_url=sha_url,
    )


# --------------------------------------------------------------------------
# Resolver registry
# --------------------------------------------------------------------------

DISTROS: dict[str, DistroInfo] = {
    "gentoo": DistroInfo(
        user_data_template=DISTRO_TEMPLATE["gentoo"],
        resolve=_resolve_gentoo,
    ),
    "ubuntu-lts": DistroInfo(
        user_data_template=DISTRO_TEMPLATE["ubuntu-lts"],
        resolve=_resolve_ubuntu_lts,
    ),
}

# The download resolver registry must stay in lockstep with the canonical
# distro list in config.py -- a distro added to one but not the other is a
# bug. Adding a new distro means touching BOTH DISTRO_ORDER (config.py) and
# this dict. user_data_template derives from config.DISTRO_TEMPLATE, so the
# two can't drift.
_registry_mismatch = set(DISTROS) ^ set(DISTRO_ORDER)
if _registry_mismatch:
    raise RuntimeError(
        "distro registry mismatch between config.DISTRO_ORDER and "
        f"download.DISTROS: {sorted(_registry_mismatch)}"
    )


# --------------------------------------------------------------------------
# Download + checksum
# --------------------------------------------------------------------------
def _atomic_replace_with_retry(tmp: Path, dest: Path, attempts: int = 8) -> None:
    """Atomically rename `tmp` -> `dest`, retrying transient PermissionErrors.

    macOS file-indexing watchers (Spotlight etc.) can hold a brief handle on
    a freshly-written file, making the rename fail with EPERM; a short
    retry ladder lands it instead of failing the whole download. Shared by
    the normal and 416-complete paths (M8).
    """
    for rename_attempt in range(attempts):
        try:
            tmp.replace(dest)
            return
        except PermissionError:
            if rename_attempt == attempts - 1:
                raise
            time.sleep(0.5 * (rename_attempt + 1))


def download(url: str, dest: Path, label: str) -> None:
    """Download `url` to `dest` with progress + resume support.

    Retry policy: keep retrying until either the file completes OR the
    30-min total wall-clock budget is exhausted.
    """
    log(f"Downloading {label}: {url}", "step")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    DOWNLOAD_BUDGET_SEC = 1800
    deadline = time.monotonic() + DOWNLOAD_BUDGET_SEC

    last_exc: Exception | None = None
    attempt = 0
    while True:
        attempt += 1
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Download exceeded {DOWNLOAD_BUDGET_SEC}s budget: {last_exc}"
            )
        already = tmp.stat().st_size if tmp.exists() else 0
        if already:
            log(f"Resuming download at {already / 1e6:.1f} MB (attempt {attempt})", "step")
        try:
            req = urllib.request.Request(url)
            if already:
                req.add_header("Range", f"bytes={already}-")
            try:
                with _urlopen(req, timeout=30) as resp:
                    # A server that ignores our Range header answers 200 with
                    # the FULL body; appending that onto the partial file
                    # corrupts the image (M7). Truncate + restart from 0.
                    # The complementary case -- already complete -- surfaces
                    # as HTTP 416 below.
                    if already > 0 and resp.status != 206:
                        log(
                            "Server ignored the Range request (HTTP 200) -- "
                            "truncating partial file and restarting from 0",
                            "warn",
                        )
                        already = 0
                    content_len = int(resp.headers.get("Content-Length", 0))
                    total = already + content_len if content_len else 0
                    mode = "ab" if already else "wb"
                    CHUNK_BYTES = 8192
                    with tmp.open(mode) as out:
                        read = already
                        last_progress_print = 0
                        while True:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    f"Download budget exceeded mid-stream ({DOWNLOAD_BUDGET_SEC}s)"
                                )
                            chunk = resp.read(CHUNK_BYTES)
                            if not chunk:
                                break
                            out.write(chunk)
                            read += len(chunk)
                            now = time.monotonic()
                            if total and (now - last_progress_print) >= 1.0:
                                pct = (read / total) * 100
                                print(
                                    f"\r  {read / 1e6:.1f} / {total / 1e6:.1f} MB ({pct:5.1f}%)",
                                    end="",
                                    flush=True,
                                )
                                last_progress_print = now
            except urllib.error.HTTPError as e:
                if e.code == 416 and already:
                    log(
                        f"Server reports range not satisfiable ({already} bytes "
                        "already present) -- treating download as complete",
                        "info",
                    )
                    _atomic_replace_with_retry(tmp, dest)
                    return
                raise
            if total:
                print()
            _atomic_replace_with_retry(tmp, dest)
            return
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            last_exc = e
            print()
            remaining = deadline - time.monotonic()
            sleep_for = min(15, max(0, remaining - 1))
            log(
                f"Download attempt {attempt} failed: {e}; "
                f"retrying in {sleep_for:.0f}s (budget {int(remaining)}s remaining)",
                "warn",
            )
            if sleep_for <= 0:
                break
            time.sleep(sleep_for)
    raise RuntimeError(f"Download failed (budget exhausted): {last_exc}")


def hash_file(path: Path, alg: str) -> str:
    h = hashlib.new(alg)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_hash(
    image: Path,
    alg: str = "sha256",
    hex_digest: Optional[str] = None,
    sums_url: Optional[str] = None,
) -> None:
    """Verify the hash of an image.

    Provide either `hex_digest` (pre-resolved), or `sums_url` pointing at a
    SHA256SUMS / SHA512SUMS / .sha256 / .SHA256-style file.
    """
    expected: Optional[str] = hex_digest.lower() if hex_digest else None
    hex_len = {"sha256": 64, "sha512": 128}.get(alg, 64)

    if expected is None and sums_url:
        log(f"Fetching {alg.upper()} checksum file ...", "step")
        try:
            with _urlopen(sums_url, timeout=20) as r:
                sums_text = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            # M4: fail closed -- a download whose checksum file can't be
            # fetched must NOT silently downgrade to "verified". A missing
            # SHA file from a bad/compromised mirror previously skipped the
            # integrity check entirely; the image is fetched over the
            # network, so an unverifiable download is an error, not a warning.
            raise RuntimeError(
                f"Could not fetch checksum file {sums_url} ({e}); "
                "refusing to verify the download. Check the mirror / network "
                "and re-run."
            ) from e
        for line in sums_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if not parts:
                continue
            if any(
                p.lstrip("*").endswith(image.name) or image.name in p
                for p in parts
            ):
                for token in parts:
                    candidate = token.lower()
                    if len(candidate) == hex_len and all(
                        c in "0123456789abcdef" for c in candidate
                    ):
                        expected = candidate
                        break
                if expected:
                    break
            if len(parts) == 1 and len(parts[0]) == hex_len:
                expected = parts[0].lower()
                break

    if not expected and sums_url:
        raise RuntimeError(
            f"No {alg.upper()} entry for {image.name} found in {sums_url}; "
            "refusing to verify the download (the mirror may be serving a "
            "stale or mismatched checksum file)."
        )

    if not expected:
        log(f"No {alg.upper()} checksum available; skipping verification.", "warn")
        return

    log(f"Verifying {alg.upper()} ...", "step")
    actual = hash_file(image, alg).lower()
    if actual != expected:
        raise RuntimeError(
            f"Checksum mismatch ({alg})!\n  expected: {expected}\n  actual:   {actual}"
        )
    log(f"{alg.upper()} OK.", "ok")


# --------------------------------------------------------------------------
# GNOME Shell extensions
# --------------------------------------------------------------------------
# The six curated GNOME Shell extensions are installed into the guest from
# tagged GitHub-release ZIPs by _gs_extensions_common.j2. Historically that
# block curled api.github.com DURING cloud-init, which is fragile: the guest's
# network to GitHub is flaky at first boot (the same DNS-flap class of failure
# that hits distro mirrors, HISTORY #13-#15), so installs silently failed and
# left extensions "enabled" (via dconf) but not on disk. To make installs
# deterministic we pre-fetch the zips on the HOST (reliable network + CA
# bundle) and serve them to the guest over QEMU's user-net gateway
# (10.0.2.2) via a tiny HTTP server. The guest fetches from that local URL
# first and only falls back to GitHub if the server isn't offered.
#
# Each entry: (github_repo, extension_uuid). The uuid is also the release
# asset filename (<uuid>.zip) on GitHub.
# Each entry: (repo_label, uuid, direct_source_url|None, github_branch|None).
#   * direct_source_url set -> download that URL directly (non-GitHub hosts,
#     e.g. dynamic-panel-ng on a self-hosted Gitea). Served from host cache.
#   * github_branch set     -> fetch the *branch* archive (used when the latest
#     *tag* is stale but the branch carries the GNOME-48/49/50 code, e.g. the
#     hermes83 compiz extensions whose only tag is a GNOME-44 build).
#   * neither               -> GitHub release asset, then latest tag archive.
GNOME_EXTENSION_REPOS = [
    ("Schneegans/Burn-My-Windows", "burn-my-windows@schneegans.github.com", None, None),
    ("Schneegans/Desktop-Cube", "desktop-cube@schneegans.github.com", None, None),
    ("micheleg/dash-to-dock", "dash-to-dock@micxgx.gmail.com", None, None),
    # Dash to Panel: user-requested as an INSTALLED-BUT-DISABLED alternative to
    # Dash to Dock (both hijack the panel/dash, so only one may be ENABLED at a
    # time). Pinned to v73 (GNOME 46-50; EGO 1160). Its uuid is deliberately
    # OMITTED from the dconf enabled-extensions list in _dconf_common.j2, so it
    # lands on disk but stays off until the user toggles it on in the Extensions
    # app. Source = GitHub release asset (zip already has metadata.json at root,
    # so no repack needed).
    ("home-sweet-gnome/dash-to-panel", "dash-to-panel@jderose9.github.com",
     "https://github.com/home-sweet-gnome/dash-to-panel/releases/download/v73/dash-to-panel%40jderose9.github.com_v73.zip", None),
    # Compiz-style effects: hermes83 originals (EGO 3210 / 3740). Their latest
    # *tag* is a stale GNOME-44 build. compiz-alike-magic-lamp-effect's
    # `master` carries GNOME 45-50 (single build covers all distros).
    # compiz-windows-effect has NO single build spanning GNOME 48-50: `master`
    # is 49-50 only, and the GNOME-48-capable v29 is an older commit
    # (2db7c8801a, GNOME 45-49). So it is fetched as TWO version-specific
    # zips -- `<uuid>.49.zip` (v29, for GNOME <=49) and `<uuid>.50.zip`
    # (master, for GNOME 50) -- and the guest picks by its GNOME version
    # (see _gs_extensions_common.j2). All four distros then get it.
    ("hermes83/compiz-alike-magic-lamp-effect", "compiz-alike-magic-lamp-effect@hermes83.github.com", None, "master"),
    ("hermes83/compiz-windows-effect", "compiz-windows-effect@hermes83.github.com", None, {"49": "2db7c8801a68692ffb44f886b3211c236c3909d0", "50": "master"}),
    # dynamic-panel: velade build covers GNOME 46-49 (Gentoo 49);
    # dynamic-panel-ng (below) covers GNOME 50. Both listed so the
    # shell-version gate installs exactly one per distro (no conflict).
    ("velade/dynamic-panel", "dynamic-panel@velhlkj.com", None, None),
    # dynamic-panel-ng: GNOME-50-only, hosted on a self-hosted Gitea.
    ("jdneer/gnome-dynamic-panel-ng", "dynamic-panel-ng@jdneer.com",
     "https://git.jdneer.com/jd/gnome-dynamic-panel-ng/releases/download/v5.0.1/dynamic-panel-ng@jdneer.com_v5.0.1.zip", None),
]
GNOME_EXT_SERVER_PORT_RANGE = (8753, 8773)


def prefetch_gnome_extensions(cache_dir: Path) -> Path:
    """Download the latest GitHub zip for each extension into
    <cache_dir>/gnome-extensions/<uuid>.zip (host-side, reliable network).

    Source resolution per repo:
      * Try the latest GitHub *release* asset (most repos publish here).
      * Fall back to the latest *tag* archive, re-packed so the extension
        files sit at the zip root (for repos that ship tags but no
        releases, so releases/latest 404s).

    Idempotent: existing non-empty zips are kept. Failures are logged as
    warnings and skipped so one dead repo never aborts the prefetch.
    """
    import tempfile
    import zipfile

    ext_dir = cache_dir / "gnome-extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)

    def _fetch_release_asset(repo: str, uuid: str, dest: Path) -> None:
        api = f"https://api.github.com/repos/{repo}/releases/latest"
        with _urlopen(api, timeout=30) as r:
            rel = json.loads(r.read().decode("utf-8", errors="replace"))
        asset = next(
            (a for a in rel.get("assets", []) if str(a.get("name", "")).endswith(".zip")),
            None,
        )
        if not asset or not asset.get("browser_download_url"):
            raise RuntimeError("no zip asset in latest release")
        download(asset["browser_download_url"], dest, f"GNOME extension {uuid}")

    def _fetch_tag_archive(repo: str, uuid: str, dest: Path) -> None:
        tags_url = f"https://api.github.com/repos/{repo}/tags?per_page=1"
        with _urlopen(tags_url, timeout=30) as r:
            tags = json.loads(r.read().decode("utf-8", errors="replace"))
        if not tags:
            raise RuntimeError("no tags published")
        tag = tags[0]["name"]
        archive_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
        tmp = dest.with_suffix(".tag.zip")
        try:
            download(archive_url, tmp, f"GNOME extension {uuid} (tag {tag})")
            with zipfile.ZipFile(tmp) as zf:
                names = zf.namelist()
                prefix = names[0].split("/")[0] + "/" if names else ""
                if not all(n.startswith(prefix) for n in names):
                    prefix = ""
                with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
                    for n in names:
                        if n.endswith("/"):
                            continue
                        out.writestr(n[len(prefix):], zf.read(n))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _fetch_branch_archive(repo: str, branch: str, uuid: str, dest: Path) -> None:
        # A 40-char hex ref is a commit SHA (used to pin an older build that
        # still supports an older GNOME, e.g. compiz-windows-effect v29 for
        # GNOME 48); otherwise treat it as a branch name.
        if len(branch) == 40 and all(c in "0123456789abcdef" for c in branch):
            archive_url = f"https://github.com/{repo}/archive/{branch}.zip"
        else:
            archive_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
        tmp = dest.with_suffix(".branch.zip")
        try:
            download(archive_url, tmp, f"GNOME extension {uuid} (branch {branch})")
            with zipfile.ZipFile(tmp) as zf:
                names = zf.namelist()
                prefix = names[0].split("/")[0] + "/" if names else ""
                if not all(n.startswith(prefix) for n in names):
                    prefix = ""
                with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
                    for n in names:
                        if n.endswith("/"):
                            continue
                        out.writestr(n[len(prefix):], zf.read(n))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    for repo, uuid, source_url, branch in GNOME_EXTENSION_REPOS:
        if isinstance(branch, dict):
            # Version-specific builds: one cached zip per GNOME-major key
            # (e.g. compiz-windows-effect serves v29 for <=49 and master for
            # 50). The guest picks the right artifact by its GNOME version.
            for key, ref in branch.items():
                dest = ext_dir / f"{uuid}.{key}.zip"
                if dest.exists() and dest.stat().st_size > 0:
                    continue
                try:
                    _fetch_branch_archive(repo, ref, uuid, dest)
                    log(f"GNOME ext {uuid} ({key}): prefetched ({dest.stat().st_size // 1024} KB)", "ok")
                except Exception as e:  # noqa: BLE001
                    log(f"GNOME ext {uuid} ({key}) prefetch failed: {e} -- skipped", "warn")
                    continue
            continue
        dest = ext_dir / f"{uuid}.zip"
        if dest.exists() and dest.stat().st_size > 0:
            continue
        try:
            if source_url:
                download(source_url, dest, f"GNOME extension {uuid}")
            elif branch:
                _fetch_branch_archive(repo, branch, uuid, dest)
            else:
                try:
                    _fetch_release_asset(repo, uuid, dest)
                except Exception:
                    _fetch_tag_archive(repo, uuid, dest)
            log(f"GNOME ext {uuid}: prefetched ({dest.stat().st_size // 1024} KB)", "ok")
        except Exception as e:  # noqa: BLE001 - prefetch is best-effort
            log(f"GNOME ext {uuid} prefetch failed: {e} -- skipped", "warn")
            continue
    return ext_dir


def _pick_free_port(start: int, end: int) -> int:
    for p in range(start, end + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def start_gnome_ext_server(cache_dir: Path):
    """Pre-fetch the extension zips and serve them over HTTP (ephemeral port,
    bound 127.0.0.1) so the guest can reach them at http://10.0.2.2:<port>.

    QEMU's SLIRP user-net routes guest traffic to 10.0.2.2 through the
    host's loopback interface, so 127.0.0.1 is sufficient and avoids
    exposing the extension server on the LAN.

    Returns (subprocess.Popen, port, guest_base_url). The server is a
    detached process that outlives the calling Python (so it survives
    setup_vm.py exiting after it launches the VM); callers that own its
    lifecycle (the fleet orchestrator) must terminate it via
    stop_gnome_ext_server().
    """
    ext_dir = prefetch_gnome_extensions(cache_dir)
    port = _pick_free_port(*GNOME_EXT_SERVER_PORT_RANGE)
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port),
         "--bind", "127.0.0.1", "--directory", str(ext_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, port, f"http://10.0.2.2:{port}"


def stop_gnome_ext_server(proc) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


# --------------------------------------------------------------------------
# Cache management
# --------------------------------------------------------------------------
def download_to_cache(
    url: str,
    cache_dir: Path,
    distro: str,
    label: str,
    expected_hash_alg: str | None = None,
    expected_hash_hex: str | None = None,
    expected_hash_url: str | None = None,
) -> Path:
    """Ensure the cloud image is in the shared cache; return its path."""
    from .config import filename_from_url

    cache_dir.mkdir(parents=True, exist_ok=True)
    distro_cache = cache_dir / distro
    distro_cache.mkdir(parents=True, exist_ok=True)
    cached = distro_cache / filename_from_url(url)
    if cached.exists() and cached.stat().st_size > 0:
        try:
            if expected_hash_alg and (expected_hash_hex or expected_hash_url):
                verify_hash(
                    cached,
                    alg=expected_hash_alg,
                    hex_digest=expected_hash_hex,
                    sums_url=expected_hash_url,
                )
            log(f"Using cached image: {cached} ({cached.stat().st_size / 1e6:.1f} MB)", "ok")
            return cached
        except Exception as e:
            log(f"Cached image failed hash check ({e}); re-downloading", "warn")
            try:
                cached.unlink()
            except OSError:
                pass
    download(url, cached, label)
    if expected_hash_alg and (expected_hash_hex or expected_hash_url):
        verify_hash(
            cached,
            alg=expected_hash_alg,
            hex_digest=expected_hash_hex,
            sums_url=expected_hash_url,
        )
    return cached


def materialize_image(cached: Path, dest: Path) -> None:
    """Place a cached image into the per-VM target dir.

    Strategy:
      1. If dest already exists and is the same file (hardlink), nothing to do.
      2. Try hardlink (same filesystem, instant, zero-byte).
      3. Fall back to copy if hardlink fails (cross-volume, FAT32 dest, etc.).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            if dest.samefile(cached):
                return
        except OSError:
            pass
        try:
            dest.unlink()
        except OSError:
            pass
    try:
        os.link(str(cached), str(dest))
        log(f"Hardlinked cached image -> {dest.name}", "ok")
    except OSError:
        log(f"Copying cached image -> {dest.name} (cross-volume, no hardlink)", "step")
        shutil.copy2(str(cached), str(dest))
