# AGENTS.md — AI assistant guide for `linux-desktop-vm`

This file is the canonical onboarding document for AI assistants (Claude, Copilot, etc.) working in this repo. It covers conventions and gotchas that aren't obvious from reading the code alone. **Read this before making changes.** For deep dives on any topic, refer to [README.md](./README.md) (user docs) or [ARCHITECTURE.md](./docs/ARCHITECTURE.md) (internal flow + timing). This file is a fast-lookup table of "stuff that has bitten us."

## 🎯 What this project is

A Python orchestrator (`linux_vm/` package, invoked via `setup_vm.py` shim) + Jinja2 cloud-init templates that **unattended-installs 4 Linux distros** (Debian, Fedora, Ubuntu LTS, and Gentoo) with **GNOME on Wayland** inside a **QEMU** VM. macOS-only host (Apple Silicon with HVF). One command per VM, fully provisioned.

## 🏗️ Repository layout (key files only)

```
setup_vm.py                       # Thin shim -> linux_vm.__main__ (backwards-compat entry point)
linux_vm/
  __init__.py                     # Package marker
  __main__.py                     # Entry point: routes CLI to orchestrate or monitor
  config.py                       # VMConfig, DISTRO_DEFAULTS, banner, helpers
  host.py                         # HostPlatform, tool discovery
  log.py                          # ANSI colours (C) + log() function (extracted from host.py)
  download.py                     # SSL, _urlopen, download, hash/verify, distro resolvers, DISTROS dict
  templates.py                    # render_jinja2_template, build_seed_iso
  provider.py                     # VmProvider base, QemuProvider, PROVIDERS dict, shared QEMU helpers
  monitor.py                      # monitor_main, _monitor_tail_loop, SSH helpers
  orchestrate.py                  # parse_args, main(), _build_one_vm()
  fleet/
    __init__.py                   # Fleet orchestrator shim (re-exports linux_vm.fleet.*)
    constants.py                  # Fleet-wide constants: timeouts, DISTRO_MIRROR, USERNAMES,
                                   #   DISTROS/PROVIDERS order
    executor.py                   # run_with_hard_timeout, run_to_file (subprocess wrappers)
    ssh.py                        # SSH orchestration: log_master, check_guest_dns,
                                   #   wait_ssh_reachable, ssh_wait_cloud_init, marker checks,
                                   #   diagnostic capture
    lifecycle.py                  # kill_pid, preflight_cleanup, shutdown_and_verify
    orchestrator.py               # build_and_provision, prefetch_images, simulate_distro, simulate_all
    main.py                       # Fleet CLI entry: main() (arg parsing + pipeline orchestration)
README.md                         # Authoritative user-facing docs
docs/
  ARCHITECTURE.md                 # Internal flow + timing, latest-version discovery, fleet building
  CONTRIBUTING.md                 # Contribution guide
scripts/
  build-fleet-sequential.py       # Thin shim -> linux_vm.fleet.main (re-exported from linux_vm.fleet.main)
                                   # Pre-flight gates: lint -> smoke -> prefetch -> simulate -> per-VM build
                                   # Flags: --distros, --prefetch-only, --no-prefetch,
                                   #        --simulate-only, --no-simulate, --no-preflight,
                                   #        --start-from distro
  lint-templates.py               # Gate 0: render every template (real + sim modes), validate YAML,
                                   # assert VERIFY-OK / VERIFY-FAIL / SIMULATE-OK / SIMULATE-FAIL
                                   # markers present. Runs in ~3 sec. Pre-commit gate.
  smoke-test-cli.py               # Gate 1: end-to-end orchestrator <-> setup_vm.py contract check.
                                    # Runs --prefetch + --simulate for ubuntu-lts.
                                   # Catches AttributeError / unknown-arg bugs. ~2-3 min warm.
                                    # Also runs pre-flight host-side DNS check against all
                                     # 4 distro mirrors (DISTRO_MIRROR) and warns on failure.
  audit-packages.py               # Cross-distro package alignment audit (static analysis).
                                   # Prints category x distro table. Run after any new package addition.
templates/
  _base.j2                        # Common cloud-init scaffolding
                                    # Defines blocks: packages, bootcmd, pre/post_runcmd,
                                    #   simulate_install, verify, extra_write_files
                                    # Conditionalised on simulate_only flag for the simulate phase.
   _dconf_common.j2                # System-wide dconf: wallpaper, dark mode, single workspace,
                                     #   Epiphany webextensions. Included by all 3 family/extender templates.
  _plymouth_common.j2             # Shared Plymouth boot-splash setup (theme + grub/initrd regen).
                                     #   Included by family templates in post_runcmd; simulate VMs
                                     #   also include it (theme + initrd skipped, no plymouth) so
                                     #   simulate VMs get the video= grub args and render at full
                                     #   window size.
   _app_platforms_common.j2        # Shared vendor-app platform installs (Google Chrome, Flathub
                                     #   Flatseal/Gear Lever, PowerShell). Best-effort.
                                     #   Included by all 3 family/extender templates in post_runcmd.
  _runcmd_common.j2               # runcmd boilerplate: graphical.target + GDM enable (skipped where
                                     #   the desktop meta enables GDM itself). Included by _base.j2.
   _write_files_common.j2          # write_files boilerplate: /etc/issue login banner. Included by _base.j2.
    ubuntu.j2                       # Standalone APT template (ubuntu.j2 inlined _apt_family.j2
                                     #   content).
    meta-data.j2                    # cloud-init meta-data
  network-config.j2               # Netplan YAML template for DHCP on e* interfaces
```

