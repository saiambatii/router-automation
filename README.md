# Router Automation

Python toolkit for automating Cisco IOS/IOS-XE routers (any Netmiko-supported platform works). Built around the everyday network engineering loop: back up, change safely, verify.

## Features

- **backup**: save timestamped `running-config` files from many devices in parallel
- **show**: run any show commands across devices
- **push**: render per-device Jinja2 templates (OSPF/BGP baseline included); **dry-run by default**, auto-backup before `--apply`
- **check**: parse `show ip ospf neighbor` and `show ip bgp summary`, flag non-FULL OSPF and non-Established BGP sessions; exits non-zero so it can gate a CI/CD pipeline
- Credentials via environment variables only, never in the inventory
- Strict templates: missing variables fail instead of producing a bad config

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp inventory.example.yaml inventory.yaml   # edit with your devices
export NET_USER=admin NET_PASS='...'       # optional: NET_SECRET for enable
```

## Usage

```bash
python router_automation.py backup
python router_automation.py -g edge show "show ip route summary" "show version"
python router_automation.py -d edge-r1 push ospf_bgp.j2            # dry-run, prints config
python router_automation.py -d edge-r1 push ospf_bgp.j2 --apply    # backs up, pushes, saves
python router_automation.py check                                  # OSPF/BGP health
```

Filters: `-g GROUP`, `-d DEVICE` (repeatable), `-w WORKERS`, `-v` for debug logs.

## Tests

```bash
pytest -q
```

Unit tests cover parsers, inventory filtering, template rendering and the dry-run safety guarantee, with no devices needed.

## Ideas to extend

- Run `check` in a GitHub Actions job after config changes (non-zero exit fails the pipeline)
- Add post-change diffing against the latest backup
- Add more templates (ACLs, NTP, SNMP) under `templates/`
- Swap the inventory loader for NetBox or Nornir

Test against lab gear (EVE-NG, CML, or DevNet Sandbox) before production.
