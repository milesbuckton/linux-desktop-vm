# linux-desktop-vm — Architecture & Fleet Guide

This document covers the internal architecture, build phases, pre-flight
gates, timing data, and fleet-building orchestration. It's intended for
developers and maintainers — end users should start with [README.md](../README.md).

## Template hierarchy

```mermaid
flowchart TB
  base["_base.j2"]
  ubuntu["ubuntu.j2"]
  gentoo["gentoo.j2"]

  base --> ubuntu
  base --> gentoo
```

Each concrete template is self-contained. `ubuntu.j2` owns the full APT
stack (inlined from the retired `_apt_family.j2`).

## Latest-Version Discovery

Each distro's "latest" is resolved at runtime, not hardcoded:

| Distro | Source of truth |
|--------|----------------|
| Ubuntu LTS | Canonical cloud-image metadata; highest supported LTS wins |

When a new release ships (Ubuntu 28.04 LTS, etc.), the script
picks it up automatically with no code change.

## How It Works (internal flow)

1. Detect host (macOS on x86_64 or arm64) and locate QEMU tools.
2. Auto-install `qemu-img` if missing (via Homebrew).
3. Auto-install `pycdlib`, `certifi`, and `jinja2` pip packages. Inside a venv this installs directly into the venv; outside, it uses `--user` scope with a `--break-system-packages` fallback for PEP 668 (externally-managed) environments.
4. Resolve the chosen distro's latest cloud image URL + hash.
5. Download + verify the cloud image (SHA256 or SHA512 depending on distro).
6. Resize qcow2 to target disk size using `qemu-img resize`.
7. Render cloud-init templates with hostname / username / password / TZ.
8. Build a NoCloud seed ISO (volume label `cidata`) using `pycdlib`.
9. Render the VM definition file (shell launcher script).
10. Start the VM via the shell launcher script (QEMU on macOS with HVF).

Inside the guest, cloud-init then:
- Sets hostname, creates the user with sudo, sets the password
- Updates packages and installs the desktop (distro-specific install path)
- Sets `graphical.target` as default, enables the display manager
- Reboots into the GDM login screen (Wayland session)

## Code Organization

