#!/usr/bin/env python3
"""Router automation toolkit (Cisco IOS/IOS-XE focus, works with any Netmiko platform).

Subcommands:
  backup   Save running-config from every device to a timestamped file.
  show     Run one or more show commands on devices and save/print the output.
  push     Render a Jinja2 template per device and push it (dry-run by default).
  check    Health-check OSPF neighbors and BGP sessions, exit non-zero on problems.

Credentials come from environment variables (NET_USER, NET_PASS, optional NET_SECRET),
never from the inventory file.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

log = logging.getLogger("router_automation")

TEMPLATE_DIR = Path(__file__).parent / "templates"


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
@dataclass
class Device:
    name: str
    host: str
    device_type: str = "cisco_ios"
    port: int = 22
    vars: dict = field(default_factory=dict)

    def connection_params(self) -> dict:
        user = os.environ.get("NET_USER")
        password = os.environ.get("NET_PASS")
        if not user or not password:
            raise RuntimeError("Set NET_USER and NET_PASS environment variables.")
        params = {
            "device_type": self.device_type,
            "host": self.host,
            "port": self.port,
            "username": user,
            "password": password,
        }
        if os.environ.get("NET_SECRET"):
            params["secret"] = os.environ["NET_SECRET"]
        return params


def load_inventory(path: str, group: str | None = None, names: list[str] | None = None) -> list[Device]:
    """Load devices from YAML. Optionally filter by group or explicit device names."""
    data = yaml.safe_load(Path(path).read_text())
    defaults = data.get("defaults", {})
    devices: list[Device] = []
    for entry in data.get("devices", []):
        merged = {**defaults, **entry}
        if group and group not in merged.get("groups", []):
            continue
        if names and merged["name"] not in names:
            continue
        devices.append(
            Device(
                name=merged["name"],
                host=merged["host"],
                device_type=merged.get("device_type", "cisco_ios"),
                port=merged.get("port", 22),
                vars=merged.get("vars", {}),
            )
        )
    if not devices:
        raise SystemExit("No devices matched the inventory filters.")
    return devices


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
def connect(device: Device):
    # Imported lazily so parsing/templating can be unit tested without netmiko.
    from netmiko import ConnectHandler

    conn = ConnectHandler(**device.connection_params())
    if device.connection_params().get("secret"):
        conn.enable()
    return conn


def run_parallel(devices: list[Device], func, workers: int = 10) -> dict[str, object]:
    """Run func(device) concurrently. Returns {device_name: result_or_Exception}."""
    results: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(func, d): d for d in devices}
        for fut in as_completed(futures):
            dev = futures[fut]
            try:
                results[dev.name] = fut.result()
            except Exception as exc:  # keep going if one device fails
                log.error("%s: %s", dev.name, exc)
                results[dev.name] = exc
    return results


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def backup_device(device: Device, out_dir: Path) -> Path:
    with connect(device) as conn:
        config = conn.send_command("show running-config")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{device.name}_{stamp}.cfg"
    path.write_text(config)
    log.info("%s: backup saved to %s", device.name, path)
    return path


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
def show_device(device: Device, commands: list[str]) -> dict[str, str]:
    with connect(device) as conn:
        return {cmd: conn.send_command(cmd) for cmd in commands}


# --------------------------------------------------------------------------- #
# push (templated config)
# --------------------------------------------------------------------------- #
def render_config(template_name: str, device: Device) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,  # fail loudly on missing variables
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_name)
    return template.render(device=device, **device.vars)


def push_device(device: Device, template_name: str, apply: bool, backup_dir: Path) -> str:
    config = render_config(template_name, device)
    lines = [ln for ln in config.splitlines() if ln.strip() and not ln.strip().startswith("!")]
    if not apply:
        log.info("%s: dry-run, %d config lines rendered", device.name, len(lines))
        return config

    # Safety net: back up before any change.
    backup_device(device, backup_dir)
    with connect(device) as conn:
        output = conn.send_config_set(lines)
        conn.save_config()
    log.info("%s: config applied and saved", device.name)
    return output


# --------------------------------------------------------------------------- #
# check (OSPF / BGP health)
# --------------------------------------------------------------------------- #
OSPF_NEIGHBOR_RE = re.compile(
    r"^(?P<id>\d+\.\d+\.\d+\.\d+)\s+\S+\s+(?P<state>[A-Z0-9]+)/\S*\s+\S+\s+(?P<addr>\d+\.\d+\.\d+\.\d+)\s+(?P<iface>\S+)",
    re.MULTILINE,
)
# BGP summary neighbor rows end with either a prefix count (Established) or a state word.
BGP_NEIGHBOR_RE = re.compile(
    r"^(?P<neighbor>\d+\.\d+\.\d+\.\d+)\s+\d+\s+(?P<asn>\d+)\s+.*?\s+(?P<last>\S+)\s+(?P<state>\S+)\s*$",
    re.MULTILINE,
)


def parse_ospf_neighbors(output: str) -> list[dict]:
    return [m.groupdict() for m in OSPF_NEIGHBOR_RE.finditer(output)]


def parse_bgp_summary(output: str) -> list[dict]:
    neighbors = []
    for m in BGP_NEIGHBOR_RE.finditer(output):
        row = m.groupdict()
        # A numeric last column means prefixes received, i.e. session is Established.
        row["established"] = row["state"].isdigit()
        neighbors.append(row)
    return neighbors


def check_device(device: Device) -> dict:
    with connect(device) as conn:
        ospf_out = conn.send_command("show ip ospf neighbor")
        bgp_out = conn.send_command("show ip bgp summary")
    ospf = parse_ospf_neighbors(ospf_out)
    bgp = parse_bgp_summary(bgp_out)
    problems = []
    for n in ospf:
        if n["state"] != "FULL":
            problems.append(f"OSPF neighbor {n['id']} on {n['iface']} is {n['state']}")
    for n in bgp:
        if not n["established"]:
            problems.append(f"BGP neighbor {n['neighbor']} (AS {n['asn']}) is {n['state']}")
    expected_ospf = device.vars.get("expected_ospf_neighbors")
    if expected_ospf is not None and len(ospf) < expected_ospf:
        problems.append(f"Expected {expected_ospf} OSPF neighbors, found {len(ospf)}")
    return {"ospf": ospf, "bgp": bgp, "problems": problems}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Router automation toolkit")
    p.add_argument("-i", "--inventory", default="inventory.yaml", help="inventory YAML file")
    p.add_argument("-g", "--group", help="only devices in this group")
    p.add_argument("-d", "--device", action="append", help="only this device (repeatable)")
    p.add_argument("-w", "--workers", type=int, default=10, help="parallel connections")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="back up running-configs")
    b.add_argument("--out", default="backups")

    s = sub.add_parser("show", help="run show commands")
    s.add_argument("commands", nargs="+", help='e.g. "show ip route" "show version"')

    c = sub.add_parser("push", help="render and push a Jinja2 template")
    c.add_argument("template", help="template file in templates/, e.g. ospf_bgp.j2")
    c.add_argument("--apply", action="store_true", help="actually push (default is dry-run)")
    c.add_argument("--out", default="backups", help="pre-change backup directory")

    sub.add_parser("check", help="OSPF/BGP health check")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    devices = load_inventory(args.inventory, args.group, args.device)
    failed = False

    if args.command == "backup":
        out = Path(args.out)
        results = run_parallel(devices, lambda d: backup_device(d, out), args.workers)
        failed = any(isinstance(r, Exception) for r in results.values())

    elif args.command == "show":
        results = run_parallel(devices, lambda d: show_device(d, args.commands), args.workers)
        for name, res in sorted(results.items()):
            if isinstance(res, Exception):
                failed = True
                continue
            for cmd, text in res.items():
                print(f"\n===== {name} | {cmd} =====\n{text}")

    elif args.command == "push":
        out = Path(args.out)
        results = run_parallel(
            devices, lambda d: push_device(d, args.template, args.apply, out), args.workers
        )
        for name, res in sorted(results.items()):
            if isinstance(res, Exception):
                failed = True
            elif not args.apply:
                print(f"\n===== {name} (dry-run) =====\n{res}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to push.")

    elif args.command == "check":
        results = run_parallel(devices, check_device, args.workers)
        for name, res in sorted(results.items()):
            if isinstance(res, Exception):
                failed = True
                print(f"[FAIL] {name}: {res}")
            elif res["problems"]:
                failed = True
                print(f"[FAIL] {name}")
                for prob in res["problems"]:
                    print(f"       - {prob}")
            else:
                print(f"[ OK ] {name}: {len(res['ospf'])} OSPF, {len(res['bgp'])} BGP neighbors healthy")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
