"""Jinja2 template rendering and NoCloud seed ISO generation."""
from __future__ import annotations

import io
from pathlib import Path

from .log import log


def get_jinja_env(templates_dir: Path):
    """Build the shared Jinja2 environment.

    CRITICAL: the settings here MUST be the single source of truth. The
    lint/audit scripts use this same factory so a render there is
    byte-identical to what setup_vm.py produces at runtime (drift between
    these cost us the simulate_install block once).
    """
    import jinja2

    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )


def render_jinja2_template(templates_dir: Path, template_name: str, ctx: dict) -> str:
    """Render any Jinja2 template (.j2) under templates/ with the shared
    inheritance/include search path."""
    template = get_jinja_env(templates_dir).get_template(template_name)
    return template.render(**ctx)


def parse_cloud_config(rendered: str):
    """Strip the `#cloud-config` header and YAML-parse a rendered user-data.

    Raises on YAML errors (caller decides how to surface them); returns the
    parsed object (typically a dict, possibly None for empty input).
    """
    import yaml

    body = rendered.lstrip()
    if body.startswith("#cloud-config"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    return yaml.safe_load(body)


def build_seed_iso(seed_path: Path, user_data: str, meta_data: str, templates_dir: Path) -> None:
    """Build a NoCloud seed ISO with volume label 'cidata'.

    The ISO contains three files at its root:
    * **user-data** -- distro-specific cloud-init config
    * **meta-data** -- minimal NoCloud meta-data (instance-id, hostname)
    * **network-config** -- cloud-init "Network Config Version 2" (netplan schema)
    """
    import pycdlib

    log("Building NoCloud seed ISO ...", "step")
    if seed_path.exists():
        seed_path.unlink()

    iso = pycdlib.PyCdlib()
    iso.new(
        interchange_level=3,
        joliet=3,
        rock_ridge="1.09",
        vol_ident="cidata",
    )

    def _add(name: str, data: str) -> None:
        b = data.encode("utf-8")
        iso.add_fp(
            io.BytesIO(b),
            len(b),
            iso_path=f"/{name.upper().replace('-', '_')}.;1",
            rr_name=name,
            joliet_path=f"/{name}",
        )

    network_config = render_jinja2_template(templates_dir, 'network-config.j2', {})

    _add("user-data", user_data)
    _add("meta-data", meta_data)
    _add("network-config", network_config)

    iso.write(str(seed_path))
    iso.close()
    log(f"Seed ISO: {seed_path}", "ok")
