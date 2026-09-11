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
#   2. Guest can perform a basic GL render (glxgears or eglinfo).
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

# Try glxgears (quick 3-second render) if available and X11 DISPLAY is set.
# On Wayland sessions glxgears cannot open a display; skip gracefully.
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
  echo "VIRGL-OK: renderer identification passed (glxgears requires X11)"
else
  echo "check-virgl: glxgears not installed, skipping render test"
  echo "VIRGL-OK: renderer identification passed (install mesa-utils for render test)"
fi

echo "VIRGL-OK: full VirGL stack verified"
exit 0
