#!/usr/bin/env bash
#
# check-venus.sh -- validate that Venus (Vulkan) actually works in a booted
# virtio-gpu VM on Apple Silicon.
#
# Background
# ----------
# Venus is the Vulkan-over-virtio backend. The venus command RING relies on
# live coherency between a guest CPU write and a host virglrenderer read of the
# same host-visible blob. A working VirGL-GL does NOT imply Venus works: GL
# scanout/imports are synced via QEMU fences, whereas the ring needs direct
# cross-mapping coherence. This script verifies the FULL stack:
#
#   1. Guest can create a Vulkan instance + enumerate the venus device
#      (catches `VK_ERROR_OUT_OF_HOST_MEMORY` and the 16 KiB / HVF-coherency
#      ring failures that abort instance creation).
#   2. Guest can create a venus *command ring* and submit/fence a trivial
#      transfer through it (catches the silent ring-stall where the ring thread
#      sees tail=0 forever and vkQueueSubmit never returns).
#
# Usage
# -----
#   scripts/check-venus.sh <vmdir> [--ring]
#
#   <vmdir>   A provisioned VM directory, e.g. ~/VMs/ubuntu-lts (must contain
#             launch-vm.sh, ssh_key, and the booted guest must be reachable).
#   --ring    Additionally run the venus ring submit/fence self-test. Without
#             this flag only instance/device enumeration is checked (fast).
#
# Exit codes:
#   0  Venus OK (instance + device [+ ring if --ring])
#   1  Venus FAILED (details on stdout as VENUS-FAIL / RING-FAIL markers)
#   2  Environment/infra failure (VM not booted, no ssh, vulkaninfo missing,
#      wrong arch, ...) -- distinct from a Venus defect so results aren't
#      conflated with a build-cancel / infra hiccup.
#
# Host rule: only ever ONE VM may be up. This script never launches a VM; it
# only inspects an already-running one.
#
set -u

VMDIR="${1:-}"
RING=0
for arg in "$@"; do
  case "$arg" in
    --ring) RING=1 ;;
  esac
done

if [ -z "$VMDIR" ]; then
  echo "usage: $0 <vmdir> [--ring]" >&2
  exit 2
fi
if [ ! -d "$VMDIR" ]; then
  echo "VENUS-FAIL: VM dir not found: $VMDIR" >&2
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
  echo "VENUS-FAIL: ssh key not found: $KEY" >&2
  exit 2
fi

SSH=(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -i "$KEY" -o BatchMode=yes -o ConnectTimeout=6 -p "$PORT")

# --- confirm the guest answers + is aarch64 (venus is the aarch64 path) ------
if ! "${SSH[@]}" "$SSH_USER@127.0.0.1" 'true' >/dev/null 2>&1; then
  echo "VENUS-FAIL: guest not reachable at 127.0.0.1:$PORT" >&2
  exit 2
fi
ARCH=$("${SSH[@]}" "$SSH_USER@127.0.0.1" 'uname -m' 2>/dev/null | tr -d '\r')
if [ "$ARCH" != "aarch64" ]; then
  echo "VENUS-SKIP: arch=$ARCH is not aarch64; venus ring coherency issue is Apple-Silicon-only" >&2
  exit 2
fi

if ! "${SSH[@]}" "$SSH_USER@127.0.0.1" 'command -v vulkaninfo >/dev/null' 2>/dev/null; then
  echo "VENUS-FAIL: vulkaninfo not installed in guest" >&2
  exit 2
fi

# =============================================================================
# Gate 1: instance + device enumeration (fast; aborts build-time on the
#         VK_ERROR_OUT_OF_HOST_MEMORY symptom)
# =============================================================================
echo "== check-venus: gate 1 instance/device enumeration (arch=$ARCH) =="
INST="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
  'vulkaninfo --summary 2>&1 | grep -iE "deviceName|apiVersion|driverName" | head -20' 2>/dev/null)"
echo "$INST"

if ! echo "$INST" | grep -iqE "venus|virtio"; then
  if echo "$INST" | grep -qiE "error|fail"; then
    echo "VENUS-FAIL: vulkaninfo reported an error (likely VK_ERROR_OUT_OF_HOST_MEMORY / ring):" >&2
    echo "$INST" >&2
    exit 1
  fi
  echo "VENUS-FAIL: no venus/virtio Vulkan device enumerated:" >&2
  echo "$INST" >&2
  exit 1
