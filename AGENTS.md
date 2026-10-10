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
  download.py                     # SSL, _urlopen, download, hash/verify, distro resolvers, DISTROS dict
  templates.py                    # render_jinja2_template, build_seed_iso
  provider.py                     # Running-VM discovery: list_running_qemu_pids, find_running_ssh_port
  monitor.py                      # monitor_main, _monitor_tail_loop, SSH helpers
  orchestrate.py                  # parse_args, main(), _build_one_vm()
  fleet/
    __init__.py                   # Fleet orchestrator shim (re-exports linux_vm.fleet.*)
    constants.py                  # Fleet-wide constants: timeouts, DISTRO_MIRROR, USERNAMES,
                                   #   DISTROS order
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
                                     #   Included by all 3 family/extender templates.
   _gs_extensions_common.j2         # Curated GNOME Shell extension install (dash-to-dock) from
                                     #   a GitHub-release / host-served zip, gated on the SAME
                                     #   install_dash_to_dock var as _dconf_common so "installs"
                                     #   and "enables" cannot drift apart. ubuntu.j2 sets it
                                     #   false (keeps stock ubuntu-dock, which its session mode
                                     #   force-enables; two docks otherwise), gentoo.j2 true
                                     #   (no session-mode dock at all). Markers: GS-EXT-OK /
                                     #   GS-EXT-SKIPPED / GS-EXT-DONE.
  _plymouth_common.j2             # Shared Plymouth boot-splash setup (theme + grub/initrd regen).
                                     #   Included by family templates in post_runcmd; simulate VMs
                                     #   also include it (theme + initrd skipped, no plymouth) so
                                     #   simulate VMs get the video= grub args and render at full
                                     #   window size.
   _app_platforms_common.j2        # Shared vendor-app platform installs (Google Chrome, Flathub
                                     #   Flatseal/Gear Lever, PowerShell) + Epiphany default-browser
                                     #   wiring (mimeapps.list / xdg-settings). Best-effort.
                                     #   Included by all 3 family/extender templates in post_runcmd.
  _runcmd_common.j2               # runcmd boilerplate: graphical.target + GDM enable (skipped where
                                     #   the desktop meta enables GDM itself). Included by _base.j2.
   _write_files_common.j2          # write_files boilerplate: /etc/issue login banner. Included by _base.j2.
    ubuntu.j2                       # Standalone APT template (ubuntu.j2 inlined _apt_family.j2
                                     #   content).
    meta-data.j2                    # cloud-init meta-data
  network-config.j2               # Netplan YAML template for DHCP on e* interfaces
.opencode/
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
| **0b. Audit** | Fleet auto (after lint), `--no-audit` to skip, or `python scripts/audit-packages.py` | ~10 sec | Required app category dropped from either distro's explicit package list (verdict in `REQUIRED`, 101 pairs) | Hard (fleet aborts, exit code 7) |
| **0c. Pylint** | Fleet auto (after audit), `--no-pylint` to skip, or `python scripts/lint-python.py` | ~4 sec | Python source errors in `linux_vm/`, `scripts/`, `tests/` (whole tree; `PYLINT-OK`/`PYLINT-FAIL`) | Hard (fleet aborts, exit code 8) |
| **0d. Tests** | Fleet auto (after pylint), `--no-tests` to skip, or `python scripts/run-tests.py` | ~7 sec | Template logic + fleet control flow (98 tests): leaked-VM cleanup, process-group teardown on timeout, cloud-init status → rc ladder, marker-grep rc handling, CLI flag validation (`TESTS-OK`/`TESTS-FAIL`) | Hard (fleet aborts, exit code 9) |
| **1. Smoke** | Fleet auto (after tests), `--no-smoke` to skip, or `python scripts/smoke-test-cli.py` | ~2-3 min | Orchestrator ↔ setup_vm.py CLI contract mismatches, and a `--simulate`/`--prefetch` run that leaks a qemu-system process | Hard (fleet aborts, exit code 6) |
| **2. Prefetch** | Fleet auto, or `--prefetch-only` | ~10-15 min cold, ~1-2 min warm | Dead upstream URLs, network/firewall | Hard (fleet aborts) |
| **3. Simulate** | Fleet auto, or `--simulate-only` | ~10-20 min warm for all 2 distros (ubuntu-lts ~1.7-1.8 min, gentoo ~8-18 min) | Package resolver failures (apt dry-run; includes vendor-repo Chrome) | Soft (table at end; iterate) |

**Standing rule:** After changing any of:
- A `templates/*.j2` file → run lint (the fleet now runs this itself as Gate 0, so a broken template aborts before any download)
- Any `.py` file under `linux_vm/`, `scripts/` or `tests/` → run `python scripts/lint-python.py` (Gate 0c; the fleet runs it too, so a pylint finding aborts a build)
- Any `.py` file, or anything asserted by `tests/` → run `python scripts/run-tests.py` (Gate 0d). It is the **only** coverage of `linux_vm/fleet/`, which is where a wrong verdict is invisible until hours into an unattended overnight run.
- `setup_vm.py` argparse / render-context / CLI flags → run smoke + lint
- `scripts/build-fleet-sequential.py` subprocess.run calls → run smoke
- Any package list in a template → run audit + simulate for that distro

Note: Gates 0 (lint), 0b (audit), 0c (pylint), 0d (tests), 1 (smoke), 2 (prefetch) and 3 (simulate) **all** execute inside the fleet runner by default — lint, then audit, then pylint, then tests, then smoke, then prefetch, then simulate. Each is skippable with its own flag (`--no-lint`, `--no-audit`, `--no-pylint`, `--no-tests`, `--no-smoke`, `--no-prefetch`, `--no-simulate`), and lint/audit/pylint/tests/smoke remain runnable standalone as pre-commit/CI steps. Conflicting pairs (`--simulate-only --no-simulate`, `--prefetch-only --no-prefetch`) are rejected with exit code 2 before any gate runs — `--simulate-only` used to be honoured only *inside* `if not skip_simulate:`, so that pair fell through into a full multi-hour real build, the opposite of what was asked for.

