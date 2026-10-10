---
name: update-mesa-fork
description: Pull the latest main branch of the miles.buckton mesa fork inside the VMs, rebuild mesa, and validate with check-virgl and check-venus. Use when the user asks to update/rebuild the mesa fork on the VMs, pull mesa changes, refresh mesa after a fork push, or run the virgl/venus checks after a mesa change. Runs Ubuntu first, then Gentoo.
---

# Update Mesa Fork on VMs

Pulls the latest `main` of `https://gitlab.freedesktop.org/miles.buckton/mesa.git` in each VM's `/var/cache/mesa-fork` clone, rebuilds + reinstalls mesa with that distro's exact meson configuration, then validates VirGL and Venus with the host-side check scripts. **Ubuntu first, then Gentoo.**

## Hard rules

- **One VM at a time.** Boot → work → checks → `poweroff` → wait for QEMU exit → next VM. Never two QEMU processes at once (shared `~/VMs/` cache and port range 2222-2322).
- The check scripts are **bash**, not Python: always `bash scripts/check-*.sh` (never `python3 scripts/check-*.sh`).
- Both checks must **exit 0** on each distro before moving on. Exit 1 = real defect, exit 2 = infra (VM down / SSH / missing tools).
- The check output's Mesa version string must show the **new git hash** (e.g. `Mesa 26.3.0-devel (git-77cb3fe907)`) — that proves the fresh build is what's loaded, not the old one.
- Run all in-guest scripts via `ssh ... 'sudo -n bash -s' <<'EOF' ... EOF` (heredoc quoted — no nested-quote escaping headaches).

## Fixed facts

| | Ubuntu | Gentoo |
|---|---|---|
| VM dir | `~/VMs/ubuntu-lts` | `~/VMs/gentoo` |
| SSH user | `ubuntu` | `gentoo` |
| SSH key | `<vmdir>/ssh_key` | `<vmdir>/ssh_key` |
| Sudo | passwordless (`sudo -n`) | passwordless (`sudo -n`) |
| Mesa clone | `/var/cache/mesa-fork` (root-owned, shallow clone of `main`) | same |
| Build log | `/var/log/mesa-fork-rebuild.log` | same |

SSH port is **dynamic** (2222-2322, probed at launch). Discover it after boot:

```bash
ps -Ao command= | grep -oE 'hostfwd=tcp:127\.0\.0\.1:[0-9]+' | head -1 | grep -oE '[0-9]+$'
# or: head -1 <vmdir>/launch.log   # "[setup_vm launcher] SSH-forward host port: N"
```

SSH invocation used everywhere (user/key/port per table above):

```bash
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
    -o ConnectTimeout=6 -i ~/VMs/<distro>/ssh_key -p <port> <user>@127.0.0.1
```

## Steps (repeat once per distro, order: ubuntu-lts then gentoo)

### 1. Boot + wait for SSH

```bash
nohup ~/VMs/<distro>/launch-vm.sh > ~/VMs/<distro>/launch.log 2>&1 & disown
# then poll every 5s (up to ~200s ubuntu / ~300s gentoo):
ssh ... '<user>@127.0.0.1' 'echo SSH-READY'
```

### 2. Pull latest main + rebuild (one SSH session, as root — clone is root-owned)

The clone is `--depth 1`, and the fork's `main` is occasionally **force-updated** (history rewrite), so use fetch + hard reset, not `git pull`.

Log everything to `/var/log/mesa-fork-rebuild.log` with `tee`. A warm rebuild of this config takes **~1 min** (15-20 min tool timeout is plenty).

**Ubuntu** (mirrors `templates/ubuntu.j2` post_runcmd — do not improvise meson args):

