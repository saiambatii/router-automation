import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import router_automation as ra  # noqa: E402

OSPF_OUTPUT = """\
Neighbor ID     Pri   State           Dead Time   Address         Interface
10.255.0.2        1   FULL/DR         00:00:36    10.10.0.2       GigabitEthernet0/0
10.255.0.3        1   EXSTART/BDR     00:00:33    10.10.1.2       GigabitEthernet0/1
"""

BGP_OUTPUT = """\
BGP router identifier 10.255.0.1, local AS number 65001
Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
198.51.100.1    4        65100    1234    1230       42    0    0 2d03h           150
198.51.100.9    4        65300       0       0        1    0    0 never    Idle
"""


def test_parse_ospf_neighbors():
    n = ra.parse_ospf_neighbors(OSPF_OUTPUT)
    assert [x["state"] for x in n] == ["FULL", "EXSTART"]
    assert n[0]["iface"] == "GigabitEthernet0/0"


def test_parse_bgp_summary():
    n = ra.parse_bgp_summary(BGP_OUTPUT)
    assert len(n) == 2
    assert n[0]["established"] is True
    assert n[1]["established"] is False and n[1]["state"] == "Idle"


def test_load_inventory_filters(tmp_path):
    inv = tmp_path / "inv.yaml"
    inv.write_text(
        "devices:\n"
        "  - {name: a, host: 1.1.1.1, groups: [x]}\n"
        "  - {name: b, host: 2.2.2.2, groups: [y]}\n"
    )
    assert [d.name for d in ra.load_inventory(str(inv), group="x")] == ["a"]
    assert [d.name for d in ra.load_inventory(str(inv), names=["b"])] == ["b"]
    with pytest.raises(SystemExit):
        ra.load_inventory(str(inv), group="nope")


def test_render_template():
    dev = ra.Device(
        name="r1",
        host="1.1.1.1",
        vars={
            "router_id": "10.0.0.1",
            "ospf": {
                "process_id": 1,
                "networks": [{"prefix": "10.0.0.0", "wildcard": "0.0.0.255", "area": 0}],
            },
            "bgp": {
                "asn": 65001,
                "neighbors": [{"ip": "192.0.2.1", "remote_as": 65100, "description": "ISP"}],
            },
        },
    )
    cfg = ra.render_config("ospf_bgp.j2", dev)
    assert "router ospf 1" in cfg
    assert "network 10.0.0.0 0.0.0.255 area 0" in cfg
    assert "neighbor 192.0.2.1 remote-as 65100" in cfg


def test_render_fails_on_missing_var():
    dev = ra.Device(name="r1", host="1.1.1.1", vars={})
    with pytest.raises(Exception):
        ra.render_config("ospf_bgp.j2", dev)


def test_dry_run_does_not_connect(monkeypatch, tmp_path):
    def boom(_):
        raise AssertionError("dry-run must not open a connection")

    monkeypatch.setattr(ra, "connect", boom)
    dev = ra.Device(
        name="r1",
        host="1.1.1.1",
        vars={
            "router_id": "10.0.0.1",
            "ospf": {"process_id": 1, "networks": []},
            "bgp": {"asn": 1, "neighbors": []},
        },
    )
    out = ra.push_device(dev, "ospf_bgp.j2", apply=False, backup_dir=tmp_path)
    assert "router ospf 1" in out