CI (`.github/workflows/pylint.yml`) runs `scripts/lint-python.py`: on `push` with `--changed-since <ref>` so it only lints the files that push changed (`before..HEAD`, one run covers a multi-commit push), and on a manual `workflow_dispatch` with **no arguments, i.e. a full-tree lint** — a manual run is an explicit request to validate everything. When a push's ref will not resolve (root commit, force-push, first-push all-zero sha) the script falls back to the full tree, so CI never reports success without having checked something. The fleet gate always lints the whole tree, because a long build runs every checked-in file, not just the last diff. CI additionally runs `scripts/run-tests.py` and `scripts/lint-templates.py` on **every** event: the tests have no `--changed-since` mode because they assert build-verdict behaviour (any subset can pass while the thing under test regressed), and Gate 0 needs no network or VM, so it costs ~3 s. Dependencies come from `requirements.txt` (also bootstrapped at runtime by `linux_vm/host.py`, so a missing one is not fatal for a plain `setup_vm.py` run).

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
}
for distro, template in templates.items():
    yaml.safe_load(env.get_template(template).render(**ctx))
print('all 2 distros parse OK')
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
| Build one VM (the "real user" path, see README Quick Start) | `python setup_vm.py --distro ubuntu-lts` |
| Build one VM without booting (phase 1 only) | Default (omit `--start`) |
| Build one VM AND boot it (phase 1 + phase 2) | Add `--start` |
| Re-build (skip re-download) | Add `--keep-qcow2` |
| Build all 2 distros sequentially, real-user simulation (build + wait for cloud-init each) | `python scripts/build-fleet-sequential.py` |
| Resume fleet from a specific distro | `python scripts/build-fleet-sequential.py --start-from gentoo --no-prefetch` |
| Build with target dir override | `--target-dir ~/VMs/my-custom-name` (default = `~/VMs/<distro>/`) |

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
2. Add to `DISTROS` dict in `linux_vm/download.py`.
3. Add to `DISTRO_DEFAULTS` in `linux_vm/config.py`.
4. Create `templates/<distro>.j2` extending the closest family or another concrete template; include the `{% block verify %}` AND `{% block simulate_install %}` + `{% block extra_write_files %}` (conditional on `simulate_only`).
5. Add to README's supported-distros table.
6. Add to `DISTRO_ORDER` in `linux_vm/config.py` (insert in easiest→hardest position; the fleet builder + lint + audit scripts all derive their distro list from it — `scripts/build-fleet-sequential.py` is a thin shim).
7. Add to `DISTRO_TEMPLATE` in `linux_vm/config.py` (the single source of truth; `download.DISTROS[].user_data_template`, the lint script, and the audit script all derive from it).
8. Run `scripts/lint-templates.py` + `scripts/lint-python.py` + `scripts/smoke-test-cli.py` BEFORE committing.

## 🩹 Known battle scars