## 🛡️ Pre-flight gates (run in this order before any real build)

The fleet orchestrator runs four cheap-to-expensive gates that catch
failures fast. Each gate's purpose is to surface a specific class of
bug in seconds-to-minutes instead of hours.

| Gate | Trigger | Time | Catches | Hard/soft fail |
|---|---|---|---|---|
| **0. Lint** | `python scripts/lint-templates.py` | ~3 sec | Jinja syntax, YAML structure, missing verify/simulate blocks | Hard (exit non-zero) |
| **1. Smoke** | `python scripts/smoke-test-cli.py` | ~2-3 min | Orchestrator ↔ setup_vm.py CLI contract mismatches | Hard |
| **2. Prefetch** | Fleet auto, or `--prefetch-only` | ~10-15 min cold, ~1 min warm | Dead upstream URLs, network/firewall | Hard (fleet aborts) |
| **3. Simulate** | Fleet auto, or `--simulate-only` | ~20-25 min warm for all 2 distros | Package resolver failures (apt dry-run; includes vendor-repo Chrome) | Soft (table at end; iterate) |

**Standing rule:** After changing any of:
- A `templates/*.j2` file → run lint
- `setup_vm.py` argparse / render-context / CLI flags → run smoke + lint
- `scripts/build-fleet-sequential.py` subprocess.run calls → run smoke
- Any package list in a template → run simulate for that distro

## 🔒 Verify-block: the build-success contract

Every distro template's `runcmd` ends with a `{% block verify %}` that
checks the desktop core is actually installed:
- `gnome-shell`, `gdm`/`gdm3`, `gnome-control-center`, `nautilus`
- `gnome-software`

On miss: emits `VERIFY-FAIL: <reason>` and exits non-zero (cloud-init
flags runcmd as failed). On full pass: emits `VERIFY-OK: all required
components present`. The orchestrator's `_check_success_marker` looks
for `VERIFY-OK` literal in `cloud-init-output.log` — without it, an
`error - done` status is treated as a real failure (no more masking
half-empty installs as success).

## ⚠️ YAML / Jinja2 escape gotchas (have bitten us 3+ times)

When editing templates that produce cloud-init YAML, these escapes are non-obvious:

