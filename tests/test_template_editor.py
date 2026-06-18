"""Template editor: manager get/save/delete + validation + YAML round-trip.

Uses a temp templates dir so it never touches the shipped templates. Verifies
that a template authored via the editor API reloads losslessly through
load_template (the engine's own parser) — the guarantee the UI depends on.
"""
import yaml

from janitza.virtual_meter import load_template
from janitza.virtual_meter_manager import VirtualMeterManager


def _mgr(tmp_path):
    cur = {
        "1": {"name": "_G_P_SUM3", "label": "Total power", "unit": "W", "value": -1234.0,
              "timestamp": "2026-06-17T22:00:00"},
        "2": {"name": "_G_FREQ", "label": "Frequency", "unit": "Hz", "value": 50.0,
              "timestamp": "2026-06-17T22:00:00"},
    }
    return VirtualMeterManager(cur, config_path=str(tmp_path / "vm.yaml"),
                               templates_dir=str(tmp_path / "templates"))


def test_save_and_get_roundtrip(tmp_path):
    mgr = _mgr(tmp_path)
    payload = {
        "id": "my_meter", "name": "My Meter", "byte_order": "little",
        "port": 1700, "unit_id": 2, "bind": "0.0.0.0",
        "registers": [
            {"addr": "0x000b", "type": "uint16", "source_kind": "const", "source": 1651,
             "scale": 1, "length": 1, "note": "model"},
            {"addr": "0x0028", "type": "int32", "source_kind": "live", "source": "_G_P_SUM3",
             "scale": 10, "length": 1, "note": "Total power"},
            {"addr": "0x5000", "type": "string", "source_kind": "const_str", "source": "JANITZA",
             "scale": 1, "length": 7, "note": ""},
        ],
    }
    res = mgr.save_template("my_meter", payload)
    assert res.get("saved") and res["registers"] == 3

    # editor view comes back intact
    got = mgr.get_template("my_meter")
    assert got["byte_order"] == "little"
    assert got["transport"]["port"] == 1700 and got["transport"]["unit_id"] == 2
    by_addr = {r["addr"]: r for r in got["registers"]}
    assert by_addr[0x000b]["source_kind"] == "const" and by_addr[0x000b]["source"] == 1651
    assert by_addr[0x0028]["source_kind"] == "live" and by_addr[0x0028]["source"] == "_G_P_SUM3"
    assert by_addr[0x0028]["scale"] == 10 and by_addr[0x0028]["note"] == "Total power"
    assert by_addr[0x5000]["source_kind"] == "const_str" and by_addr[0x5000]["source"] == "JANITZA"

    # the engine's own parser loads it (the contract the meter relies on)
    t = load_template(str(tmp_path / "templates" / "my_meter.yaml"))
    assert t.id == "my_meter" and len(t.registers) == 3
    p = next(r for r in t.registers if r.addr == 0x0028)
    assert p.source_kind == "live" and p.source == "_G_P_SUM3" and p.scale == 10
    assert p.note == "Total power"


def test_validation_rejects_overlap(tmp_path):
    mgr = _mgr(tmp_path)
    res = mgr.save_template("bad", {
        "id": "bad", "name": "Bad", "byte_order": "big", "port": 1700,
        "registers": [
            {"addr": "0x0000", "type": "int32", "source_kind": "const", "source": 1},  # 0x0000-0x0001
            {"addr": "0x0001", "type": "uint16", "source_kind": "const", "source": 2},  # overlaps
        ],
    })
    assert "error" in res and "overlap" in res["error"]


def test_validation_rejects_bad_id_and_type(tmp_path):
    mgr = _mgr(tmp_path)
    assert "error" in mgr.save_template("Bad-Id", {"id": "Bad-Id", "name": "x",
                                                   "registers": [{"addr": 0, "type": "int16",
                                                                  "source_kind": "const", "source": 1}]})
    assert "error" in mgr.save_template("ok", {"id": "ok", "name": "x",
                                               "registers": [{"addr": 0, "type": "nope",
                                                              "source_kind": "const", "source": 1}]})
    # path traversal is blocked
    assert mgr._template_path("../etc/passwd") is None


def test_live_source_requires_name(tmp_path):
    mgr = _mgr(tmp_path)
    res = mgr.save_template("lm", {"id": "lm", "name": "x", "port": 1700,
                                   "registers": [{"addr": 0, "type": "int32",
                                                  "source_kind": "live", "source": ""}]})
    assert "error" in res and "live source" in res["error"]


def test_delete_template(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.save_template("gone", {"id": "gone", "name": "Gone", "port": 1700,
                               "registers": [{"addr": 0, "type": "int16",
                                              "source_kind": "const", "source": 1}]})
    assert mgr.get_template("gone").get("id") == "gone"
    assert mgr.delete_template("gone").get("deleted")
    assert "error" in mgr.get_template("gone")


def test_list_sources(tmp_path):
    mgr = _mgr(tmp_path)
    names = [s["name"] for s in mgr.list_sources()]
    assert "_G_P_SUM3" in names and "_G_FREQ" in names


def test_port_range_info_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("VMETER_PORT_START", "1502")
    monkeypatch.setenv("VMETER_PORT_END", "1504")
    mgr = _mgr(tmp_path)
    mgr.save_template("t", {"id": "t", "name": "T", "port": 1502,
                            "registers": [{"addr": 0, "type": "int16",
                                           "source_kind": "const", "source": 1}]})
    info = mgr.port_info()
    assert info["start"] == 1502 and info["end"] == 1504 and info["next_free"] == 1502

    # out-of-range port is rejected
    assert "error" in mgr.add_instance("t", port=9999)
    # in-range succeeds and is then "used"
    assert mgr.add_instance("t", port=1503).get("added")
    assert mgr.port_info()["used"] == [1503] and mgr.port_info()["next_free"] == 1502

    # same port twice (different template) → rejected
    mgr.save_template("u", {"id": "u", "name": "U", "port": 1502,
                            "registers": [{"addr": 0, "type": "int16",
                                           "source_kind": "const", "source": 1}]})
    assert "error" in mgr.add_instance("u", port=1503)