**Host-level gotchas that bite during fleet builds:**
- **Host CPU starvation**: on ≤4-physical-core Macs, a full-core VM + the OpenChamber app + agent polling saturates the host (load 8 on 4 cores) → guest kernel soft lockups (`stuck for 34s! [swapper/*]`) → cloud-init stalls → 60-min wait timeouts. It also collapses the orchestrator's 5-min SSH probe cadence (single wait-log lines, 40+ min gaps). Default VM is **half the host's physical cores (clamped 2-8) / half the host's RAM (clamped 8-32 GB) / 80 GB disk**; run fleet builds with the machine otherwise idle.
- **Firefox is explicit on both distros**: Ubuntu template lists `firefox` (the apt snap-shim package; `ubuntu-desktop-minimal` also **Recommends: firefox** so the chain was always there — the template just makes it explicit) and Gentoo lists `www-client/firefox`. On Ubuntu, `snapd`/`snap-store`/`bingwall` are installed via apt + post_runcmd. **Gentoo now installs them too** (`app-containers/snapd` + `sys-fs/squashfuse` in `_gentoo_pkgs`, snaps in post_runcmd) — they used to be omitted on a fear of "hours of arm64 source building", but that was overstated: `dev-lang/go` ships as an arm64 binpkg, so snapd is a single Go compile, not a toolchain bootstrap. **The real trap:** neither package carries an arm64 keyword at all (`snapd KEYWORDS="amd64"`, `squashfuse KEYWORDS="~amd64 ~riscv ~x86"`), so Portage masks them as *missing keyword* — and a plain `~arm64` entry in `package.accept_keywords` does **not** unmask those, only `**` does (KEYWORDS ignored entirely; Portage itself suggests `**`). Both are `**`-unmasked in the real install *and* the simulate section (keep them in sync or the dry-run validates against different keywords than the build), and both are asserted in the verify block since snapd is a strict build requirement. `sys-fs/fuse` (squashfuse's dep) *is* arm64-keyworded, so it resolves normally. **Epiphany is the default browser** (system-wide `/etc/xdg/mimeapps.list` + `xdg-settings` in `_app_platforms_common.j2`; marker `BROWSER-DEFAULT-OK`) — Firefox and Chrome stay installed but are not default.
- **macOS DNS flap**: `getent hosts` can fail for everything while Python `socket.gethostbyname` (what the fleet uses) works fine. Probe with `python -c "import socket; socket.gethostbyname('<mirror>')"` and wait the flap out before retrying.
- **The host has TWO pythons and the wrong one kills Gate 0 in 1 second** — `/opt/homebrew/bin/python3` (Homebrew 3.14.8) has **no pyyaml**, while `/usr/bin/python3` (3.9) has jinja2/yaml/pylint in the 3.9 user site-packages; there is no `python` on this host at all. An agent-launched or login-shell fleet run that puts `/opt/homebrew/bin` first on `PATH` dies immediately with `! ERROR: missing dependency: No module named 'yaml'` + `!!! HARD-FAIL: lint gate (rc=2) -- aborting fleet before any build` — which reads like a template bug but is purely an interpreter-shadowing one. `linux_vm/host.py` bootstraps `requirements.txt` for a plain `setup_vm.py` run, so nothing warns you. Fix: launch with the **explicit interpreter** (`exec nohup /usr/bin/python3 scripts/build-fleet-sequential.py > ~/fleet-run.log 2>&1 &`) and never `python3` off `PATH` for the fleet; all internal subprocess calls use `sys.executable`, so the one explicit choice propagates safely through every gate.
- **Only one VM at a time**: The fleet orchestrator runs sequentially by design. Manual `setup_vm.py` runs and fleet builds share the same `~/VMs/` cache and SSH host ports (2222–2322). If two builds race, they can corrupt a qcow2 image because the backing store gets rebuilt mid-run. Always kill existing QEMU processes (`pkill -f qemu-system`) before starting a new build, or use the fleet orchestrator which handles cleanup automatically.
- **Background shells don't survive session boundaries — launch multi-hour fleet builds outside the agent's background-shell mechanism**: the orchestrator has been killed mid-wait **twice** (overnight, and at a morning session boundary) when the agent harness tore down the background shell that launched it — the shell's `.out` file disappears, no completion notification ever arrives, and the fleet python dies with it, while the guest keeps provisioning and usually finishes fine on its own (the cold-cache all-2 run's gentoo reached `VERIFY-OK` ~10 min *after* the fleet died; only the phase-3 shutdown, the VM `TOTAL` line, and the final summary were lost, and the VM had to be shut down manually). Signature: a wait-log gap of >20 min where probes normally land every ~5 min, QEMU still running, SSH answering instantly, and `pgrep -f build-fleet` empty. Launch long runs from a plain terminal instead of an agent background shell. Recovery without a fleet: check `cloud-init status --long` + `VERIFY-OK` in the guest directly, snapshot `status.json` into `<target_dir>/`, and shut the VM down manually. **Verified detached-launch recipe** (used for the 5.16 h all-2 run; fleet reparented to launchd, PPID 1, so the agent harness tearing down the session cannot kill it): `osascript -e 'do shell script "cd <repo> && export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin && exec nohup /usr/bin/python3 scripts/build-fleet-sequential.py > /Users/milesbuckton/fleet-run.log 2>&1 &"'` then monitor with `tail -f /Users/milesbuckton/fleet-run.log` — note the explicit `/usr/bin/python3` (see the python-shadowing scar above).
- **`degraded done` root cause is a single benign `lock_passwd` warning — FIXED by `plain_text_passwd` on the user entry**: `cloud-init status --long` on a fully successful Ubuntu build used to show `extended_status: degraded running` → `degraded done` (rc=2), which looked alarming but was success (`VERIFY-OK` present, password usable). Chain: `_base.j2` set `lock_passwd: false` on the user but supplied the password via the separate `chpasswd:` block instead of `passwd`/`plain_text_passwd`/`hashed_passwd` **on the user entry** — so `cloudinit/distros/__init__.py` logged `lifecycle.py[WARNING]: Not unlocking password for user <name>...` while creating the user. `cloudinit/log/loggers.py` attaches a `LogExporter` handler at `setLevel(logging.WARN)`, and `cmd/status.py` sets `condition_status = DEGRADED` whenever any stage's `recoverable_errors` is non-empty. That warning was the **only** WARNING/ERROR record in `/var/log/cloud-init.log`, so it alone flipped the whole status to degraded. It was spurious: `cc_set_passwords` runs ~0.3 s later and sets the hash (`passwd -S <user>` → `P`). **Fix applied**: `cloudinit/distros/__init__.py` `create_user()` checks `lock_passwd:false` against `passwd`/`plain_text_passwd`/`hashed_passwd` **on the user entry** (that is the whole bug — the top-level `chpasswd:` block is invisible to it), so `_base.j2` now sets `plain_text_passwd: '{{ PASSWORD | replace("'", "''") }}'` on the entry while `chpasswd:` stays the password module. Verified: a post-fix simulate run's `cloud-init-status.json` reports `recoverable_errors: {}` in every stage. Keep the mechanism in mind when triaging *other* WARN sources. **Do not chase the `needrestart.conf` / `update-alternatives ... spinner.plymouth` lines** — they print to `cloud-init-output.log` but never enter `recoverable_errors`, so they don't cause `degraded`.
- **`eglinfo` segfaults on Gentoo (exit 139) and check-virgl still reports VIRGL-OK — guard the FILE, not `command -v`**: `x11-apps/mesa-progs-9.0.0` now ships its own `/usr/bin/eglinfo`, which **segfaults** in default multi-platform mode, mid-way through the EGL config table (197 lines then a SIGSEGV; `-p gbm`/`-p surfaceless` complete fine, the default platform walk does not). It is **not** a QEMU or mesa-fork regression — the identical coredump signature is in Gentoo's persistent journal on every pre-upgrade boot (`journalctl -b -1 -p err | grep eglinfo`), while Ubuntu's packaged eglinfo never coredumps. Two things hid it: (1) the GLTOOLS guard was `if ! command -v eglinfo` and mesa-progs is emerged immediately before it, so the guard was always satisfied and the mesa-demos build (`/usr/local/bin/eglinfo`, which runs to completion — 1168 lines, no crash) **never fired**; (2) check-virgl pipes `eglinfo ... | grep`, and the `virgl` / `virtio_gpu` lines print *before* the crash, so the check consumed real data and passed anyway. **Fix**: GLTOOLS now guards on `[ ! -x /usr/local/bin/eglinfo ]` and the verify block tests that same file rather than PATH. Diagnostic pattern: when a guest tool misbehaves *silently under a passing check*, diff the coredump count before/after the check run (`journalctl -p err | grep -c '(eglinfo) of user'`) — 15→15 proves the check itself is clean. Build a suspect binary beside the packaged one and compare exit codes under identical conditions before assuming the driver stack is at fault.
- **`check-virgl.sh` gate 2 never ran on a Wayland session yet still printed `VIRGL-OK: full VirGL stack verified` — a skipped gate must never claim verification**: gate 2's render test reached only for `glxgears`, which is X11-only, so on every Wayland boot (both distros) it hit the "no X11 DISPLAY, skipping" branch and then the script printed an unconditional success line. Every VirGL pass to date proved **renderer identity** (`renderer: virgl`, `EGL driver: virtio_gpu`) and nothing about actually drawing a frame. **Fix**: gate 2 now prefers `es2gears_wayland` (GLES over EGL on Wayland, same virgl gallium path) and the final line is conditional — `full VirGL stack verified (renderer identity + render via <tool>)` only when a render actually ran, else `VirGL renderer identified, but NOT render-verified (gate 2 skipped)` + a `VIRGL-WARN`. Three non-obvious things this gate needs, all measured not assumed:
  - **Discovery must run as root.** Another user's runtime dir is mode `0700` (`/run/user/<gdm-greeter>`), so an unprivileged `find /run/user` returns `Permission denied` and silently reports "no display" — the gate then skips on a guest that has a perfectly good socket.
  - **A non-graphical SSH shell has no `WAYLAND_DISPLAY`/`DISPLAY`/`XDG_RUNTIME_DIR`**, so run the tool as the socket's owning user with that user's `XDG_RUNTIME_DIR` + `WAYLAND_DISPLAY` (`sudo -n -u`, or plain `env` when the socket is already ours). Unadorned it dies instantly with `EGLUT: failed to initialize native display`.
  - **The success signal is the exit code, not stdout.** `es2gears_wayland` prints *nothing* on success and renders forever, so `timeout` kills it and **rc=124** is the pass signal (rc=0 also passes); rc=1 + `EGLUT:` is the fail signal. Grepping its output for `frames|fps` (as the glxgears branch does) would always warn. Note `head`/pipes swallow `timeout`'s rc — append an explicit `__RC=$?` line and parse it back.
  - **Gentoo's render tool is built from `mesa/demos`, not emerged** (same clone that already provides `eglinfo`): `media-libs/mesa` has **no `demos` USE flag** and the fork tree at `/var/cache/mesa-fork` ships **no in-tree `demos/`** (and no `demos` option in `meson.options`) — Mesa moved the demos out of the main tree, so neither Portage nor the fork can produce them. GLTOOLS therefore clones `gitlab.freedesktop.org/mesa/demos` once and builds `eglinfo` **plus** `src/egl/opengles2/es2gears` + `src/egl/opengl/eglgears` from it into `/usr/local/bin`. **Upstream names them WITHOUT a `_wayland` suffix** — they are EGLUT-based, so one binary auto-detects X11 vs Wayland inside `eglutInit`; Ubuntu's `es2gears_wayland` name is Debian per-platform renaming. So `check-virgl.sh` probes `es2gears_wayland`, `es2gears`, `eglgears_wayland`, `eglgears`. Two traps: (a) with `-Dwayland=enabled`, `libdecor-0`/`wayland-client`/`wayland-egl`/`xkbcommon` are **hard** `dependency()` requirements (no `required: false`), so one missing aborts configure and would take the `eglinfo` build down with it — probe them all with `pkg-config --exists` first and fall back to `-Dwayland=disabled`; the dep is **`gui-libs/libdecor`** (not `x11-libs/`), which arrived only transitively via the GNOME stack, so it is now listed explicitly in `_gentoo_pkgs` *and* in the hand-maintained `PKGS=` duplicate inside `gentoo-install.sh` (the simulate/real **parity gate** fails if you add it to only one — that is what it is for); (b) **pass no arguments**: the demos hand argv straight to `eglutInit`, which rejects unknown flags and exits 2 (`Exited with code 2`), so `-info`/`-sb` are invalid — run them bare. Xwayland is **not** a fallback — `/tmp/.X11-unix/X1024` exists but fails `Authorization required` with no Xauthority. When adding any gate, prove it **fails** when it should (e.g. temporarily point it at a bogus display and confirm exit 1) before trusting a green run.