| Want literal in shell | Write in YAML | Why |
|---|---|---|
| `\n` (printf format) | `\\n` | YAML eats single `\n` as a newline; double escape so YAML produces `\n` for printf |
| `'` (single quote inside YAML double-quoted string) | use bash-double-quoted: `printf "...'literal'..."` then YAML-escape as `\"` | YAML rejects `\'` as "unknown escape character" |
| `$` (preserve for shell to expand) | `$VAR` inside YAML single-quoted, or `\\$VAR` inside YAML double-quoted | YAML double-quoted doesn't expand but Jinja2 might still touch it |
| `\\` (literal backslash for shell) | `\\\\` in YAML double-quoted | YAML eats one, Jinja2 eats none, shell sees one |
| `{%- block` strips leading newline | Remove the `-` from `{%- block` | Jinja2's whitespace control (`{%-`) eats the newline before the child's content. If the preceding YAML entry is a literal block (`|`), the next entry merges into it and gets silently skipped by cloud-init. Use `{% block` (no dash) in YAML block contexts. |
| `{%- block` strips leading newline | Remove the `-` from `{%- block` | Jinja2's whitespace control (`{%-`) eats the newline before the child's content. If the preceding YAML entry is a literal block (`|`), the next entry merges into it and gets silently skipped by cloud-init. Use `{% block` (no dash) in YAML block contexts. |
| **Invalid top-level cloud-config keys → schema error aborts `bootcmd`** | Keep `user-data` to valid keys only (`hostname`, `bootcmd`, `runcmd`, `packages`, `write_files`, `ssh_authorized_keys`, `users`, `timezone`, `locale`, `output`, `manage_etc_hosts`, etc.) | `network:` and `datasource_list:` are **NOT** valid top-level cloud-config keys (network goes in the separate `network-config` doc); an invalid-shape `output:` (e.g. `output:` nested under `output:`) is also rejected. cloud-init 25.1 reports `schema errors: Additional properties are not allowed` and **aborts the entire bootcmd module**, so every `bootcmd:` entry silently fails to run. The NoCloud datasource + `network-config` are supplied by the seed ISO, so just drop these keys from `user-data`. Fix confirmed on Gentoo: removing them made bootcmd run. |
| **`bootcmd` entry silently skipped (multi-line `bash -c` scalar)** | Each statement on its own line; `echo` progress markers; never `#` mid-line after `;` | A `bootcmd:` entry written as a double-quoted YAML scalar that contains `# comment` right after `stmt;` (no newline) comments out the rest of the command; a child line indented **12 spaces instead of 10** becomes a YAML block-collection that breaks the entry. In both cases the entry produces no output and is effectively dropped (cloud-init still reports bootcmd "SUCCESS"). Symptom: a specific entry (e.g. the long Portage-sync or binhost-config step) never appears in `/var/log/cloud-init-bootcmd.log`. Fix: keep bootcmd scripts flat with `echo` markers; ensure every line inside the scalar is 10 spaces; no inline `#`. |
| `EMERGE_DEFAULT_OPTS` / `PYTHON_TARGETS` written to `make.conf` need quoting/targets | `EMERGE_DEFAULT_OPTS="--jobs=4 --load-average=8 --binpkg-respect-use=n"`; `PYTHON_TARGETS="python3_14"`; `PYTHON_SINGLE_TARGET="python3_14"` | Portage make.conf is shell-assigned: an unquoted value with a space (`EMERGE_DEFAULT_OPTS=--jobs=4 ...`) is a syntax error ("Invalid token '4'"); `binpkg-respect-use` is an *emerge flag*, not a make.conf variable (put it in `EMERGE_DEFAULT_OPTS`, not as its own line); the 23.0 `desktop/gnome/systemd` profile defaults to `python3_14` and the official arm64 binhost is built for that same target, so pin `PYTHON_TARGETS="python3_14"` + `PYTHON_SINGLE_TARGET="python3_14"` and every package (incl. `dev-python/pycairo`, `x11-base/xorg-drivers`) resolves as a binpkg. |
| **`bootcmd` is a single blocking stage — a hang in it blocks SSH + the whole boot** | Keep `bootcmd` to *fast, idempotent* prep only (network/DNS, locale gen, repo registration). Never put a slow/flaky step (Portage tree sync, large `emerge`) in `bootcmd`. | cloud-init runs `bootcmd` as one blocking unit; if a step inside it hangs (e.g. an unreliable `emaint sync` rsync), the bootcmd lock is held and `sshd` (started later in `runcmd`/`pre_runcmd`) never comes up, so the guest is unreachable and the build looks like a silent failure. Gentoo's fix: `bootcmd` does minimal prep, an `early-ssh.service` (`Before=cloud-init.target`) brings `sshd` up *before* cloud-init, and the real install runs in `gentoo-install.service` (written via `extra_write_files`, `After=sshd.service`), fully decoupled from cloud-init's lock. **Verify on every Gentoo change:** boot a real VM and `ssh` must answer within ~30s of QEMU start. |
| Place a fix in a child template **outside any block** → silently dropped in simulate mode | Put it inside an **always-rendered** `{% block %}` (added to `_base.j2`) | In Jinja inheritance, text in a child template that is not inside a block only appears if it lands inside an *always-rendered* block in the parent. `_base.j2` nests `{% block packages %}` inside `{% block package_management %}`, which in **simulate mode** renders only the `# SIMULATE` comment (no `packages:`). A Gentoo fix that put `network:`/`datasource_list:` right after `{% endblock %}` worked in real builds but was **dropped in simulate mode** — so the simulate gate kept hanging with no error. Fix: add a `{% block cloud_init_wiring %}{% endblock %}` to `_base.j2` (unconditionally, before `package_management`) and override it in the child. Verify both `simulate_only=True` and `=False` render the keys. |