The orchestration lives in `linux_vm/` (see [AGENTS.md](../AGENTS.md)'s repository layout).
`_build_one_vm()` in `linux_vm/orchestrate.py` delegates to
single-responsibility helpers:

1. **`_find_or_install_qemu_img(host)`** - Locate or install qemu-img
2. **`_install_if_missing(installer_func, package_name)`** - Centralized prerequisite dependency handling (the `ensure_pycdlib` / `ensure_certifi` / `ensure_jinja2` installers catch their own ImportErrors)
3. **`_build_vm_config(args, host, defaults, resolved)`** - Build and configure VM configuration with distro-specific VM name logic (uses the already-resolved distro object; resolve happens once before config)
4. **`_ensure_image(resolved, qcow2, cache_dir, distro)`** - Download, hash verification, and cache management. The shared-cache path (`download_to_cache`) verifies the hash once on the cached file; a hardlink into the target dir shares that inode so it is not re-hashed, while a real cross-volume copy gets its own verification. A stale target image is detected by the reuse-path hash check and re-downloaded.

`_build_one_vm()` runs these in four phases: provider detection, host-side
preparation, image resolution/caching, and launch (template rendering +
VM startup).

## Pre-flight gates

The fleet orchestrator runs four cheap-to-expensive gates that catch
failures fast instead of discovering a typo in a 6-hour fleet run.

### Gate 0 — Lint (~3 sec)

`python scripts/lint-templates.py` renders every (distro × provider ×
mode) combo via jinja2, validates the YAML, and asserts every render
contains the `VERIFY-OK` / `SIMULATE-OK` markers. Currently 12/12 pass
(2 distros × 2 arches × real+sim+parity modes).

### Gate 1 — Smoke test (~2-3 min warm, ~5-10 min cold)

`python scripts/smoke-test-cli.py` exercises the orchestrator ↔
setup_vm.py CLI contract by running `--prefetch` and `--simulate`
end-to-end for gentoo. The shim delegates to
`linux_vm.orchestrate.main()`; catches AttributeError-class bugs where
the orchestrator passes an arg the CLI doesn't know about.

### Gate 2 — Prefetch (~10-15 min cold, ~1 min warm)

`build-fleet-sequential.py --prefetch-only` warms the shared image cache
at `~/VMs/cache/<distro>/` before any VM build starts. Hard-fail: if
any URL is dead, the fleet aborts immediately. Cache survives across
runs; subsequent runs are SHA-verification only.

### Gate 3 — Simulate (~25 min for all 2, ~5-12 min per single)

`build-fleet-sequential.py --simulate-only` runs a dry-run package
resolver per distro: `apt-get install --simulate` / `dnf install
--assumeno`.
Hard-fail: per-distro PASS/FAIL/ERROR table printed at the end.

## Per-VM build phases

Once the gates pass, each VM goes through:

- **Phase 1 — host-side build** (~2-10 min): download cloud image
  (from cache), resize to target disk size, generate seed ISO, launch VM.
- **Phase 2 — guest cloud-init install** (~5-17 min measured, ~10-275 min
  worst case): cloud-init
  runs bootcmd + packages + runcmd + verify-block; orchestrator polls
  via SSH every 5 min until `done` / `degraded done` or `error - done`.
- **Verify-block** (~5-10 sec, last runcmd entry): asserts every
  README-promised component is installed; emits `VERIFY-OK` on success.
  Orchestrator only treats a build as success if this marker is present.

## Timing

### Phase 1 — host-side build (~1-5 min, measured on macOS host)

Representative numbers from a warm-cache fleet build on macOS host
with an SSD:

| Distro | QEMU (min) | Cloud image (~MB) | Notes |
|--------|:---------:|:-----------------:|-------|
| ubuntu-lts | 0.7 | ~700 | |
| gentoo | ~2 | ~500 | Stage3 cloud image + Portage tree sync |

Phase 1 step breakdown (averaged):

| Step | QEMU |
|------|------|
| Resolve (network) | <2 s |
| Download (~300 MB - 2 GB) | 30 s - 4 min |
| Resize disk | ~5 s (qemu-img resize) |
| Build seed ISO + definition | <5 s |

### Phase 2 — guest cloud-init install (~15-26 min)

Timings below assume an **idle host**. Under host CPU contention (app +
agent actively polling a 4-core Mac while the VM installs) they balloon
badly: ubuntu-lts exceeded the
60-min wait timeout entirely (see -#15).

The Phase 2 timing has improved dramatically after fixing orphaned SSH child processes. The fix prevents the process-hanging issue that caused times to exceed 279+ minutes.

Measured from a full-fleet re-run (Apple Silicon MacBook Pro, 24 GB RAM, 8 vCPU, warm cache):

| Distro | Total (min) | Notes |
|--------|:---------:|-------|
| ubuntu-lts | 21.5 | apt cloud-init ~20.5 min (phase-2), shutdown ~0.8 min |
| gentoo | 134 | cloud-init ~134 min (gentoo-install.service), shutdown ~0.8 min; wobbles ±10 min with binhost warmth + guest-DNS flap retries |

### Total wall time = phase 1 + phase 2 + shutdown

Measured from a full-fleet re-run (Apple Silicon MacBook Pro, 24 GB RAM, 8 vCPU, warm binhost):

| Distro | Total (min) |
|--------|:---------:|
| ubuntu-lts | 21.5 |
| gentoo | 134 |

**Key Improvement Summary:**
- **SSH Child Process Fix**: After fixing orphaned SSH child processes that were hanging indefinitely, Phase 2 times dropped from 279+ minutes to ~15 minutes
- **Success Rate**: Previously unpredictable hangs, now all distros complete in predictable timeframes
  - **Fleet Total**: A clean all-2 run lands near **~2.6 h wall** on the Apple Silicon MacBook Pro (ubuntu-lts 21.5 min + gentoo 134 min = ~156 min guest provisioning; plus prefetch/simulate gates + shutdown overhead → **~2.6 h total**).

The Phase 2 timing shows the impact of the SSH child process fix — most distros now complete in ~15 minutes instead of hanging for 279+ minutes.


### Day-2 patterns

All day-2 installs (previously handled by `install-day2-packages.service`)
have been removed from the stack. No heavy post-boot downloads occur;
everything installs during cloud-init's runcmd phase.

## Fleet building

`build-fleet-sequential.py` builds all 4 supported distros for QEMU
in sequence. It runs one VM at a time so memory stays bounded.

Each VM defaults to **half the host's physical CPU cores (clamped 2-8) /
8 GB RAM / 80 GB disk** — e.g. 8 vCPU on an 18-core host, 2 vCPU on a
4-core host. `recommended_vcpus()` in `host.py` reads `sysctl hw.physicalcpu`
(physical, not logical: hyperthreaded Intel Macs would otherwise
double-count). Halving leaves the other half of the cores for the host and
other apps: a full-core VM on a small contended host starves the guest
(kernel soft lockups → cloud-init stalls → wait timeouts) and throttles the
orchestrator's own SSH probes (see ). For
fastest, most reliable runs, keep the machine otherwise idle during a
fleet build.

```bash
python build-fleet-sequential.py

# Or build a specific subset, in the order you want:
python build-fleet-sequential.py --distros ubuntu-lts,gentoo
# Order is honoured: ubuntu-lts-qemu -> gentoo-qemu -> ...
```

Per-VM logs:
- `~/VMs/<distro>-<provider>.build.log` — phase 1
- `~/VMs/<distro>-<provider>.wait.log` — phase 2 (cloud-init monitor)
- `~/VMs/build-fleet.log` — master status

Flags:
- `--distros d1,d2,...`: scope to specific distros (default: all 4)
- `--prefetch-only`: run Gate 2 only (warm cache)
- `--simulate-only`: run Gate 3 only (dry-run package resolver)
- `--no-prefetch` / `--no-simulate`: skip individual gates
- `--no-preflight`: skip the preflight orphan-VM cleanup (only kills leftover qemu-system processes under `~/VMs`)

## Watching progress

The script captures the guest's serial console (ttyS0) to
`<target_dir>/console.log`. The `monitor` subcommand has two modes:

**Phase-bar mode** (default):
```bash
python setup_vm.py monitor ~/VMs/ubuntu-lts-qemu
python setup_vm.py monitor ~/VMs/ubuntu-lts-qemu --once  # snapshot, exit
```

Live progress bar shows 5 phases with ~30-sec SSH queries for the
current cloud-init module name.

**Live-tail mode** (`--tail`):
```bash
python setup_vm.py monitor ~/VMs/ubuntu-lts-qemu --tail
```

Streams every package-manager line via SSH. Requires `paramiko`
(`pip install paramiko`) and the per-VM `ssh_key` file.