- **`status.json` is overwritten on every boot — the fleet snapshots it now**: `/var/lib/cloud/data/status.json` (symlinked from `/run/cloud-init/status.json`) only reflects the *current* boot's `recoverable_errors`. Re-booting the VM to inspect a finished build silently replaces the evidence with a clean `done`. **Fixed**: `linux_vm/fleet/ssh.py::_snapshot_cloud_init_status` runs in a `finally` around `_ssh_wait_cloud_init`, so *every* wait exit (success, terminal failure, timeout, exception) writes the raw JSON to `<target_dir>/cloud-init-status.json`, appends an `errors=`/`recoverable=` summary plus one line per message to the wait log, and `log_master`s a `WARN:` when either is non-empty — a genuinely degraded build can no longer pass silently. It never raises (best-effort evidence must not mask the caller's verdict). For a VM you boot by hand, reconstruct from `journalctl -b <N>` (the build boot stays listed as a negative index after reboot) and `/var/log/cloud-init.log`, which append rather than reset.
- **Every template password must be YAML single-quoted** — unquoted interpolation silently corrupts numeric/boolean-looking passwords: `password: {{ PASSWORD }}` renders `--password 1234` as an **int** and `--password yes` as **bool**, and cloud-init rejects both — `cloud-init schema` reports `users.0.plain_text_passwd: 1234 is not of type 'string'` and `chpasswd.users.0 ... is not valid under any of the given schemas` (this was a **pre-existing** bug in `chpasswd:`, not introduced by the `plain_text_passwd` fix). Fix: write `'{{ VAR | replace("'", "''") }}'` — YAML single quotes preserve every other byte literally, and `''` is the only escape. Verified against the guest with `sudo cloud-init schema --config-file` for `1234` / `yes` / `it's` / `a"b` / `p@ss:word`. Guarded by `tests/test_logic.py::TestPlainPasswordOnUserEntry`.

**Guest-arch gotchas that bite the aarch64 simulate/build path:**
- **aarch64 cloud-init seed must be `virtio-blk-pci`, NOT usb-storage**: cloud-init's generator runs `ds-identify` at ~1s; USB storage enumerates too late under TCG, so cloud-init disables itself for the whole boot (no user-data, no network). Rule: any change to seed attachment must be verified on the aarch64 build path.
- **QEMU `virt`-machine keyboard is PL050 PS/2 — aarch64 distro kernels don't ship the PL050 driver, so GDM has NO keyboard**: the mouse works only because the launcher adds `usb-tablet`. Fix: always add `-device usb-kbd,bus=xhci.0` next to the tablet (kernel `usbhid` is universal). Verify via `/proc/bus/input/devices` showing "QEMU QEMU USB Keyboard".

**The `GLD_TEXTURE_INDEX_2D is unloadable` message on QEMU stderr is benign** (Apple GLES + virglrenderer, `log once` + `gst-plugin-scan` virgl probes). Don't chase it.