**Always validate after editing templates:**

```python
import yaml
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
ctx = {'HOSTNAME':'h','USERNAME':'u','PASSWORD':'p','ROOT_PASSWORD':'r','TIMEZONE':'UTC',
       'PROVIDER':'qemu',
       'SSH_PUBLIC_KEY':'ssh-ed25519 AAAA test',
       'INSTANCE_ID':'i','LAUNCH_DATE':'d'}
templates = {

    'ubuntu-lts': 'ubuntu.j2',
}
for distro, template in templates.items():
    ctx['PROVIDER'] = 'qemu'
    yaml.safe_load(env.get_template(template).render(**ctx))
print('all 2 (2 distros × 1 provider) parse OK')
```

If this passes, the renders are syntactically valid. It doesn't catch logic bugs (wrong package names, bad repo URLs) — that's what the fleet test does.

## 📦 Per-distro package name reference

Same app, different package name per distro. See the "Package naming differences" table in [README.md](./README.md) for the full reference. Check that table before adding a new package to multiple templates.

## 🌍 Timezone + locale defaults

Templates default to `Africa/Johannesburg` (SAST, UTC+02:00) and `en_ZA.UTF-8`. They also generate `en_GB.UTF-8` and `en_US.UTF-8` so all three English variants are available. APT translations are disabled.

## 🔄 Cloud-init module ordering

When you need something registered before `packages:` is processed, it goes in `bootcmd`. When you need it after package install but before runcmd, use `pre_runcmd`. Pattern:

```mermaid
flowchart LR
    bootcmd["bootcmd"]:::early --> packages["cc_package_update_upgrade_install<br/>(processes packages: list)"]:::mid
    packages --> runcmd["runcmd / pre_runcmd / post_runcmd"]:::late

    classDef early fill:#1b5e20,color:#fff
    classDef mid fill:#0d47a1,color:#fff
    classDef late fill:#4a148c,color:#fff
```

**Practical rules:**
- **Add a repo so a package can install**: `bootcmd` (always before packages)
- **Install an extra app that needs an already-installed dep**: `post_runcmd`
- **Anything that touches the package manager from a cloud-init script**: Do NOT call `cloud-init status --wait` from within cloud-init's own `runcmd` — it causes self-deadlock because `runcmd` IS the final cloud-init stage. Either wait for the package manager lock directly, or have the host orchestrator wait for cloud-init completion.

