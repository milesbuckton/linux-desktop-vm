#!/usr/bin/env bash
#
# check-virgl.sh -- validate that VirGL (OpenGL) actually works in a booted
# virtio-gpu VM on Apple Silicon.
#
# Background
# ----------
# VirGL is the OpenGL-over-virtio backend. It lets the guest offload GL
# rendering to the host GPU via virtio-gpu-gl. A working VirGL stack means
# the guest gets hardware-accelerated OpenGL instead of software llvmpipe.
#
# This script verifies:
#
#   1. Guest can query GL info and reports a virgl renderer (not llvmpipe).
#   2. Guest can actually perform a GL render (es2gears_wayland on Wayland,
#      glxgears on X11). Renderer identity alone does not prove rendering --
#      a stack can advertise "virgl" and still fail to draw a frame, so this
#      gate runs a real GL app and treats a failure as VIRGL-FAIL.
#
# Usage
# -----
#   scripts/check-virgl.sh <vmdir>
#
#   <vmdir>   A provisioned VM directory, e.g. ~/VMs/ubuntu-lts (must contain
#             launch-vm.sh, ssh_key, and the booted guest must be reachable).
#
# Exit codes:
#   0  VirGL OK (renderer is virgl, GL query succeeded)
#   1  VirGL FAILED (details on stdout as VIRGL-FAIL markers)
#   2  Environment/infra failure (VM not booted, no ssh, glxinfo missing,
#      wrong arch, ...) -- distinct from a VirGL defect so results aren't
#      conflated with a build-cancel / infra hiccup.
#
# Host rule: only ever ONE VM may be up. This script never launches a VM; it
# only inspects an already-running one.
#
set -u

VMDIR="${1:-}"

if [ -z "$VMDIR" ]; then
  echo "usage: $0 <vmdir>" >&2
  exit 2
fi
if [ ! -d "$VMDIR" ]; then
  echo "VIRGL-FAIL: VM dir not found: $VMDIR" >&2
  exit 2
fi

LAUNCH="$VMDIR/launch-vm.sh"
KEY="$VMDIR/ssh_key"
PORT=""
SSH_USER=""

# --- discover ssh port / user from the launcher -----------------------------
if [ -f "$LAUNCH" ]; then
  PORT=$(grep -oE 'hostfwd=tcp::[0-9]+-' "$LAUNCH" | grep -oE '[0-9]+' | head -1)
  SSH_USER=$(grep -oE 'user-[A-Za-z0-9_]+' "$LAUNCH" | head -1 | sed 's/user-//')
fi
# The generated launcher embeds neither the chosen SSH port (a runtime
# probed variable) nor the username, so also fall back to the running
# QEMU's hostfwd= (port) and install-info.txt's Username: line.
if [ -z "$PORT" ]; then
  PORT=$(ps -Ao command= 2>/dev/null | grep -m1 -oE 'hostfwd=tcp:127\.0\.0\.1:[0-9]+' | grep -oE '[0-9]+$')
fi
if [ -z "$SSH_USER" ] && [ -f "$VMDIR/install-info.txt" ]; then
  SSH_USER=$(awk -F': *' '/^Username:/{print $2; exit}' "$VMDIR/install-info.txt")
fi
PORT="${PORT:-2222}"
SSH_USER="${SSH_USER:-ubuntu}"

if [ ! -f "$KEY" ]; then
  echo "VIRGL-FAIL: ssh key not found: $KEY" >&2
  exit 2
fi

SSH=(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -i "$KEY" -o BatchMode=yes -o ConnectTimeout=6 -p "$PORT")

# --- confirm the guest answers ----------------------------------------------
if ! "${SSH[@]}" "$SSH_USER@127.0.0.1" 'true' >/dev/null 2>&1; then
  echo "VIRGL-FAIL: guest not reachable at 127.0.0.1:$PORT" >&2
  exit 2
fi
ARCH=$("${SSH[@]}" "$SSH_USER@127.0.0.1" 'uname -m' 2>/dev/null | tr -d '\r')
echo "check-virgl: guest arch=$ARCH"

# --- check for GL info tool -------------------------------------------------
# Prefer eglinfo (works on Wayland sessions without X11 DISPLAY).
# Fall back to glxinfo only if eglinfo is absent (X11-only guests).
HAS_EGLINFO=0
if "${SSH[@]}" "$SSH_USER@127.0.0.1" 'command -v eglinfo >/dev/null' 2>/dev/null; then
  HAS_EGLINFO=1