**qemu-virgl master bottle (QEMU 11.1.50) hangs EDK2 boot under HVF with its default in-kernel vGIC**: the vCPU spins at a fixed PC with zero HVF exits (`-d int` logs nothing after reset), guest never reaches grub, SSH never comes up. TCG boots fine, ruling out firmware/ROMs. Workaround (patched into `linux_vm/qemu.py`): `virt,accel=hvf:tcg,kernel-irqchip=off` forces userspace GIC emulation. Revisit and revert once upstream fixes the vGIC-on-HVF path.

**A mid-emerge `net-misc/openssh` merge silently kills SSH for the rest of the Gentoo install (cost two full builds until root-caused via journal forensics)**: the running `fleet-ssh` listener forks and execs the *on-disk* sshd session binary per connection; when a world update (`-uDn`) merges a new openssh, the new session code expects 4 hostkeys (OpenSSH 10.x added the `mldsa44` post-quantum host key) while the old listener passes 3 → every NEW connection aborts before the banner with `internal error: hostkeys confused (config 4 recvd 3)`. Signature: TCP:2222 accepts but `nc -w5 127.0.0.1 2222` returns no banner and `ssh` dies with `kex_exchange_identification: Connection closed by remote host`, while QEMU stays busy and the disk keeps growing — **it is NOT a guest wedge** (misdiagnosed twice as a wedge/CPU-starvation before forensics). The listener never dies, so `fleet-ssh.service`'s `Restart=always` never fires: only SSH is black, the fleet's `rc=255` probes keep retrying (it tolerates long blind windows — run 3's wait never aborted), but no `VERIFY-OK` can be observed and the run looks dead. Root-cause forensics: the guest journal is **persistent** — `journalctl -b -1 -t sshd` shows the first-error timestamp, which matched the fleet's first `rc=255` probe to the second, and `sudo grep openssh /var/log/emerge.log` shows the merge landing minutes earlier (630/637 in the main gnome emerge). **Fix (committed)**: `heal_ssh()` in `gentoo-install.sh` (`ssh-keygen -A` so newly-introduced host key types exist, then `systemctl restart fleet-ssh.service`; ~1 s recovery, verified live on a post-merge guest) called after every `-uDn` world-update site — STAGE1 git emerge, main gnome emerge, PKGS loop. Post-fix run: openssh merge → 31 s self-healed outage → 30/30 probes `rc=0` → `VERIFY-OK`. If you ever see `hostkeys confused` again, restart the listener — don't kill the build.

**Gentoo mesa-fork build failure modes (three separate 2.5h cycles to find)**: (1) the fork's default branch is `main`, not `master` — clone fails with "Remote branch master not found"; (2) Gentoo slots LLVM so `llvm-config` is in `/usr/lib/llvm/<N>/bin`, not on PATH — meson needs a `--native-file` with `llvm-config = /usr/lib/llvm/22/bin/llvm-config` or it fails looking for a non-existent `llvm.wrap` subproject; (3) the libgallium guard `ls /usr/lib64/libgallium-*.so /usr/lib/libgallium-*.so` fails under `set -u` because the unmatched `/usr/lib` glob stays literal — test each libdir independently. Also: disarm the mesa section's `ERR` trap (`trap - ERR`) before the best-effort Chrome step, or `eselect repository enable gentoo` (rc=250 "already enabled") trips it and masks a successful build.

**QEMU stderr `virtio_gpu_virgl_process_cmd: ctrl 0x103, error 0x1205` is BENIGN and is NOT a blob failure.** `0x103` is `VIRTIO_GPU_CMD_SET_SCANOUT`, *not* `RESOURCE_CREATE_BLOB` (which is `0x10c` — the 2D command block starts at 0x100: GET_DISPLAY_INFO=0x100, CREATE_2D=0x101, UNREF=0x102, SET_SCANOUT=0x103, ..., CREATE_BLOB=0x10c). The 0x1205 (`ERR_INVALID_PARAMETER`) comes from `virtio_gpu_check_scanout_bounds()` (virtio-gpu.c) rejecting the EDK2/early-boot driver's scanout probe (a rect with `x+width > 0` while QEMU passes `width=0,height=0` in the virgl set_scanout path). It repeats during boot and stops once the real display is set; the desktop and virgl still come up fine. **Don't chase it as a Venus/blob bug.** To decode a virtio-gpu command/error, read the enum in `include/standard-headers/linux/virtio_gpu.h` — never assume the command number.

**Venus blob debugging: verify the flag at runtime, not by reading the property table.** When Venus `ctrl 0x10c` fails with "blob not enabled", add a temporary `fprintf(stderr,...)+fflush` in `virtio_gpu_gl_device_realize` and `virgl_cmd_resource_create_blob` printing `g->parent_obj.conf.flags`, and do a **fast local build** (`./configure --target-list=aarch64-softmmu --enable-cocoa --enable-opengl --enable-virglrenderer ...` + `ninja qemu-system-aarch64`, ~4 min) rather than a 27-min bottle publish. `blob`/`venus`/`hostmem` are valid `-device virtio-gpu-gl-pci` props (QEMU forwards them to the embedded `VirtIOGPUGL` child). With `venus=on,blob=on,hostmem=512m` the realize-time flags should read `0x16a` (VIRGL|EDID|BLOB|CONTEXT_INIT|VENUS). Confirmed: blob flag set, `virgl_renderer_resource_create_blob` returns 0, guest negotiates `+resource_blob` — the QEMU side is correct; a guest-side "MESA-VIRTIO: stuck in ring seqno wait" hang is a **guest Mesa fork ↔ host virglrenderer Venus** protocol issue, not a QEMU blob-flag issue.

**Venus end-to-end: SOLVED — three host-side bugs, not a guest/protocol mismatch.** `check-venus.sh --ring` now passes (Venus device enumerates as "Virtio-GPU Venus (Apple M5 Pro)"; ring submit+fence OK). The silent client SIGABRT (`aborting on expired ring alive status`) was a chain of three independent host bugs, each masked by the previous one:
1. **HVF blob coherency (QEMU fork `d555313d1a` #1)**: `VIRGL_HAS_MAP_FIXED` swaps host pages under the already-mapped hostmem ramblock. KVM's mmu_notifier catches this; HVF does not, so the guest EPT keeps pointing at the original anonymous pages. Symptom: server and QEMU see the blob contents (verified with a magic-value probe), the guest reads zeros — vn_ring submit writes never reach the server and status bits never reach the client. Fix: disable map_fixed on `__APPLE__`, use the blob-subregion fallback (adding a MemoryRegion forces HVF to re-map).
2. **Ring startup IDLE bit (virglrenderer fork `d12b0be1`)**: `vkr_ring_start` must set `VK_RING_STATUS_IDLE_BIT_MESA` before the thread starts — the Mesa client only ever sends the wake-up notify after observing IDLE, and its notify throttle is zero-initialized so the first observation always notifies. Without it, the ring still worked via polling most of the time, which made this bug intermittent and the earlier "server doesn't set IDLE" root-cause analysis incomplete.
3. **Fence retirement without eventfd (QEMU `d555313d1a` #2 + virglrenderer `d12b0be1`)**: macOS has no eventfd, so the proxy sync-thread notification chain is dead and the render server only advances the shmem timelines. With `VIRGL_RENDERER_ASYNC_FENCE_CB` set (QEMU always sets it under EGL), `virgl_renderer_poll()` skipped proxy-context retirement entirely, and execbuffer fences without `INFO_RING_IDX` went to the *global* ctx0 timeline which never advances in render-server mode → `vkQueueSubmit` fences never complete. Fixes: QEMU routes all `ctx_id != 0` fences through `virgl_renderer_context_create_fence`; virglrenderer lets `virgl_renderer_poll()` retire proxy contexts even in async mode.
Debugging technique that cracked it: instrument BOTH sides with `fprintf(stderr)+fflush` (server traces land in `qemu_stderr.log`), compare the `VkRingCreateInfoMESA` fields client-vs-server to rule out protocol mismatch, then a magic-value write/read probe to prove blob-page identity across processes. gdb does NOT work in the guest (SVE ptrace errors on this kernel) — use an `LD_PRELOAD`ed `backtrace_symbols_fd` SIGABRT handler instead. `MESA_LOG_LEVEL=debug` is essential: `vn_log` logs at DEBUG level and is silently dropped by default, which is why venus aborts look "silent".

**QEMU SIGABRT (`assert_hvf_ok` in `hvf_set_phys_mem`) on unaligned virgl blob maps — FIXED in qemu fork commit `4fc203647d`.** Apple HVF requires host-page (16 KB) alignment for `hv_vm_map`/`hv_vm_unmap`, but guest blob geometry is guest-page (4 KB) granular: the guest kernel packs hostmem blobs at 4 KB offsets, and vrend's `glMapBufferRange()` host pointers are arbitrary. The first unaligned `RESOURCE_MAP_BLOB` (gnome-shell's first GL blob as GDM starts) made `hvf_set_phys_mem` flip `add=false` and call `hv_vm_unmap()` on a *never-mapped* range → `HV_BAD_ARGUMENT (0xfae94003)` at `accel/hvf/hvf-all.c` → assert → whole VM dies mid-boot. Symptom: console.log just stops at GNOME-session startup (the benign `0x103/0x1205` scanout errors are the last lines), SSH drops, port 2222 refuses; crash report lands in `~/Library/Logs/DiagnosticReports/qemu-system-aarch64-*.ips` showing `assert_hvf_ok_impl` ← `address_space_update_topology_pass` ← `virtio_gpu_virgl_process_cmd`. Fixes: `hvf-all.c` skips (warn-once) unaligned RAM sections instead of unmapping; `virtio-gpu-virgl.c` rejects MAP_BLOB with `-EOPNOTSUPP` under HVF when host ptr/size/offset aren't host-page aligned (guest gets `VIRTIO_GPU_RESP_ERR_UNSPEC`, falls back to transfers). Venus still needs the guest mesa fork's 16 KB blob alignment (`1be02a69192 "venus: align hostmem blobs to 16KB for Apple Silicon"`) — the in-guest mesa-fork build in both templates provides it; stock-mesa guests keep working VirGL but Venus fails gracefully (`vkCreateInstance` → `ERROR_OUT_OF_HOST_MEMORY`). Diagnosis gotchas: `setup_vm.py` launches QEMU with stderr → DEVNULL, so relaunch via `/bin/sh launch-vm.sh 2> qemu_stderr.log` to capture the assert; `qemu_log_mask(LOG_GUEST_ERROR)` is masked unless QEMU runs with `-d guest_errors -D <file>` (note: the `-d` item is `guest_errors`, NOT `guest_error` — a typo silently prints the `-d` help and exits 1). Also: cloud-init per-instance modules (`cc_scripts_user`/runcmd) run ONCE — a QEMU crash mid-provisioning leaves the VM semi-provisioned (GDM up, but no mesa-fork/snaps/VERIFY-OK) and simply rebooting will NOT re-run them; use `sudo cloud-init clean --logs` + reboot for a full re-provision.

**Post-snag-list forensics scars (root-caused on the built VMs after the first full-fleet validation run — these are template bugs that shipped silently for a long time):**

**Root-run `xdg-settings` in `_app_platforms_common.j2` created root-owned dotdirs and caused FOUR unrelated-looking user-facing bugs**: the default-browser step ran `xdg-settings set default-web-browser` as root with `HOME=/home/<user>`, which created `~/.config` and `~/.local/share/applications` owned by `root:root`. Cascade: (1) GNOME Initial Setup's `~/.config/gnome-initial-setup-done` marker couldn't be written → **the first-load wizard reappeared on every boot**; (2) user dconf wasn't writable → **the light-mode switch was dead** (system-wide `prefer-dark` could be set but not overridden); (3) Chrome refused to start with `No Home Directory. Make sure the directory exists and has correct access permissions set.`; (4) on Gentoo, EDS/gnome-shell couldn't create `~/.local/share/evolution` or their session data → **the clock showed but the calendar widget was empty** and gnome-shell ran degraded. One bug, four symptoms across both distros. Fix: run the step as the target user (`runuser -u <user> -- env HOME=... xdg-settings ...`) plus an unconditional `chown -R <user>:<user> ~/.config ~/.local` net. Signature to check first on any "can't change X / wizard repeats / app won't start" report: `ls -ld /home/*/.config /home/*/.local`.

**`systemctl --global enable A B C ...` is TRANSACTIONAL, and a `-systemd` binpkg silently removed the unit → every Gentoo VM shipped with no audio**: emerge's default `--binpkg-respect-use=n` happily installed `media-video/wireplumber` built WITHOUT `systemd`, so `/usr/lib/systemd/user/wireplumber.service` didn't exist; the single `systemctl --global enable pipewire.service pipewire.socket pipewire-pulse.* wireplumber.service` then exited rc=1 creating **zero** symlinks (one missing unit aborts the whole transaction) and `|| true` hid it — PipeWire never started for the session even though virtio-snd, the `audio` group and pipewire itself were all fine. The `_runcmd_common.j2` copy also fired pre-install on Gentoo (units don't exist yet), same one-liner. Fix: `media-video/wireplumber` added to the `--binpkg-respect-use=y` emerge (+ explicit `media-video/wireplumber systemd` in package.use — a plain `package.use` line does NOT unseat a `respect-use=n` binpkg), **per-unit** enable loops with `GLOBAL-ENABLE-OK/MISS` markers, and verify asserts on the unit file + `systemctl --global is-enabled pipewire.socket`. Diagnostic signature: `systemctl --global is-enabled pipewire.socket` → `disabled` with no error anywhere in the logs. (Package USE subtlety: `emerge -p` output `USE="systemd*"` means the chosen binpkg's value differs from what you'd get from config — re-run the pretend with the exact flag you intend to ship before trusting the selection.)

**Gentoo Snap Store missing: `experimental.user-daemons` + a failure-masked pipeline** — `snap install snap-store` failed outright with `error: cannot install "snap-store": experimental feature disabled - test it by setting 'experimental.user-daemons' to true` (no `/var/lib/snapd/features/user-daemons`), and the block hid it twice over: `snap install ... | tail -3 || echo FAIL` never failed (the pipeline's rc was tail's, always 0) and `SNAPS-OK` printed unconditionally after it. Fix: `snap set system experimental.user-daemons=true` before the installs (as root — doing it as a non-root user without sudo fails with access denied), and every step tests its real rc and emits exactly one truthful marker (`SNAP-STORE-OK/FAIL`, `SNAPS-OK` only when `snap list` proves both). Note: Gentoo's kernel has NO AppArmor (`enabled=N`, not in the LSM list) — `snapd.apparmor.service` has `ConditionSecurity=apparmor` so enabling it is inert, snaps run unconfined (classic/devmode) and `snap run` works; ignore `snap warnings`' "snapd.apparmor is disabled" noise on Gentoo.

**Gentoo Chrome was masked THREE ways at once**: (1) `LICENSE="google-chrome"` is in the **EULA** license group, not `@BINARY-REDISTRIBUTABLE`, so the existing `package.license/binredist` file didn't accept it — `emerge www-client/google-chrome` failed in <1 s with "masked by: google-chrome license(s)" (fix: `package.license/google-chrome` containing `www-client/google-chrome google-chrome`; `--accept-license` is NOT a valid emerge flag); (2) no arm64 binpkg exists upstream (binhost 404s `www-client/google-chrome`), so `--getbinpkgonly` could never work — the arm64 `.deb` comes from `dl.google.com` and the ebuild just unpacks it (fast, deps already present); (3) the post_runcmd retry loop's `eselect repository enable gentoo &&` short-circuited (rc=250 "already enabled") so **emerge never ran from that site at all**, and `| tail -5` masked emerge's rc so `CHROME-OK` could print on failure. Also: chrome's dep-resolve includes `media-libs/mesa`, and the mesa-fork section **unmerges** stock mesa — so a post-fork `emerge www-client/google-chrome` merges STOCK mesa over the fork and clobbers the fork's dri/libgallium files (observed live). Fix: no eselect, no pipe, real rc, and `--nodeps` on both chrome sites (every dep except mesa is already present; the fork provides mesa). Keep chrome best-effort (marker, not verify-asserted) — it's a vendor fetch.

## 🧾 Review-hardening invariants (don't undo these without re-reading the tests)

**`grep -F MARKER file1 file2` exits 2 on a MATCH when either file is missing — so `_check_success_marker` must not gate on `rc == 0`.** Only `templates/gentoo.j2:1199` writes `/var/log/verify-marker.log`; `ubuntu.j2`'s verify block (via `templates/_macros.j2`) only echoes to stdout. Ubuntu's grep therefore prints the match to stdout, errors on the absent second file, and exits **2** — an `rc == 0` gate would fail every Ubuntu build despite VERIFY-OK being present. The check is now `marker in (stdout or "")` (stdout holds only matched lines now that the old `2>&1`/`|| echo NO_MARKER` tail is gone) with `rc not in (0, 1, 2)` used only to note that grep may never have run. Locked in by `tests/test_fleet.py::TestCheckSuccessMarker::test_grep_rc2_with_marker_still_passes`.

**A `subprocess` timeout kill does not reach a `start_new_session=True` grandchild.** `setup_vm.py` launches QEMU via `Popen(..., start_new_session=True)`, so when the fleet's `run_to_file` timeout fired it killed only the python parent and the VM kept running — invisible, because `build_and_provision` only set `vm_started = True` *after* `run_to_file` returned, so the `finally` never shut it down. Fix on both sides: `fleet/executor.py` now runs the child with `Popen` + `start_new_session=True` and `_kill_process_tree()` (getpgid → `os.killpg(SIGKILL)`), and `fleet/orchestrator.py` has `_qemu_running_for(target)` which flips `vm_started` on in the `finally` so an orphan is cleaned up regardless of which failure path produced it. An orphan burns host RAM/CPU for the rest of a multi-hour run — exactly the starvation condition that soft-locks guests.

**`--keep-qcow2` does NOT mean "reuse the disk I built last time".** `linux_vm/qemu.py::prepare_disk` always `unlink()`s `disk.qcow2` and re-converts + resizes from the cloud image, so every build starts from a pristine disk even with the flag; the flag only preserves the *extracted intermediate* image from the archive. That is why `shutdown_and_verify` now waits up to `SHUTDOWN_TIMEOUT_SEC` for a clean self-shutdown before SIGKILL — an unclean kill costs a re-extract, not a corrupted build.

**`audit-packages.py`'s gentoo set must be extracted from BOTH renders.** The simulate render's package list (`/etc/install-simulate-pkgs.txt`) does not include packages the real `gentoo-install.sh` emerges (binhost/tools/vendor steps), so the audit read only the simulate set and could report a category present that the real build never installs. It now merges `gentoo_atoms_from_text()` over the real install script's `emerge` lines and its hand-maintained `PKGS=` list, with a path-prefix blocklist applied to both (the `PKGS=` branch previously had none, so `tmp/foo`-shaped entries leaked into the audit set).

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
- He runs on **macOS** (Apple Silicon MacBook Pro 18 cores, QEMU/HVF). Keep the host otherwise idle during fleet builds — a VM using all the host's cores while the app + agent polling run can soft-lock the guest (see Known battle scars).
- He prefers **explicit over implicit** — even if a package is pulled in transitively, add it to the install list for clarity.
- He expects **autonomous iteration on simulate failures** — don't ask for input on every package fix; iterate the simulate-fix-rerun loop and only escalate when the fix would change user-facing behaviour or >3 unrelated failures suggest a systemic issue.
- He expects **comprehensive forensics on real-build failures** — every failure should produce enough diagnostic dump that the assistant can root-cause without operator input. New `UNKNOWN`-category patterns should be proactively added to the categoriser.
- **No dates in docs** — never write absolute dates (any `YYYY-MM-DD` form) into `README.md`, `AGENTS.md`, or `docs/*.md`, including in timing passages. Reference measurements relatively instead: *latest run*, *previous run*, *earlier run*, *fastest measured*.
- **Real fleet builds have passed** — the fleet completed the full matrix with `VERIFY-OK`. The gate counts quoted in the run records below (lint 12/12, "all 5 gates") describe the gate set as it stood when those runs were measured; Gate 0d (unit tests) was added afterwards, so a fresh run reports 16/16 on lint and one more gate — the per-VM and gate timings are unaffected. Verified all-2 fleet runs measured **1.71-5.16 h wall** on the Apple Silicon MacBook Pro: the latest run (cold cache — `~/VMs` wiped, images re-downloaded; every gate green: lint 16/16, audit 101/101, pylint 10.00/10, 101 tests, smoke, prefetch, simulate 2/2 PASS in 25.6 min [ubuntu-lts 1.8 min + gentoo 23.8 min]) reached `DONE in 5.16h` + ubuntu-lts 43.8 min VM total / 42.3 min cloud-init + gentoo 235.5 min VM total / 231.4 min cloud-init, both VERIFY-OK, 0 errors, 0 recoverable errors (gentoo's slow end was ~3.5 h of arm64 source builds — papers Rust, snapd Go, glslang, spirv-tools, mesa 26.1.8 — and its known openssh-merge SSH blip self-healed via `heal_ssh` in ~3 min); a previous cold-cache run (gate set as it then stood) reached `DONE in 1.71h` with all 5 gates passed (lint 12/12, audit 101/101, smoke, prefetch 1.9 min, simulate 15.0 min [ubuntu-lts 1.8 min + gentoo 13.2 min]) + ubuntu-lts 16.5 min VM total / 15.1 min cloud-init + gentoo 67.0 min VM total / 63.3 min cloud-init, both VERIFY-OK, 0 errors, 0 recoverable errors; a previous warm-binhost run (ubuntu-lts 37.9 min + gentoo 45.5 min + prefetch + ~19 min simulate gate) at 1.72 h; an earlier cold-cache run at 2.27 h (ubuntu-lts 38.2 min + gentoo ~65 min cloud-init); up to 4.56 h on a slower run (ubuntu-lts 49.0 min + gentoo 214.2 min + ~10 min simulate gate). Gentoo dominates the spread (cloud-init 42-231 min, binhost-warmth + host-load dependent; the slow end is arm64 source builds landing cold — papers Rust, snapd Go, glslang, spirv-tools, mesa — not a defect, and recent runs add a ~10 min in-guest build of the miles.buckton mesa fork). Latest gentoo full build: **235.5 min VM total / 231.4 min cloud-init, VERIFY-OK** (openssh-merge blip self-healed by `heal_ssh` in ~3 min, full VERIFY-OK, 0 recoverable errors); previous gentoo full build **67.0 min VM total / 63.3 min cloud-init, VERIFY-OK** (clean run with `heal_ssh`, full VERIFY-OK, 0 recoverable errors); previous gentoo rerun **167.8 min VM total / 164.1 min cloud-init, VERIFY-OK** (~65 min of PKGS-loop source builds). Fastest measured ubuntu build: **16.4 min total / 15.1 min cloud-init, VERIFY-OK** (previous run matched it closely at 16.5 min total / 15.1 min cloud-init; the latest run's ubuntu took 42.3 min cloud-init, inside the 16-49 min band). Latest simulate gate: **2/2 PASS in 25.6 min** (ubuntu-lts 1.8 min + gentoo 23.8 min; previous cold pass 15.0 min with gentoo 13.2 min; earlier cold pass 0.46 h with gentoo 23.6 min; previous warm pass 0.34 h with gentoo 18.3 min; gentoo fastest 8.2 min). Latest ubuntu-lts full build: **43.8 min VM total / 42.3 min cloud-init, VERIFY-OK**; previous ubuntu-lts full build **16.5 min VM total / 15.1 min cloud-init, VERIFY-OK** (first SUCCESS 35.5 min after launch of cold all-2 fleet). See ARCHITECTURE.md for detailed timing.

## 🔄 Project skills

- **`update-mesa-fork`** (`.opencode/skills/update-mesa-fork/SKILL.md`): Sequentially boots each VM (Ubuntu LTS then Gentoo), pulls the latest `main` of the miles.buckton Mesa fork inside `/var/cache/mesa-fork`, rebuilds and reinstalls it, and runs `scripts/check-virgl.sh` and `scripts/check-venus.sh` on the host. Use whenever refreshing Mesa on existing VMs after a fork push.

## 📚 Where to look next

- **User-facing docs**: [README.md](./README.md)
- **Architecture, timing, fleet building**: [ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- **Day-2 ops** (snapshots, SSH, etc.): README's "Day-2 operations" section