## 🚦 Build orchestration patterns

| Scenario | Command |
|---|---|
| Build one VM (the "real user" path, see README Quick Start) | `python setup_vm.py --distro ubuntu-lts --provider qemu` |
| Build one VM without booting (phase 1 only) | Default (omit `--start`) |
| Build one VM AND boot it (phase 1 + phase 2) | Add `--start` |
| Re-build (skip re-download) | Add `--keep-qcow2` |
| Build all 2 (2 distros × 1 provider) sequentially, real-user simulation (build + wait for cloud-init each) | `python scripts/build-fleet-sequential.py` |
| Resume fleet from a specific distro | `python scripts/build-fleet-sequential.py --start-from gentoo --no-prefetch` |
| Build with target dir override | `--target-dir ~/VMs/my-custom-name` (default = `~/VMs/<distro>-<provider>/`) |

## 📝 Git / commit convention

- **No PRs. Ever.** Merge directly to `main` and force-push. Do not create feature branches or pull requests.


## 🛠️ Common change patterns

### Adding a new app to all 4 distros

1. Look up per-distro package name in the "Package naming differences" table in [README.md](./README.md) (verify via distro package indexes where needed).
2. Add to each template's appropriate insertion point:
   - apt/dnf: `packages:` list (in `{% block packages %}`) OR post_runcmd best-effort line
3. If the app needs a new vendor repo, register in `bootcmd` first.
4. Run the validation Python snippet above.
5. Update README's `## Apps preinstalled on every VM` table.
6. `git add -A && git commit --amend --no-edit && git push --force-with-lease origin main`.

### Adding a new distro

**The matrix is 2 distros: ubuntu-lts, gentoo**. Only extend it with a strong reason — each new distro multiplies the fleet test matrix and the per-distro template surface. The steps to add one are:

1. Add a resolver in `linux_vm/download.py` (`_resolve_<distro>()`) — see existing patterns; pick from the distro's official cloud image directory.
2. Add to `DISTROS` dict in `linux_vm/download.py`.
3. Add to `DISTRO_DEFAULTS` in `linux_vm/config.py`.
4. Create `templates/<distro>.j2` extending the closest family or another concrete template; include the `{% block verify %}` AND `{% block simulate_install %}` + `{% block extra_write_files %}` (conditional on `simulate_only`).
5. Add to README's supported-distros table.
6. Add to `DISTRO_ORDER` in `linux_vm/config.py` (insert in easiest→hardest position; the fleet builder + lint + audit scripts all derive their distro list from it — `scripts/build-fleet-sequential.py` is a thin shim).
7. Add to `DISTRO_TEMPLATE` in `linux_vm/config.py` (the single source of truth; `download.DISTROS[].user_data_template`, the lint script, and the audit script all derive from it).
8. Run `scripts/lint-templates.py` + `scripts/smoke-test-cli.py` BEFORE committing.

## 🩹 Known battle scars

**Host-level gotchas that bite during fleet builds:**
- **Host CPU starvation**: on ≤4-physical-core Macs, a full-core VM + the OpenChamber app + agent polling saturates the host (load 8 on 4 cores) → guest kernel soft lockups (`stuck for 34s! [swapper/*]`) → cloud-init stalls → 60-min wait timeouts. It also collapses the orchestrator's 5-min SSH probe cadence (single wait-log lines, 40+ min gaps). Default VM is **half the host's physical cores (clamped 2-8) / 8 GB RAM / 80 GB disk** (e.g. 8 vCPU on an 18-core Apple Silicon MacBook Pro, 2 vCPU on a 4-core Intel); run fleet builds with the machine otherwise idle.
- **Firefox is deliberately not installed**: Ubuntu's metapackages only *Recommend* it (via the snap shim), and the `snapd` pin at −1 in the apt family's parameterised `99-no-bloat.pref` bootcmd keeps that recommendation unresolvable — no extra countermeasure needed. Epiphany is the browser everywhere.
- **macOS DNS flap**: `getent hosts` can fail for everything while Python `socket.gethostbyname` (what the fleet uses) works fine. Probe with `python -c "import socket; socket.gethostbyname('<mirror>')"` and wait the flap out before retrying.