```bash
ssh ... 'ubuntu@127.0.0.1' 'sudo -n bash -s' <<'EOF'
set -euo pipefail
MESA_LOG=/var/log/mesa-fork-rebuild.log
echo "--- rebuild started at $(date -Iseconds) ---" | tee "$MESA_LOG"
cd /var/cache/mesa-fork
BEFORE=$(git rev-parse HEAD)
echo "BEFORE: $BEFORE" | tee -a "$MESA_LOG"
git fetch --depth 1 origin main 2>&1 | tee -a "$MESA_LOG"
echo "=== NEW COMMITS ===" | tee -a "$MESA_LOG"
git log --oneline "$BEFORE..origin/main" 2>/dev/null | tee -a "$MESA_LOG" || echo "(shallow clone: range unavailable)" | tee -a "$MESA_LOG"
git reset --hard origin/main 2>&1 | tee -a "$MESA_LOG"
echo "AFTER: $(git rev-parse HEAD)" | tee -a "$MESA_LOG"
rm -rf builddir
meson setup builddir --prefix=/usr -Dlibdir=/usr/lib/aarch64-linux-gnu \
  -Dplatforms=x11,wayland -Dgallium-drivers=virgl \
  -Dllvm=disabled -Dshared-llvm=disabled -Dvulkan-drivers=virtio \
  -Dbuildtype=release 2>&1 | tail -20 | tee -a "$MESA_LOG"
ninja -C builddir -j$(nproc) 2>&1 | tail -5 | tee -a "$MESA_LOG"
ninja -C builddir install 2>&1 | tail -5 | tee -a "$MESA_LOG"
ldconfig
if ! ls /usr/lib/aarch64-linux-gnu/libgallium-*.so >/dev/null 2>&1; then
  echo 'MESA-FORK-REBUILD-FAIL: fork libgallium missing' | tee -a "$MESA_LOG"; exit 1
fi
ls -la /usr/lib/aarch64-linux-gnu/libgallium-*.so | tee -a "$MESA_LOG"
ls -la /usr/share/vulkan/icd.d/virtio* | tee -a "$MESA_LOG"
touch /var/lib/mesa-fork-installed
echo "MESA-FORK-REBUILD-OK at $(date -Iseconds)" | tee -a "$MESA_LOG"
EOF
```

**Gentoo** (mirrors `templates/gentoo.j2` — no `-Dlibdir`, llvm via slotted native-file, extra drivers):

```bash
ssh ... 'gentoo@127.0.0.1' 'sudo -n bash -s' <<'EOF'
set -euo pipefail
MESA_LOG=/var/log/mesa-fork-rebuild.log
echo "--- rebuild started at $(date -Iseconds) ---" | tee "$MESA_LOG"
MESA_FORK_DIR=/var/cache/mesa-fork
cd "$MESA_FORK_DIR"
BEFORE=$(git rev-parse HEAD)
echo "BEFORE: $BEFORE" | tee -a "$MESA_LOG"
git fetch --depth 1 origin main 2>&1 | tee -a "$MESA_LOG"
echo "=== NEW COMMITS ===" | tee -a "$MESA_LOG"
git log --oneline "$BEFORE..origin/main" 2>/dev/null | tee -a "$MESA_LOG" || echo "(shallow clone: range unavailable)" | tee -a "$MESA_LOG"
git reset --hard origin/main 2>&1 | tee -a "$MESA_LOG"
echo "AFTER: $(git rev-parse HEAD)" | tee -a "$MESA_LOG"
MESA_ARGS="-Dplatforms=x11,wayland -Dgallium-drivers=virgl,llvmpipe -Dllvm=enabled -Dshared-llvm=enabled -Dvulkan-drivers=swrast,virtio -Dbuildtype=release"
rm -rf "$MESA_FORK_DIR/builddir"
LLVM_CONFIG_BIN=$(ls -1 /usr/lib/llvm/*/bin/llvm-config 2>/dev/null | sort -V | tail -1)
printf '[binaries]\nllvm-config = %s\n' "'$LLVM_CONFIG_BIN'" > "$MESA_FORK_DIR/llvm-native.ini"
echo "llvm-config: $LLVM_CONFIG_BIN" | tee -a "$MESA_LOG"
meson setup "$MESA_FORK_DIR/builddir" "$MESA_FORK_DIR" --prefix=/usr \
  --native-file "$MESA_FORK_DIR/llvm-native.ini" $MESA_ARGS \
  2>&1 | tail -25 | tee -a "$MESA_LOG"
ninja -C "$MESA_FORK_DIR/builddir" -j$(nproc) 2>&1 | tail -5 | tee -a "$MESA_LOG"
ninja -C "$MESA_FORK_DIR/builddir" install 2>&1 | tail -5 | tee -a "$MESA_LOG"
[ -x /sbin/ldconfig ] && /sbin/ldconfig || true
if ! ls /usr/lib64/libgallium-*.so >/dev/null 2>&1 && ! ls /usr/lib/libgallium-*.so >/dev/null 2>&1; then
  echo 'MESA-FORK-REBUILD-FAIL: fork libgallium missing' | tee -a "$MESA_LOG"; exit 1
fi
ls -la /usr/lib64/libgallium-*.so 2>/dev/null | tee -a "$MESA_LOG" || true
ls -la /usr/share/vulkan/icd.d/ | tee -a "$MESA_LOG"
echo "MESA-FORK-REBUILD-OK at $(date -Iseconds)" | tee -a "$MESA_LOG"
EOF
```