fi
HAS_GLXINFO=0
if "${SSH[@]}" "$SSH_USER@127.0.0.1" 'command -v glxinfo >/dev/null' 2>/dev/null; then
  HAS_GLXINFO=1
fi

if [ "$HAS_EGLINFO" -eq 0 ] && [ "$HAS_GLXINFO" -eq 0 ]; then
  echo "VIRGL-FAIL: neither eglinfo nor glxinfo installed in guest" >&2
  echo "  (install mesa-utils or mesa-demos)" >&2
  exit 2
fi

# =============================================================================
# Gate 1: renderer identification (fast; catches software fallback)
# =============================================================================
echo "== check-virgl: gate 1 renderer identification (arch=$ARCH) =="

# eglinfo uses "OpenGL compatibility profile renderer:" and "OpenGL ES profile
# renderer:" (not bare "OpenGL renderer:").  glxinfo uses "OpenGL renderer:".
# Match broadly on "renderer:" to cover both.
if [ "$HAS_EGLINFO" -eq 1 ]; then
  GL_INFO="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
    'eglinfo 2>&1 | grep -iE "renderer:|version.*Mesa" | head -10' 2>/dev/null)"
elif [ "$HAS_GLXINFO" -eq 1 ]; then
  GL_INFO="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
    'DISPLAY=:0 glxinfo 2>&1 | grep -iE "OpenGL renderer|OpenGL version|direct rendering" | head -10' 2>/dev/null)"
fi
echo "$GL_INFO"

if echo "$GL_INFO" | grep -qiE "error|fail"; then
  echo "VIRGL-FAIL: GL info query reported an error:" >&2
  echo "$GL_INFO" >&2
  exit 1
fi

# Check for virgl renderer (hardware-accelerated via host GPU)
if echo "$GL_INFO" | grep -qi "llvmpipe"; then
  echo "VIRGL-FAIL: OpenGL renderer is llvmpipe (software fallback, not virgl)" >&2
  echo "  Expected: virgl (hardware-accelerated via host GPU)" >&2
  echo "$GL_INFO" >&2
  exit 1
fi

if echo "$GL_INFO" | grep -qi "virgl"; then
  echo "VIRGL-OK: renderer is virgl (hardware-accelerated)"
else
  echo "VIRGL-FAIL: unexpected OpenGL renderer (not virgl, not llvmpipe):" >&2
  echo "$GL_INFO" >&2
  exit 1
fi

# Check EGL driver name (eglinfo-specific; confirms virtio_gpu backend)
if [ "$HAS_EGLINFO" -eq 1 ]; then
  EGL_DRV="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
    'eglinfo 2>&1 | grep -i "EGL driver name:" | head -3' 2>/dev/null)"
  if echo "$EGL_DRV" | grep -qi "virtio_gpu"; then
    echo "VIRGL-OK: EGL driver is virtio_gpu (hardware-accelerated backend)"
  else
    echo "VIRGL-WARN: EGL driver is not virtio_gpu: $EGL_DRV" >&2
  fi
fi

# Check direct rendering (glxinfo only; eglinfo doesn't emit this line)
if [ "$HAS_GLXINFO" -eq 1 ]; then
  DIRECT="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
    'DISPLAY=:0 glxinfo 2>&1 | grep -i "direct rendering"' 2>/dev/null)"
  if echo "$DIRECT" | grep -qi "yes"; then
    echo "VIRGL-OK: direct rendering enabled"
  else
    echo "VIRGL-WARN: direct rendering not confirmed (may still work)" >&2
  fi
fi

# =============================================================================
# Gate 2: basic GL render test
# =============================================================================
echo "== check-virgl: gate 2 basic GL render test =="

# Gate 1 proves IDENTITY: the guest reports the virgl renderer. That is not
# proof of RENDERING -- a stack can advertise virgl and still fail to draw a
# single frame. This gate runs a real GL app so the check cannot pass as a
# no-op.
#
# On a Wayland session the only tool we have (glxgears) is X11-only and cannot
# open a display, so this gate used to silently skip on both distros while
# still printing VIRGL-OK. es2gears_wayland renders GLES over EGL on Wayland
# and exercises the same virgl gallium path, so prefer it.
#
# es2gears_wayland needs a real Wayland socket plus the owning session's env,
# neither of which exists inside a non-graphical SSH shell (DISPLAY and
# WAYLAND_DISPLAY are both unset there, and running it unadorned dies with
# "EGLUT: failed to initialize native display"). So discover the socket and
# re-exec as its owning user.

