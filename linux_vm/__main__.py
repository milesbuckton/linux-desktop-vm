"""Entry point for `python -m linux_vm`."""
from __future__ import annotations
import subprocess
import sys

from .host import reconfigure_stdout_utf8
from .log import log
from .monitor import monitor_main
from .orchestrate import main


def entry() -> None:
    try:
        if len(sys.argv) >= 2 and sys.argv[1] == "monitor":
            reconfigure_stdout_utf8()
            sys.exit(monitor_main(sys.argv[2:]))
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        log("Interrupted.", "warn")
        sys.exit(130)
    except subprocess.CalledProcessError as e:
        log(f"Command failed: {e}", "err")
        sys.exit(e.returncode or 1)
    except Exception as e:
        log(f"Unhandled error: {type(e).__name__}: {e}", "err")
        raise


if __name__ == "__main__":
    entry()