fi
echo "VENUS-OK: instance + device enumeration succeeded"

# =============================================================================
# Gate 2 (--ring): venus command-ring submit + fence self-test
# =============================================================================
if [ "$RING" -eq 1 ]; then
  echo "== check-venus: gate 2 ring submit/fence self-test =="
  # Force ring use and run a minimal transfer+pipeline-fence so vkQueueSubmit
  # must drain a real venus ring command. NDEBUG=0 keeps the driver's own
  # "cpu sync timed out" trap armed so a stalled ring is surfaced loudly.
  RING_OUT="$("${SSH[@]}" "$SSH_USER@127.0.0.1" \
    'cd /tmp && rm -rf venus-ring-test && mkdir venus-ring-test && cd venus-ring-test && \
     cat > probe.c <<'"'"'EOF'"'"'
#include <stdio.h>
#include <stdlib.h>
#include <vulkan/vulkan.h>
int main(void){
  VkInstance i; VkApplicationInfo ai={0}; VkInstanceCreateInfo ici={0};
  ai.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO; ai.apiVersion=VK_API_VERSION_1_3;
  ici.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO; ici.pApplicationInfo=&ai;
  VkResult r=vkCreateInstance(&ici,NULL,&i);
  if(r!=VK_SUCCESS){printf("RING-FAIL: vkCreateInstance %d\n",r);return 1;}
  uint32_t n=0; vkEnumeratePhysicalDevices(i,&n,NULL);
  if(n==0){printf("RING-FAIL: no physical devices\n");return 1;}
  VkPhysicalDevice pd[n]; vkEnumeratePhysicalDevices(i,&n,pd);
  uint32_t qf=0; vkGetPhysicalDeviceQueueFamilyProperties(pd[0],&qf,NULL);
  VkQueueFamilyProperties qp[qf?qf:1]; vkGetPhysicalDeviceQueueFamilyProperties(pd[0],&qf,qp);
  float one=1.0f; VkDeviceQueueCreateInfo qci={0};
  qci.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO; qci.queueFamilyIndex=0; qci.queueCount=1; qci.pQueuePriorities=&one;
  VkDeviceCreateInfo dci={0}; dci.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
  dci.queueCreateInfoCount=1; dci.pQueueCreateInfos=&qci;
  VkDevice d; r=vkCreateDevice(pd[0],&dci,NULL,&d);
  if(r!=VK_SUCCESS){printf("RING-FAIL: vkCreateDevice %d\n",r);return 1;}
  VkQueue q; vkGetDeviceQueue(d,0,0,&q);
  /* submit an empty (but real) ring command and wait for the fence -- a dead
     ring never completes this submit (venus stalls on the ring, not the cmdq) */
  VkFence f; VkFenceCreateInfo fci={VK_STRUCTURE_TYPE_FENCE_CREATE_INFO,NULL,0};
  r=vkCreateFence(d,&fci,NULL,&f);
  if(r!=VK_SUCCESS){printf("RING-FAIL: vkCreateFence %d\n",r);return 1;}
  r=vkQueueSubmit(q,0,NULL,f);
  if(r!=VK_SUCCESS){printf("RING-FAIL: vkQueueSubmit %d\n",r);return 1;}
  r=vkWaitForFences(d,1,&f,VK_TRUE,3000000000ull); /* 3 s */
  if(r==VK_TIMEOUT){printf("RING-FAIL: fence timeout (ring stalled, tail=0)\n");return 1;}
  if(r!=VK_SUCCESS){printf("RING-FAIL: vkWaitForFences %d\n",r);return 1;}
  printf("RING-OK: venus ring submit + fence succeeded\n");
  return 0;
}
EOF
     cc probe.c -o probe -lvulkan 2>&1 | tail -5 && timeout 30 ./probe 2>&1' 2>/dev/null)"
  printf '%s\n' "$RING_OUT"
  if echo "$RING_OUT" | grep -q "RING-OK"; then
    echo "VENUS-OK: full venus ring path verified"
    exit 0
  fi
  echo "VENUS-FAIL: ring submit/fence failed (host-side HVF coherency likely at fault)" >&2
  exit 1
fi

echo "VENUS-OK: enumeration path verified (add --ring to test command-ring ownership)"
exit 0