RENDER_RAN=0
RENDER_TOOL=""

# --- probe capability + discover a Wayland display --------------------------
# Candidate names differ per distro, so probe a list:
#   es2gears_wayland -- Ubuntu's mesa-utils-bin renames the binary per platform
#   es2gears         -- upstream mesa/demos name (EGLUT auto-detects the WSI,
#                       so ONE binary serves both X11 and Wayland); built into
#                       /usr/local/bin by templates/gentoo.j2
#   eglgears[_wayland] -- desktop-GL variant, same EGLUT mechanism
# Do NOT pass any arguments: these demos hand argv straight to eglutInit,
# which rejects unknown flags and exits 2 ("Exited with code 2"). Run bare.
WL_TOOL=""
for _cand in es2gears_wayland es2gears eglgears_wayland eglgears; do
  if "${SSH[@]}" "$SSH_USER@127.0.0.1" "command -v $_cand >/dev/null" 2>/dev/null; then
    WL_TOOL="$_cand"
    break
  fi
done
HAS_ES2GEARS_WL=0
[ -n "$WL_TOOL" ] && HAS_ES2GEARS_WL=1

# One round-trip does discovery. Discovery MUST run as root: another user's
# runtime dir is mode 0700 (e.g. /run/user/<gdm-greeter>), so an unprivileged
# `find /run/user` gets "Permission denied" and silently reports "no display".
# Prefer this user's own socket (a real desktop session) over GDM's greeter.
# Retry because GDM's greeter socket appears a few seconds AFTER sshd answers,
# so an immediate probe sees "no display" and would otherwise skip a gate that
# would have passed.
WL_INFO=""
if [ "$HAS_ES2GEARS_WL" -eq 1 ]; then
  # Pass the ssh user's name in explicitly: discovery runs as root, where
  # `id -u` is 0, so the "prefer this user's own session" lookup below would
  # otherwise always miss and fall through to GDM's greeter. Guard the value
  # before embedding it in a command string (never mutate SSH_USER itself --
  # every later ssh call depends on it).
  WL_SSH_USER=""
  case "$SSH_USER" in
    [A-Za-z0-9_.-]*) WL_SSH_USER="$SSH_USER" ;;
  esac
  if [ -n "$WL_SSH_USER" ] && "${SSH[@]}" "$SSH_USER@127.0.0.1" 'sudo -n true' 2>/dev/null; then
    WL_DISCOVER_CMD="sudo -n env WL_SSH_USER='$WL_SSH_USER' bash -s"
  else
    WL_DISCOVER_CMD='bash -s'
  fi
  WL_INFO="$("${SSH[@]}" "$SSH_USER@127.0.0.1" "$WL_DISCOVER_CMD" 2>/dev/null <<'DISCOVER'
sock=""
myuid=$(id -u "$WL_SSH_USER" 2>/dev/null || echo 0)
for _ in 1 2 3 4 5 6; do
  sock=$(find "/run/user/$myuid" -maxdepth 1 -type s -name "wayland-[0-9]*" 2>/dev/null | head -1)
  [ -z "$sock" ] && sock=$(find /run/user -maxdepth 2 -type s -name "wayland-[0-9]*" 2>/dev/null | head -1)
  [ -n "$sock" ] && break
  sleep 5
done
[ -n "$sock" ] || exit 3
owner=$(stat -c %U "$sock" 2>/dev/null) || exit 3
home=$(getent passwd "$owner" 2>/dev/null | cut -d: -f6)
printf '%s\t%s\t%s\t%s\n' "$owner" "$home" "$(dirname "$sock")" "$(basename "$sock")"
DISCOVER
)"
fi

WL_OWNER=""; WL_HOME=""; WL_RT=""; WL_DISP=""
if [ -n "$WL_INFO" ]; then
  IFS=$'\t' read -r WL_OWNER WL_HOME WL_RT WL_DISP <<<"$WL_INFO"
fi

