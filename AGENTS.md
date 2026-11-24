# AGENTS.md — AI assistant guide for `linux-desktop-vm`

This file is the canonical onboarding document for AI assistants (Claude, Copilot, etc.) working in this repo. It covers conventions and gotchas that aren't obvious from reading the code alone. **Read this before making changes.** For deep dives on any topic, refer to [README.md](./README.md) (user docs) or [ARCHITECTURE.md](./docs/ARCHITECTURE.md) (internal flow + timing). This file is a fast-lookup table of "stuff that has bitten us."

## 🎯 What this project is

A Python orchestrator (`linux_vm/` package, invoked via `setup_vm.py` shim) + Jinja2 cloud-init templates that **unattended-installs 2 Linux distros** (Ubuntu LTS and Gentoo) with **GNOME on Wayland** inside a **QEMU** VM. macOS-only host (Apple Silicon with HVF). One command per VM, fully provisioned.

## 🏗️ Repository layout (key files only)

```
setup_vm.py                       # Thin shim -> linux_vm.__main__ (backwards-compat entry point)
linux_vm/
  __init__.py                     # Package marker
  __main__.py                     # Entry point: routes CLI to orchestrate or monitor
  config.py                       # VMConfig, DISTRO_DEFAULTS, banner, helpers
  host.py                         # HostPlatform, tool discovery
  log.py                          # ANSI colours (C) + log() function (extracted from host.py)
  download.py                     # SSL, _urlopen, download, hash/verify, distro resolvers, DISTRO_REGISTRY dict
  templates.py                    # render_jinja2_template, build_seed_iso
  provider.py                     # Running-VM discovery: list_running_qemu_pids, find_running_ssh_port
  monitor.py                      # monitor_main, _monitor_tail_loop, SSH helpers
  orchestrate.py                  # parse_args, main(), _build_one_vm()
  qemu.py                         # QEMU argv builder, disk prep, launch script
  audit.py                        # Package-alignment audit logic (used by scripts/audit-packages.py)
  test_context.py                 # Shared template-render context for tests/lint
  venv_bootstrap.py               # Shared venv/dependency bootstrap for entry scripts
  fleet/
    __init__.py                   # Fleet orchestrator shim (re-exports linux_vm.fleet.*)
    constants.py                  # Fleet-wide constants: timeouts, DISTRO_MIRROR, USERNAMES,
                                   #   DISTROS order
    executor.py                   # run_with_hard_timeout, run_to_file (subprocess wrappers)
    ssh.py                        # SSH orchestration: log_master, check_guest_dns,
                                   #   wait_ssh_reachable, ssh_wait_cloud_init, marker checks,
                                   #   diagnostic capture
    lifecycle.py                  # acquire_fleet_lock, kill_pid, preflight_cleanup, shutdown_and_verify
    orchestrator.py               # build_and_provision, prefetch_images, simulate_distro, simulate_all
    main.py                       # Fleet CLI entry: main() (arg parsing + pipeline orchestration)
README.md                         # Authoritative user-facing docs
docs/
  ARCHITECTURE.md                 # Internal flow + timing, latest-version discovery, fleet building
  CONTRIBUTING.md                 # Contribution guide
scripts/
  build-fleet-sequential.py       # Thin shim -> linux_vm.fleet.main (re-exported from linux_vm.fleet.main)
                                   # Pre-flight gates: lint -> audit -> pylint -> tests -> smoke -> prefetch -> simulate -> per-VM build
                                   # Flags: --distros, --no-lint, --no-audit, --no-pylint, --no-tests,
                                   #        --no-smoke, --prefetch-only, --no-prefetch,
                                   #        --simulate-only, --no-simulate, --no-preflight,
                                   #        --start-from distro
                                   # Conflicting pairs (--simulate-only --no-simulate,
                                   # --prefetch-only --no-prefetch) exit 2 before any gate runs.
  lint-templates.py               # Gate 0: render every template (real + sim modes), validate YAML,
                                   # assert VERIFY-OK / VERIFY-FAIL / SIMULATE-OK / SIMULATE-FAIL
                                   # markers present, sh -n/bash -n every [shell, {,-c|-lc|...}, script]
                                   # entry in bootcmd+runcmd (catches YAML-fold-destroyed heredocs
                                   # and odd-quote entries that never execute), check simulate-vs-real
                                   # package parity, and re-render with hostile password shapes
                                   # (1234/yes/0000/true/quotes/colon) asserting every interpolated
                                   # password survives as a YAML str. 16 checks. ~3 sec. Pre-commit gate.
  run-tests.py                    # Gate 0d: unittest discover over tests/ (test_logic.py template
                                   # logic + test_fleet.py fleet control flow). Exit 2 when tests/
                                   # is missing or discovery finds 0 tests, so it can never pass
                                   # having checked nothing. Markers: TESTS-OK / TESTS-FAIL.
                                   # Also runs in CI, always the whole suite (~7 sec).
  lint-python.py                  # Gate 0c: pylint over the repo's .py files. Fleet scope = every
                                   # tracked *.py (whole tree, ~4 sec). CI calls it with
                                   # --changed-since <ref> on push (only what the push changed; it
                                   # falls back to the full tree when the ref won't resolve, so it
                                   # can never pass having checked nothing) and with no arguments on
                                   # a manual workflow_dispatch (explicit full lint). Markers:
                                   # PYLINT-OK / PYLINT-FAIL. Missing pylint = exit 2, never a pass.
  smoke-test-cli.py               # Gate 1: end-to-end orchestrator <-> setup_vm.py contract check.
                                    # Runs --prefetch + --simulate for ubuntu-lts.
                                   # Catches AttributeError / unknown-arg bugs. ~2-3 min warm.
                                    # Also runs pre-flight host-side DNS check against all
                                     # distro mirrors (DISTRO_MIRROR) and warns on failure.
  audit-packages.py               # Gate 0b: cross-distro package alignment audit (static analysis).
                                   # Prints category x distro table + REQUIRED verdict (exits 1 if a
                                   # required app category is missing from either distro's package
                                   # list). Run after any new package addition.
  check-virgl.sh / check-venus.sh # In-guest GL (virgl) / Vulkan (Venus) checks
tests/                            # unittest suite run by scripts/run-tests.py
templates/
  _base.j2                        # Common cloud-init scaffolding
                                    # Defines blocks: packages, bootcmd, pre/post_runcmd,
                                    #   simulate_install, verify, extra_write_files
                                    # Conditionalised on simulate_only flag for the simulate phase.
   _dconf_common.j2                # System-wide dconf: wallpaper, dark mode, single workspace,
                                     #   Epiphany webextensions, enabled-extensions (Gentoo:
                                     #   dash-to-dock + User Themes; Ubuntu: User Themes only --
                                     #   both gated by the install_dash_to_dock var set per
                                     #   distro), shell theme pin (shell_theme_name var).
                                     #   Included by both family templates.
   _gs_extensions_common.j2         # Curated GNOME Shell extension install (dash-to-dock) from
                                     #   a GitHub-release / host-served zip, gated on the SAME
                                     #   install_dash_to_dock var as _dconf_common so "installs"
                                     #   and "enables" cannot drift apart. ubuntu.j2 sets it
                                     #   false (keeps stock ubuntu-dock, which its session mode
                                     #   force-enables; two docks otherwise), gentoo.j2 true
                                     #   (no session-mode dock at all). Markers: GS-EXT-OK /
                                     #   GS-EXT-SKIPPED / GS-EXT-DONE.
  _plymouth_common.j2             # Shared Plymouth boot-splash setup (theme + grub/initrd regen).
                                     #   The video= arg goes to `plymouth_grub_file`: Ubuntu's
                                     #   /etc/default/grub.d/ drop-in (a Debian patch), Gentoo's
                                     #   /etc/default/grub itself (upstream grub-mkconfig reads
                                     #   nothing else; the tool is grub-mkconfig, not grub2-).
                                     #   Gentoo's regen steps only replace files that already exist
                                     #   and emit PLYMOUTH-GRUB-/PLYMOUTH-INITRD-OK|FAIL|SKIP.
                                     #   Included by family templates in post_runcmd; simulate VMs
                                     #   also include it (theme + initrd skipped, no plymouth) so
                                     #   simulate VMs get the video= grub args and render at full
                                     #   window size.
   _app_platforms_common.j2        # Shared vendor-app platform installs (Google Chrome, Flathub
                                     #   Flatseal/Gear Lever, PowerShell) + Epiphany default-browser
                                     #   wiring (mimeapps.list / xdg-settings). Best-effort.
                                     #   Included by both family templates in post_runcmd.
  _runcmd_common.j2               # runcmd boilerplate: graphical.target + GDM enable (skipped where
                                     #   the desktop meta enables GDM itself), qemu-guest-agent, PipeWire
                                     #   per-unit enable, /etc/localtime symlink. Included by _base.j2.
   _write_files_common.j2          # write_files boilerplate: /etc/issue login banner + check-vm
                                     #   diagnostic. Included by _base.j2.
  _macros.j2                      # Shared Jinja2 macros.
    ubuntu.j2                       # Standalone APT template (ubuntu.j2 inlined _apt_family.j2
                                     #   content).
    gentoo.j2                       # Gentoo template (binhost + gentoo-install.service).
    meta-data.j2                    # cloud-init meta-data
  network-config.j2               # Netplan YAML template for DHCP on e* interfaces
.agents/
  skills/
    update-mesa-fork/             # Project skill: rebuild mesa fork in guest VMs & run check-virgl/venus
```

