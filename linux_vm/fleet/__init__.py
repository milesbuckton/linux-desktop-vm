"""Sequential VM build orchestrator.

Package of focused modules:
  constants     -- timeouts, mirror map, usernames, DISTROS
  executor      -- run_with_hard_timeout, run_to_file
  ssh           -- check_guest_dns, wait_ssh_reachable, ssh_wait_cloud_init,
                   _kill_ssh_child_processes, _check_success_marker, _capture_diagnostics
  lifecycle     -- kill_pid, preflight_cleanup, shutdown_and_verify
  orchestrator  -- build_and_provision, prefetch_images, simulate_distro, simulate_all
  main          -- main (entry point)
"""
from __future__ import annotations
from .main import main

if __name__ == "__main__":
    main()