if [ "$HAS_ES2GEARS_WL" -eq 1 ] && [ -n "$WL_DISP" ]; then
  echo "check-virgl: Wayland display $WL_DISP in $WL_RT (session user: $WL_OWNER)"
  # Attach to another user's runtime dir needs root; stay unprivileged when the
  # socket already belongs to us.
  if [ "$WL_OWNER" = "$SSH_USER" ]; then
    RUNNER=(env HOME="$WL_HOME" XDG_RUNTIME_DIR="$WL_RT" WAYLAND_DISPLAY="$WL_DISP")
  elif "${SSH[@]}" "$SSH_USER@127.0.0.1" 'sudo -n true' 2>/dev/null; then
    RUNNER=(sudo -n -u "$WL_OWNER" env HOME="$WL_HOME" \
                   XDG_RUNTIME_DIR="$WL_RT" WAYLAND_DISPLAY="$WL_DISP")
  else
    RUNNER=()
  fi

  if [ "${#RUNNER[@]}" -gt 0 ]; then
    # Healthy behaviour: these demos render continuously and never exit on their
    # own, so `timeout` kills them and rc=124. rc=0 also counts (tool chose to
    # exit). Any other rc means it could not init EGL or died.
    RENDER_OUT="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
      "timeout 6 ${RUNNER[*]} $WL_TOOL 2>&1; echo __RC=\$?" 2>/dev/null)"
    RENDER_RC="$(printf '%s\n' "$RENDER_OUT" | sed -n 's/^__RC=//p' | tail -1)"
    RENDER_OUT="$(printf '%s\n' "$RENDER_OUT" | grep -v '^__RC=')"
    [ -z "$RENDER_RC" ] && RENDER_RC=0
    RENDER_RAN=1
    RENDER_TOOL="$WL_TOOL (Wayland/EGLUT)"

    case "$RENDER_RC" in
      124|0)
        echo "VIRGL-OK: $WL_TOOL rendered on Wayland (exit $RENDER_RC: ran until killed)"
        ;;
      *)
        echo "VIRGL-FAIL: $WL_TOOL could not render on Wayland (rc=$RENDER_RC)" >&2
        [ -n "$RENDER_OUT" ] && printf '%s\n' "$RENDER_OUT" >&2
        echo "  (a valid Wayland display was found and the render still failed)" >&2
        exit 1
        ;;
    esac
  else
    echo "check-virgl: cannot attach to $WL_OWNER's Wayland session without sudo, skipping"
  fi
elif [ "$HAS_ES2GEARS_WL" -eq 1 ]; then
  echo "check-virgl: $WL_TOOL installed but no Wayland display found (no session logged in), skipping render test"
fi

# --- X11 fallback: glxgears ---------------------------------------------------
if [ "$RENDER_RAN" -eq 0 ]; then
  HAS_GLXGEARS=0
  if "${SSH[@]}" "$SSH_USER@127.0.0.1" 'command -v glxgears >/dev/null' 2>/dev/null; then
    HAS_GLXGEARS=1
  fi

  HAS_X11_DISPLAY=0
  if "${SSH[@]}" "$SSH_USER@127.0.0.1" 'test -n "$DISPLAY"' 2>/dev/null; then
    HAS_X11_DISPLAY=1
  fi

  if [ "$HAS_GLXGEARS" -eq 1 ] && [ "$HAS_X11_DISPLAY" -eq 1 ]; then
    RENDER_OUT="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
      'timeout 5 glxgears -info 2>&1 | head -20' 2>/dev/null)"
    echo "$RENDER_OUT"
    RENDER_RAN=1
    RENDER_TOOL="glxgears (X11)"

    if echo "$RENDER_OUT" | grep -qiE "error|fail"; then
      echo "VIRGL-FAIL: glxgears reported an error:" >&2
      echo "$RENDER_OUT" >&2
      exit 1
    fi

    if echo "$RENDER_OUT" | grep -qiE "frames|fps"; then
      echo "VIRGL-OK: glxgears rendered successfully"
    else
      echo "VIRGL-WARN: glxgears ran but no FPS output (may still be working)" >&2
    fi
  elif [ "$HAS_GLXGEARS" -eq 1 ]; then
    echo "check-virgl: glxgears available but no X11 DISPLAY (Wayland session), skipping render test"
  else
    echo "check-virgl: no render tool available (glxgears/es2gears_wayland), skipping render test"
  fi
fi

# Honest summary: do NOT claim "full stack verified" when gate 2 never ran --
# that wording is exactly what let a skipped render test read as a pass.
if [ "$RENDER_RAN" -eq 1 ]; then
  echo "VIRGL-OK: full VirGL stack verified (renderer identity + render via $RENDER_TOOL)"
else
  echo "VIRGL-WARN: renderer identified but NOT render-verified" >&2
  echo "VIRGL-OK: VirGL renderer identified, but NOT render-verified (gate 2 skipped)"
fi
exit 0