## 🛡️ Pre-flight gates (run in this order before any real build)

The fleet orchestrator runs six cheap-to-expensive gates that catch
failures fast. Each gate's purpose is to surface a specific class of
bug in seconds-to-minutes instead of hours. Gates 0/0b/0c/0d are the
seconds-scale static checks; 1 through 3 grow from minutes to a quarter
of an hour.

| Gate | Trigger | Time | Catches | Hard/soft fail |
|---|---|---|---|---|
| **0. Lint** | Fleet auto (first gate), `--no-lint` to skip, or `python scripts/lint-templates.py` | ~3 sec | Jinja syntax, YAML structure, missing verify/simulate blocks, broken shell inside `runcmd`/`bootcmd`, simulate-vs-real package drift, passwords that don't survive YAML quoting (16 checks) | Hard (fleet aborts, exit code 5) |
| **0b. Audit** | Fleet auto (after lint), `--no-audit` to skip, or `python scripts/audit-packages.py` | ~1 sec | Required app category dropped from either distro's explicit package list (verdict in `REQUIRED`, 113 pairs) | Hard (fleet aborts, exit code 7) |
| **0c. Pylint** | Fleet auto (after audit), `--no-pylint` to skip, or `python scripts/lint-python.py` | ~7 sec | Python source errors in `linux_vm/`, `scripts/`, `tests/` (whole tree; `PYLINT-OK`/`PYLINT-FAIL`) | Hard (fleet aborts, exit code 8) |
| **0d. Tests** | Fleet auto (after pylint), `--no-tests` to skip, or `python scripts/run-tests.py` | ~8 sec | Template logic + fleet control flow (239 tests): leaked-VM cleanup, process-group teardown on timeout, cloud-init status → rc ladder, marker-grep rc handling, CLI flag validation, the verify-gate veto contract, and the gentoo wait-bound pair (`TESTS-OK`/`TESTS-FAIL`) | Hard (fleet aborts, exit code 9) |
| **1. Smoke** | Fleet auto (after tests), `--no-smoke` to skip, or `python scripts/smoke-test-cli.py` | 1.3 min (latest run) | Orchestrator ↔ setup_vm.py CLI contract mismatches, and a `--simulate`/`--prefetch` run that leaks a qemu-system process | Hard (fleet aborts, exit code 6) |
| **2. Prefetch** | Fleet auto, or `--prefetch-only` | 2.2 min (latest run) | Dead upstream URLs, network/firewall | Hard (fleet aborts) |
| **3. Simulate** | Fleet auto, or `--simulate-only` | 21.0 min total for both distros (ubuntu-lts 2.4 min, gentoo 18.6 min) | Package resolver failures (apt dry-run; includes vendor-repo Chrome) | Soft (table at end; iterate) |