Template gotchas (from AGENTS.md — these cost multi-hour cycles before):

- Fork default branch is **`main`**, never `master`.
- Gentoo slots LLVM → must use the `llvm-native.ini` native-file or meson fails looking for `llvm.wrap` (newest wins: `sort -V | tail -1`).
- `set -u` + unmatched `ls /usr/lib64/... /usr/lib/...` glob: test each libdir independently.
- Active Vulkan ICD is `virtio_icd.aarch64.json` (arch-suffixed) in `/usr/share/vulkan/icd.d/` on both distros.

### 3. Run both checks (from the repo, on the host)

```bash
cd /Users/milesbuckton/Developer/linux-desktop-vm
bash scripts/check-virgl.sh ~/VMs/<distro>            # expect VIRGL-OK, exit 0
bash scripts/check-venus.sh ~/VMs/<distro> --ring     # expect VENUS-OK + RING-OK, exit 0
```

`--ring` matters: plain `check-venus.sh` only enumerates devices; `--ring` runs the submit/fence self-test that catches silent ring stalls.

Expected healthy output includes `Mesa ... (git-<new-hash>)`, `renderer: virgl`, `EGL driver name: virtio_gpu`, `deviceName = Virtio-GPU Venus (...)`, `RING-OK`. (Gentoo also lists an `llvmpipe` device — expected: its config builds `swrast,virtio`.)

### 4. Shut down and wait for QEMU to exit

```bash
ssh ... '<user>@127.0.0.1' 'sudo poweroff' || true
# poll every 2s until gone: ps -Ao command= | grep -q '[q]emu-system'
```

Then proceed to the next distro. After Gentoo, all QEMU processes should be gone.

## Failure handling

- **SSH never comes up**: tail `<vmdir>/console.log`; if QEMU died, check `~/Library/Logs/DiagnosticReports/qemu-system-aarch64-*.ips` (known HVF/unaligned-blob asserts — see AGENTS.md).
- **`git reset` reports forced update**: normal — fork `main` is sometimes rebased; hard reset is correct.
- **check-virgl exit 1 / llvmpipe renderer**: fresh build didn't land — re-check `ninja install` output and `ldconfig`; confirm `libgallium-*-devel.so` timestamp is new.
- **check-venus exit 1 / VK_ERROR_OUT_OF_HOST_MEMORY**: the 16 KB venus blob alignment commit may be missing from the pulled tree — verify `git log --oneline | grep -iE '16KB|align'`.
- **check exit 2**: infra — VM not fully up, wrong port, or missing `eglinfo`/`vulkaninfo`; do not treat as a mesa defect.
- If a check fails, report the marker lines (`VIRGL-FAIL` / `VENUS-FAIL` / `RING-FAIL`) verbatim before attempting fixes.
