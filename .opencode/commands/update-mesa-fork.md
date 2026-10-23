---
description: Pull the latest miles.buckton mesa fork in the VMs, rebuild mesa, and validate with check-virgl and check-venus
---

Load and follow the `update-mesa-fork` skill at `.opencode/skills/update-mesa-fork/SKILL.md` exactly as written.

The skill file is the single source of truth for this workflow. In particular, do not improvise anything it pins down:

- Its hard rules — one VM at a time, `bash` (never `python3`) for `scripts/check-*.sh`, both checks must exit 0 on each distro before moving on, and the Mesa version string must show the new git hash.
- Its per-distro meson configuration, which mirrors `templates/ubuntu.j2` and `templates/gentoo.j2` respectively.
- Its order: **ubuntu-lts first, then gentoo**, each finished (build + both checks) and powered off before the next boots.

If anything in this message conflicts with the skill, the skill wins. Report the Mesa version string and both check verdicts per distro when done.