**Standing rule:** After changing any of:
- A `templates/*.j2` file → run lint (the fleet now runs this itself as Gate 0, so a broken template aborts before any download)
- Any `.py` file under `linux_vm/`, `scripts/` or `tests/` → run `python scripts/lint-python.py` (Gate 0c; the fleet runs it too, so a pylint finding aborts a build)
- Any `.py` file, or anything asserted by `tests/` → run `python scripts/run-tests.py` (Gate 0d). It is the **only** coverage of `linux_vm/fleet/`, which is where a wrong verdict is invisible until hours into an unattended overnight run.
- `setup_vm.py` argparse / render-context / CLI flags → run smoke + lint
- `scripts/build-fleet-sequential.py` subprocess.run calls → run smoke
- Any package list in a template → run audit + simulate for that distro

Note: Gates 0 (lint), 0b (audit), 0c (pylint), 0d (tests), 1 (smoke), 2 (prefetch) and 3 (simulate) **all** execute inside the fleet runner by default — lint, then audit, then pylint, then tests, then smoke, then prefetch, then simulate. Each is skippable with its own flag (`--no-lint`, `--no-audit`, `--no-pylint`, `--no-tests`, `--no-smoke`, `--no-prefetch`, `--no-simulate`), and lint/audit/pylint/tests/smoke remain runnable standalone as pre-commit/CI steps. Conflicting pairs (`--simulate-only --no-simulate`, `--prefetch-only --no-prefetch`) are rejected with exit code 2 before any gate runs — `--simulate-only` used to be honoured only *inside* `if not skip_simulate:`, so that pair fell through into a full multi-hour real build, the opposite of what was asked for.

CI (`.github/workflows/pylint.yml`) runs `scripts/lint-python.py`: on `push` with `--changed-since <ref>` so it only lints the files that push changed (`before..HEAD`, one run covers a multi-commit push), and on a manual `workflow_dispatch` with **no arguments, i.e. a full-tree lint** — a manual run is an explicit request to validate everything. When a push's ref will not resolve (root commit, force-push, first-push all-zero sha) the script falls back to the full tree, so CI never reports success without having checked something. The fleet gate always lints the whole tree, because a long build runs every checked-in file, not just the last diff. CI additionally runs `scripts/run-tests.py` and `scripts/lint-templates.py` on **every** event: the tests have no `--changed-since` mode because they assert build-verdict behaviour (any subset can pass while the thing under test regressed), and Gate 0 needs no network or VM, so it costs ~3 s. Dependencies come from `requirements.txt`, installed into the project `.venv`. `linux_vm/host.py` only checks that they import and **never installs anything** (it used to `pip install --user`/`--break-system-packages` into whatever interpreter ran it), so a plain `setup_vm.py` run under an interpreter without them stops at the prerequisite step with exit 5 and a message naming the missing package.

## 🔒 Verify-block: the build-success contract