**Guest-arch gotchas that bite the aarch64 simulate/build path:**
- **aarch64 cloud-init seed must be `virtio-blk-pci`, NOT usb-storage**: cloud-init's generator runs `ds-identify` at ~1s; USB storage enumerates too late under TCG, so cloud-init disables itself for the whole boot (no user-data, no network). x86_64 keeps `ide-cd` (visible immediately). Rule: any change to seed attachment must be verified on BOTH arches.
- **QEMU `virt`-machine keyboard is PL050 PS/2 — aarch64 distro kernels don't ship the PL050 driver, so GDM has NO keyboard**: the mouse works only because the launcher adds `usb-tablet`. Fix: always add `-device usb-kbd,bus=xhci.0` next to the tablet (kernel `usbhid` is universal). Verify via `/proc/bus/input/devices` showing "QEMU QEMU USB Keyboard".

**The `GLD_TEXTURE_INDEX_2D is unloadable` message on QEMU stderr is benign** (Apple GLES + virglrenderer, `log once` + `gst-plugin-scan` virgl probes). Don't chase it.

## 🚧 Constraints (project scope: 2 distros)

| | |
|---|---|
| **Distros** | The 2 distros in the matrix: ubuntu-lts, gentoo. |
| **Init** | systemd-only. Templates assume `systemctl enable/start`. Non-systemd distros (Alpine OpenRC, Void runit, Devuan sysvinit) are out of scope. |
| **DE** | GNOME only. Other DEs (KDE/XFCE/Cinnamon) not planned. |
| **Wayland** | Default session. X11 fallback exists in some templates but Wayland is the supported configuration. |
| **Atomic / image-based** | Out of scope (Silverblue, Bazzite, etc.). Standard package-manager install only. |
| **Architecture** | aarch64 is the validated primary path on Apple Silicon (Apple Silicon MacBook Pro). x86_64 guests share the same templates/gates and were validated on the earlier Intel host. |

## 🗣️ Working with the user (Miles Buckton)

- He values **honest assessments** over enthusiastic agreement. Flag risks and gotchas proactively.
- He's iterating, so expect frequent additions/changes to the curated app set.
- He runs **multi-hour overnight builds** to validate fleet-wide changes.
- He likes **structured choice menus** for design decisions (the `ask_user` tool with numbered options + recommended).
- **Latest version only** — never preserve back-compat for older majors.
- He runs on **macOS** (was Intel i7-4870HQ, 4 physical cores; now Apple Silicon MacBook Pro 18 cores, QEMU/HVF). Keep the host otherwise idle during fleet builds — a VM using all the host's cores while the app + agent polling run can soft-lock the guest (see Known battle scars).
- He prefers **explicit over implicit** — even if a package is pulled in transitively, add it to the install list for clarity.
- He expects **autonomous iteration on simulate failures** — don't ask for input on every package fix; iterate the simulate-fix-rerun loop and only escalate when the fix would change user-facing behaviour or >3 unrelated failures suggest a systemic issue.
- He expects **comprehensive forensics on real-build failures** — every failure should produce enough diagnostic dump that the assistant can root-cause without operator input. New `UNKNOWN`-category patterns should be proactively added to the categoriser.
- **Real fleet builds have passed** — the fleet completed the full matrix with `VERIFY-OK`. A clean all-2 fleet run lands near **~2.6 h wall** on the Apple Silicon MacBook Pro (ubuntu-lts 21.5 min + gentoo 134 min = ~156 min guest provisioning; plus prefetch/simulate gates + shutdown overhead → ~2.6 h total). See ARCHITECTURE.md for detailed timing.

## 📚 Where to look next

- **User-facing docs**: [README.md](./README.md)
- **Architecture, timing, fleet building**: [ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- **Day-2 ops** (snapshots, SSH, etc.): README's "Day-2 operations" section