Every distro template's `runcmd` ends with a `{% block verify %}` that
checks the desktop core is actually installed:
- `gnome-shell`, `gdm`/`gdm3`, `gnome-control-center`, `nautilus`
- `gnome-software`
- Ubuntu: the mesa-fork marker (`/var/lib/mesa-fork-installed`); Gentoo: the
  `check-virgl` tooling (`x11-apps/mesa-progs` for glxinfo/glxgears +
  `/usr/local/bin/eglinfo` from the GLTOOLS section — the verify gate tests
  that **file**, not `command -v eglinfo`, because mesa-progs also ships a
  segfaulting `/usr/bin/eglinfo` that would satisfy a PATH-based check)

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
| `"$SVER"` — a grep pattern the shell must expand, matching a version across pretty-printed JSON | `\"\\\"shell-version\\\"[^]]*\\\"$SVER([.][0-9]+)?\\\"\"`, with the file piped through `tr -d '\\n'` first | **Two silent killers in one gate** (`_gs_extensions_common.j2`, found only by booting a built VM): (a) the pattern sat in **single quotes**, so bash never expanded `$SVER` — grep searched for a literal `$SVER`, where `$` is an end-of-line anchor, so it could never match *anything*; (b) `grep` is line-based while upstream **pretty-prints** `metadata.json` (the key on one line, each version on its own), so the version is never on the key's line. Symptom: `GS-EXT-SKIPPED: ... does not declare GNOME NN support` on **every** distro even for a release declaring 45–51 — while dconf still enabled the UUID, so Gentoo shipped with **no dock at all** and Ubuntu masked it with stock `ubuntu-dock`. Also avoid `\b`/`\d`: not portable ERE (`\d` is a literal `d` in GNU grep but a digit class elsewhere), so results differ per grep. Gate 0 cannot catch this class — the rendered script is perfectly valid shell — hence `tests/test_logic.py::TestDashToDockInstallGate` **executes** the rendered condition against a pretty-printed fixture instead of pattern-matching template text. |
| `\\` (literal backslash for shell) | `\\\\` in YAML double-quoted | YAML eats one, Jinja2 eats none, shell sees one |
| `{%- block` strips leading newline | Remove the `-` from `{%- block` | Jinja2's whitespace control (`{%-`) eats the newline before the child's content. If the preceding YAML entry is a literal block (`|`), the next entry merges into it and gets silently skipped by cloud-init. Use `{% block` (no dash) in YAML block contexts. |
| **Invalid top-level cloud-config keys → schema error aborts `bootcmd`** | Keep `user-data` to valid keys only (`hostname`, `bootcmd`, `runcmd`, `packages`, `write_files`, `ssh_authorized_keys`, `users`, `timezone`, `locale`, `output`, `manage_etc_hosts`, etc.) | `network:` and `datasource_list:` are **NOT** valid top-level cloud-config keys (network goes in the separate `network-config` doc); an invalid-shape `output:` (e.g. `output:` nested under `output:`) is also rejected. cloud-init 25.1 reports `schema errors: Additional properties are not allowed` and **aborts the entire bootcmd module**, so every `bootcmd:` entry silently fails to run. The NoCloud datasource + `network-config` are supplied by the seed ISO, so just drop these keys from `user-data`. Fix confirmed on Gentoo: removing them made bootcmd run. |
| **`bootcmd` entry silently skipped (multi-line `bash -c` scalar)** | Each statement on its own line; `echo` progress markers; never `#` mid-line after `;` | A `bootcmd:` entry written as a double-quoted YAML scalar that contains `# comment` right after `stmt;` (no newline) comments out the rest of the command; a child line indented **12 spaces instead of 10** becomes a YAML block-collection that breaks the entry. In both cases the entry produces no output and is effectively dropped (cloud-init still reports bootcmd "SUCCESS"). Symptom: a specific entry (e.g. the long Portage-sync or binhost-config step) never appears in `/var/log/cloud-init-bootcmd.log`. Fix: keep bootcmd scripts flat with `echo` markers; ensure every line inside the scalar is 10 spaces; no inline `#`. |
| **Heredoc (`<<'EOF'`) inside a `bootcmd` multi-line double-quoted scalar** | Write config files with `printf '...\n...'` (YAML-escape as `\\n`), never a heredoc; keep single-quote parity even in every entry | YAML flow-scalars **fold line breaks to spaces**, so `cat > file <<'DROPIN'`, the body, and the `DROPIN` terminator all collapse onto ONE physical line. Bash then warns `here-document at line 1 delimited by end-of-file (wanted 'DROPIN')` at every boot, `cat` treats the folded body as *file operands* and writes its `No such file or directory` errors INTO the target file (so the drop-in contains error text, not config), and everything after the opener — including a following `systemctl restart ...` — is consumed as cat arguments and never runs. Same class, worse outcome: an **odd number of `'`** in the entry (e.g. a stray `&'` after a background operator) is a hard `sh -c: line 1: unexpected EOF while looking for matching `''` error — `sh -n`/`bash -n` rc=2 and **the entire entry never executes**, masked because nothing else does that job. Diagnosis: render the template and `bash -n` each `bootcmd`/`runcmd` `-c` script — Gate 0 (`scripts/lint-templates.py`) now runs `sh -n` + `bash -n` on every `-c` entry and fails the gate, so this class is caught pre-commit. |
| `EMERGE_DEFAULT_OPTS` / `PYTHON_TARGETS` written to `make.conf` need quoting/targets | `EMERGE_DEFAULT_OPTS="--jobs=4 --load-average=8 --binpkg-respect-use=n"`; `PYTHON_TARGETS="python3_14"`; `PYTHON_SINGLE_TARGET="python3_14"` | Portage make.conf is shell-assigned: an unquoted value with a space (`EMERGE_DEFAULT_OPTS=--jobs=4 ...`) is a syntax error ("Invalid token '4'"); `binpkg-respect-use` is an *emerge flag*, not a make.conf variable (put it in `EMERGE_DEFAULT_OPTS`, not as its own line); the 23.0 `desktop/gnome/systemd` profile defaults to `python3_14` and the official arm64 binhost is built for that same target, so pin `PYTHON_TARGETS="python3_14"` + `PYTHON_SINGLE_TARGET="python3_14"` and every package (incl. `dev-python/pycairo`, `x11-base/xorg-drivers`) resolves as a binpkg. |
| **`bootcmd` is a single blocking stage — a hang in it blocks SSH + the whole boot** | Keep `bootcmd` to *fast, idempotent* prep only (network/DNS, locale gen, repo registration). Never put a slow/flaky step (Portage tree sync, large `emerge`) in `bootcmd`. | cloud-init runs `bootcmd` as one blocking unit; if a step inside it hangs (e.g. an unreliable `emaint sync` rsync), the bootcmd lock is held and `sshd` (started later in `runcmd`/`pre_runcmd`) never comes up, so the guest is unreachable and the build looks like a silent failure. Gentoo's fix: `bootcmd` does minimal prep, an `early-ssh.service` (`Before=cloud-init.target`) brings `sshd` up *before* cloud-init, and the real install runs in `gentoo-install.service` (written via `extra_write_files`, `After=sshd.service`), fully decoupled from cloud-init's lock. **Verify on every Gentoo change:** boot a real VM and `ssh` must answer within ~30s of QEMU start. |
| Place a fix in a child template **outside any block** → silently dropped in simulate mode | Put it inside an **always-rendered** `{% block %}` (added to `_base.j2`) | In Jinja inheritance, text in a child template that is not inside a block only appears if it lands inside an *always-rendered* block in the parent. `_base.j2` nests `{% block packages %}` inside `{% block package_management %}`, which in **simulate mode** renders only the `# SIMULATE` comment (no `packages:`). A Gentoo fix that put `network:`/`datasource_list:` right after `{% endblock %}` worked in real builds but was **dropped in simulate mode** — so the simulate gate kept hanging with no error. Fix: add a `{% block cloud_init_wiring %}{% endblock %}` to `_base.j2` (unconditionally, before `package_management`) and override it in the child. Verify both `simulate_only=True` and `=False` render the keys. |

**Always validate after editing templates:**

```python
import yaml
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
ctx = {'HOSTNAME':'h','USERNAME':'u','PASSWORD':'p','ROOT_PASSWORD':'r','TIMEZONE':'UTC',
       'SSH_PUBLIC_KEY':'ssh-ed25519 AAAA test',
       'INSTANCE_ID':'i','LAUNCH_DATE':'d'}
templates = {

    'ubuntu-lts': 'ubuntu.j2',
    'gentoo': 'gentoo.j2',
}
for distro, template in templates.items():
    yaml.safe_load(env.get_template(template).render(**ctx))
print('all 2 distros parse OK')
```

If this passes, the renders are syntactically valid. It doesn't catch logic bugs (wrong package names, bad repo URLs) — that's what the fleet test does.

## 📦 Per-distro package name reference

Same app, different package name per distro. See the "Package naming differences" table in [README.md](./README.md) for the full reference. Check that table before adding a new package to multiple templates.

## 🌍 Timezone + locale defaults

The guest timezone defaults to the **host's** timezone (`detect_timezone()`, falling back to `UTC` when it cannot be read; `--timezone` overrides), and the locale to `en_ZA.UTF-8`. They also generate `en_GB.UTF-8` and `en_US.UTF-8` so all three English variants are available. APT translations are disabled.

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
| Build one VM (the "real user" path, see README Quick Start) | `python setup_vm.py --distro ubuntu-lts` |
| Build one VM without booting (phase 1 only) | Default (omit `--start`) |
| Build one VM AND boot it (phase 1 + phase 2) | Add `--start` |
| Keep the intermediate qcow2 (debugging; not reused by later builds) | Add `--keep-qcow2` |
| Build all 2 distros sequentially, real-user simulation (build + wait for cloud-init each) | `python scripts/build-fleet-sequential.py` |
| Resume fleet from a specific distro | `python scripts/build-fleet-sequential.py --start-from gentoo --no-prefetch` |
| Build with target dir override | `--target-dir ~/VMs/my-custom-name` (default = `~/VMs/<distro>/`) |
| Run Venus on KosmicKrisp instead of MoltenVK (macOS 26+; qemu-virgl installs `milesbuckton/qemu-virgl/kosmickrisp` there) | Add `--host-vulkan-driver kosmickrisp` (also accepted by the fleet script) |
| Run a multi-hour fleet build (anything that can exceed the shell tool's per-invocation timeout) | `nohup .venv/bin/python3 scripts/build-fleet-sequential.py > full-build-run.log 2>&1 & disown` — detaches so a shell-tool timeout cannot kill the fleet process or orphan a QEMU VM. (Alternatively a background shell call whose timeout exceeds the whole run: the tool **stops** a background command when its timeout expires, which is what killed a fleet 1 h in.) Only one fleet can run at a time — a second exits 10 on `~/VMs/build-fleet.lock` — and `>` truncates the log, so check `pgrep -f build-fleet-sequential` before relaunching. |

## 📝 Git / commit convention

- **No PRs. Ever.** Commit directly to `main`. Do not create feature branches or pull requests.


## 🛠️ Common change patterns

### Adding a new app to all distros

1. Look up per-distro package name in the "Package naming differences" table in [README.md](./README.md) (verify via distro package indexes where needed).
2. Add to each template's appropriate insertion point:
   - apt/portage: `packages:` list (in `{% block packages %}`) OR post_runcmd best-effort line
3. If the app needs a new vendor repo, register in `bootcmd` first.
4. Run the validation Python snippet above.
5. Update README's `## Apps preinstalled on every VM` table.
6. `git add -A && git commit && git push origin main`.

### Adding a new distro

**The matrix is 2 distros: ubuntu-lts, gentoo**. Only extend it with a strong reason — each new distro multiplies the fleet test matrix and the per-distro template surface. The steps to add one are:

1. Add a resolver in `linux_vm/download.py` (`_resolve_<distro>()`) — see existing patterns; pick from the distro's official cloud image directory.
2. Add to `DISTRO_REGISTRY` dict in `linux_vm/download.py`.
3. Add to `DISTRO_DEFAULTS` in `linux_vm/config.py`.
4. Create `templates/<distro>.j2` extending the closest family or another concrete template; include the `{% block verify %}` AND `{% block simulate_install %}` + `{% block extra_write_files %}` (conditional on `simulate_only`).
5. Add to README's supported-distros table.
6. Add to `DISTRO_ORDER` in `linux_vm/config.py` (insert in easiest→hardest position; the fleet builder + lint + audit scripts all derive their distro list from it — `scripts/build-fleet-sequential.py` is a thin shim).
7. Add to `DISTRO_TEMPLATE` in `linux_vm/config.py` (the single source of truth; `download.DISTRO_REGISTRY[].user_data_template`, the lint script, and the audit script all derive from it).
8. Run `scripts/lint-templates.py` + `scripts/lint-python.py` + `scripts/smoke-test-cli.py` BEFORE committing.

## 🧾 Review-hardening invariants (don't undo these without re-reading the tests)

**Never use or install stock Homebrew `qemu` — only qemu-virgl.** Stock `qemu` has no virglrenderer, and the old `brew install qemu` fallback (just to get `qemu-img`) pulled it in on clean hosts. `linux_vm/host.py::find_qemu_img` only accepts qemu-virgl's `qemu-img` (opt prefix, next to the detected virgl binary, or a `PATH` entry resolving into `Cellar/qemu-virgl`); a missing one exits 4 with `QEMU_VIRGL_INSTALL_HINT`, and a non-virgl `qemu-system-aarch64` exits 3. Don't add install fallbacks or hints that point at stock `qemu`. Locked by `tests/test_logic.py::TestFindQemuImgNeverStock` and `TestMissingQemuImgExits4`.

**`grep -F MARKER file1 file2` exits 2 on a MATCH when either file is missing — so `_check_success_marker` must not gate on `rc == 0`.** Only `templates/gentoo.j2:1199` writes `/var/log/verify-marker.log`; `ubuntu.j2`'s verify block (via `templates/_macros.j2`) only echoes to stdout. Ubuntu's grep therefore prints the match to stdout, errors on the absent second file, and exits **2** — an `rc == 0` gate would fail every Ubuntu build despite VERIFY-OK being present. The check is now `marker in (stdout or "")` (stdout holds only matched lines now that the old `2>&1`/`|| echo NO_MARKER` tail is gone) with `rc not in (0, 1, 2)` used only to note that grep may never have run. Locked in by `tests/test_fleet.py::TestCheckSuccessMarker::test_grep_rc2_with_marker_still_passes`.

**A `subprocess` timeout kill does not reach a `start_new_session=True` grandchild.** `setup_vm.py` launches QEMU via `Popen(..., start_new_session=True)`, so when the fleet's `run_to_file` timeout fired it killed only the python parent and the VM kept running — invisible, because `build_and_provision` only set `vm_started = True` *after* `run_to_file` returned, so the `finally` never shut it down. Fix on both sides: `fleet/executor.py` now runs the child with `Popen` + `start_new_session=True` and `_kill_process_tree()` (getpgid → `os.killpg(SIGKILL)`), and `fleet/orchestrator.py` has `_qemu_running_for(target)` which flips `vm_started` on in the `finally` so an orphan is cleaned up regardless of which failure path produced it. An orphan burns host RAM/CPU for the rest of a multi-hour run — exactly the starvation condition that soft-locks guests.

**`--keep-qcow2` does NOT mean "reuse the disk I built last time", and an existing `disk.qcow2` is never silently replaced.** Without `--force`, `orchestrate._build_one_vm` refuses an existing disk up front (exit 8, before any resolve/download/extract) and `linux_vm/qemu.py::prepare_disk` enforces the same rule; with `--force` it refuses while QEMU is running for that target dir, converts + resizes into `disk.qcow2.building`, and only `os.replace()`s the real disk once both steps succeed. The fleet always passes `--force` (its preflight kills leftover QEMU first), so every fleet build still starts from a pristine disk; `--keep-qcow2` only preserves the *extracted intermediate* image from the archive. That is why `shutdown_and_verify` now waits up to `SHUTDOWN_TIMEOUT_SEC` for a clean self-shutdown before SIGKILL — an unclean kill costs a re-extract, not a corrupted build.

**`audit-packages.py`'s gentoo set must be extracted from BOTH renders.** The simulate render's package list (`/etc/install-simulate-pkgs.txt`) does not include packages the real `gentoo-install.sh` emerges (binhost/tools/vendor steps), so the audit read only the simulate set and could report a category present that the real build never installs. It now merges `gentoo_atoms_from_text()` over the real install script's `emerge` lines and its hand-maintained `PKGS=` list, with a path-prefix blocklist applied to both (the `PKGS=` branch previously had none, so `tmp/foo`-shaped entries leaked into the audit set).

**A sentinel must be a value the user CANNOT type — a `Path` value collides with the very thing it guards.** `orchestrate.main()` used to call `parse_args(default_target_dir(host, "ubuntu-lts"))` and then `if args.target_dir.resolve() == placeholder: args.target_dir = default_target_dir(host, args.distro)`. Two failures: (1) **value collision** — `--target-dir ~/VMs/ubuntu-lts --distro gentoo` is indistinguishable from the sentinel, so the flag was discarded and the VM was built in `~/VMs/gentoo` (README documents `--target-dir` as a supported flag); (2) **identity collision** — the sentinel is compared `.resolve()`d on one side but is a bare `Path.home()` on the other, so a `$HOME` reached through a symlink never compares equal and then *no* distro gets its own default. `--target-dir` now defaults to `None` and only `None` derives from the distro. Locked by `tests/test_logic.py::TestTargetDirIsNeverSilentlyRewritten`.

**Every exit from a wait loop must ask the same question every other exit asks.** `_ssh_wait_cloud_init` consulted `_check_success_marker` on all four status-ladder exits and on the >900 s short-circuit, but the `while ...: break` on the overall deadline returned -1 with no marker check at all. The marker is written by the LAST cloud-init stage, so it can land in the seconds between the final probe and the cap expiring — a completed build reported TIMEOUT, its VM shut down, its disk discarded. That is precisely the loss the gentoo wait-bound pair exists to prevent: the cap can fire first while the guest has already finished. Locked by `tests/test_fleet.py::TestCloudInitDeadlineStillChecksMarker`.

**`proc.kill()` signals only the direct child — and on a stock macOS host the process holding the TCP connection to the guest is a GRANDchild.** `executor.run_to_file` already used `start_new_session=True` + `_kill_process_tree` (getpgid → killpg), but its sibling `run_with_hard_timeout` used a bare `Popen` + `proc.kill()` while `ssh.py`'s own docstring claimed "the whole tree is reaped on timeout". The fleet's SSH probes run through `ssh_cmd()`, which on a ControlMaster/jump-host setup forks a ProxyCommand helper — so a timeout left that grandchild alive holding the guest's SSH port and the disk busy. Both now share `start_new_session` + `_kill_process_tree`. Note the two behaviours (ordinary exception → `-98` tuple; `KeyboardInterrupt`/`SystemExit` → kill then re-raise) **cannot be two sequential `except` clauses** — `BaseException` is an ancestor of `Exception`, so the narrower one is unreachable and pylint E0701 says so. Locked by `tests/test_fleet.py::TestRunWithHardTimeoutKillsTree`.

**A path interpolated into `pgrep -f` is a REGEX, and `list_running_qemu_pids` raises on anything but rc 0/1.** `provider.list_running_qemu_pids` builds `f"qemu-system.*{scope}"` from a filesystem path. `v1.0` silently over-matches (`.` is any char, so an unrelated VM can match and a real one be missed); a directory with `(` makes pgrep print `Cannot compile regular expression ... (parentheses not balanced)` and exit **2**, and the function's `check_returncode()` then raises `CalledProcessError`. That exception is unhandled in `lifecycle.preflight_cleanup`, `shutdown_and_verify`'s polling, and `orchestrator._discover_ssh_port` / `build_and_provision`'s `finally` — so one oddly-named directory takes out the VM cleanup path. Now `re.escape(scope)`. Locked by `tests/test_fleet.py::TestPgrepScopeIsEscaped` (which also runs real `pgrep`, because a mock cannot detect "the pattern does not compile").

**Only one fleet run at a time — a second one's preflight kills the first one's VMs.** `preflight_cleanup` force-kills every qemu-system under `~/VMs` with no idea who owns it, so a fleet started while another was ~2 h into Gentoo cloud-init killed that build, and its `>` redirect wiped the old run's log, leaving no failure line to find. `fleet.main` now takes `lifecycle.acquire_fleet_lock()` (an `flock` on `~/VMs/build-fleet.lock`, holder PID written inside) right after `ensure_out_root()`, in every mode and before any gate, and exits **10** naming the holder when it is taken. `flock`, not a PID file: the kernel releases it when the holder dies by any route, SIGKILL included, so there is never a stale lock; the fd is non-inheritable, so an orphaned QEMU cannot keep it held. Tests that drive `main()` past `ensure_out_root` must patch `acquire_fleet_lock` — Gate 0d runs *inside* a locked fleet, so an unpatched test would hit the real lock and fail. Locked by `tests/test_fleet.py::TestFleetLock`.

**`PermissionError` IS an `OSError`, and a fault `_atomic_replace_with_retry` already gave up on cannot be fixed by retrying it again.** `download()` catches `OSError` around the fetch+write, so the `PermissionError` that `_atomic_replace_with_retry` spent 8 attempts on fell into the same handler as a network blip — an unwritable `~/VMs/cache` burned the full 1800 s `DOWNLOAD_BUDGET_SEC` in a 15 s sleep loop and then reported a **network** failure, sending the operator to inspect the mirror instead of `ls -ld ~/VMs/cache`. Same reasoning the handler already applies to permanent HTTP statuses. Locked by `tests/test_logic.py::TestDownloadFailsFastOnPermissionError` (whose second case asserts the transient-`URLError` retry path still works, so the guard cannot silently narrow the retry set).

**"Single source of truth" is a claim about a call graph, not a docstring — `linux_vm/test_context.py` said so while `orchestrate._build_one_vm` built its own dict literal.** The two had already drifted: the runtime context was missing `display_name`, which `_base.j2` interpolates into the guest's `final_message`, so **Gate 0 was rendering a message the build never ships**. The reverse drift is the dangerous direction: the gate-only context carried `VM_NAME`, `DISPLAY_NAME`, `PASSWORD_HASH`, `ROOT_PASSWORD_HASH`, `DISTRO_ID` — keys **no template has ever read**. Had the runtime adopted that context wholesale, a future `{{ PASSWORD_HASH }}` would have shipped the literal string `$6$dummy$dummy` into the guest's `chpasswd` with Gate 0 green throughout. `_build_one_vm` now calls `test_context.render_context()` and layers the runtime-only values on top; the dead keys are gone and `display_name` comes from a `DISPLAY_NAMES` map, so lint and runtime render the same final_message. Locked by `tests/test_logic.py::TestRenderContextIsSingleSource`, whose template scan skips names a template binds itself (`{% set %}`, `{% for %}`, `{% from ... import %}`) and expressions guarded with `| default(...)` — those are safe under `StrictUndefined` and are not missing keys. It scans every `templates/*.j2` rather than the two entry points' chains because Jinja's `included_templates` tracks `{% include %}` but **not** `{% extends %}`/`{% from %}` — `_base.j2` and `_macros.j2` are invisible to it.

## 🚧 Constraints (project scope: 2 distros)

| | |
|---|---|
| **Distros** | The 2 distros in the matrix: ubuntu-lts, gentoo. |
| **Init** | systemd-only. Templates assume `systemctl enable/start`. Non-systemd distros (Alpine OpenRC, Void runit, Devuan sysvinit) are out of scope. |
| **DE** | GNOME only. Other DEs (KDE/XFCE/Cinnamon) not planned. |
| **Wayland** | Default session. X11 fallback exists in some templates but Wayland is the supported configuration. |
| **Atomic / image-based** | Out of scope (Silverblue, Bazzite, etc.). Standard package-manager install only. |
| **Architecture** | aarch64 is the validated and only supported path (Apple Silicon MacBook Pro). |

## 🗣️ Working with the user (Miles Buckton)

- He values **honest assessments** over enthusiastic agreement. Flag risks and gotchas proactively.
- He's iterating, so expect frequent additions/changes to the curated app set.
- He runs **multi-hour overnight builds** to validate fleet-wide changes.
- He likes **structured choice menus** for design decisions (the `ask_user` tool with numbered options + recommended).
- **Latest version only** — never preserve back-compat for older majors.
- He runs on **macOS** (Apple Silicon MacBook Pro 18 cores, QEMU/HVF). Keep the host otherwise idle during fleet builds — a VM using all the host's cores while the app + agent polling run can soft-lock the guest.
- He prefers **explicit over implicit** — even if a package is pulled in transitively, add it to the install list for clarity.
- He expects **autonomous iteration on simulate failures** — don't ask for input on every package fix; iterate the simulate-fix-rerun loop and only escalate when the fix would change user-facing behaviour or >3 unrelated failures suggest a systemic issue.
- He expects **comprehensive forensics on real-build failures** — every failure should produce enough diagnostic dump that the assistant can root-cause without operator input. New `UNKNOWN`-category patterns should be proactively added to the categoriser.
- **No dates in docs** — never write absolute dates (any `YYYY-MM-DD` form) into `README.md`, `AGENTS.md`, or `docs/*.md`, including in timing passages. Reference measurements relatively instead: *latest run*, *previous run*, *earlier run*, *fastest measured*.
- **Real fleet builds have passed** — the fleet completed the full matrix with `VERIFY-OK`. Latest run: simulate 2/2 PASS in 10.6 min — ubuntu-lts 2.3 min, gentoo 8.3 min (standalone `--simulate-only` pre-pass, `ALL PASSED in 0.25h`); full build 2/2 PASS in 3.98h — ubuntu-lts 49.4 min total / 48.0 min cloud-init, gentoo 188.6 min total / 185.1 min cloud-init, both VERIFY-OK, 0 errors, 0 recoverable errors, clean shutdowns (`--no-simulate` run, `DONE in 3.98h`). See ARCHITECTURE.md for the per-phase timings.

## 🔄 Project skills

- **`update-mesa-fork`** (`.agents/skills/update-mesa-fork/SKILL.md`): Sequentially boots each VM (Ubuntu LTS then Gentoo), pulls the latest `main` of the miles.buckton Mesa fork inside `/var/cache/mesa-fork`, rebuilds and reinstalls it, and runs `scripts/check-virgl.sh` and `scripts/check-venus.sh` on the host. Use whenever refreshing Mesa on existing VMs after a fork push.

## 📚 Where to look next

- **User-facing docs**: [README.md](./README.md)
- **Architecture, timing, fleet building**: [ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- **Day-2 ops** (snapshots, SSH, etc.): README's "Day-2 operations" section
