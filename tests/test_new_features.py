"""Unit tests for the features added in v2.4.0:

- ModbusClient.data_health()      — acquisition-health classification (ok/degraded/down)
- VirtualMeterManager.update_instance() — partial edit + validation + persistence
- InfluxDBPublisher.query_history() — input validation / Flux-injection guards

All pure-logic (no network, no threads): instances are constructed and their
fields set directly, and only validation paths that return BEFORE any client is
touched are exercised for the InfluxDB read helper.
"""
import time

from janitza.config import ModbusConfig, InfluxDBConfig
from janitza.modbus_client import ModbusClient
from janitza.influxdb_publisher import InfluxDBPublisher
from janitza.virtual_meter_manager import VirtualMeterManager


# ── data_health ──────────────────────────────────────────────────────────
class _Poller:
    def __init__(self, interval, running=True, last=None, count=1, name="realtime"):
        self.interval = interval
        self.running = running
        self.last_poll_time = last
        self.poll_count = count
        self.poll_group_name = name


def _mc(pollers, connected=True, last_success=None, last_failure=None, registers=(1,)):
    mc = ModbusClient(ModbusConfig(), registers=list(registers), poll_groups={})
    mc.pollers = pollers
    mc.connected = connected
    mc.connection.last_success_ts = last_success
    mc.connection.last_failure_ts = last_failure
    return mc


def test_data_health_fresh_is_ok():
    h = _mc([_Poller(0.25)], last_success=time.time()).data_health(30)
    assert h["status"] == "ok" and h["stale"] is False


def test_data_health_degraded_then_down():
    now = time.time()
    assert _mc([_Poller(0.25)], last_success=now - 20).data_health(30)["status"] == "degraded"
    assert _mc([_Poller(0.25)], last_success=now - 40).data_health(30)["status"] == "down"


def test_data_health_cold_start_is_ok_until_a_failure():
    assert _mc([_Poller(0.25)], last_success=None, last_failure=None).data_health(30)["status"] == "ok"
    assert _mc([_Poller(0.25)], last_success=None, last_failure=1.0).data_health(30)["status"] == "down"


def test_data_health_disconnected_is_degraded():
    assert _mc([_Poller(0.25)], connected=False, last_success=time.time()).data_health(30)["status"] == "degraded"


def test_data_health_no_registers_is_ok():
    mc = _mc([_Poller(0.25)], registers=[], last_success=None)
    assert mc.data_health(30)["status"] == "ok"


def test_data_health_slow_only_group_no_false_positive():
    # only a 60s poller => effective threshold raised (>=3x interval), so 100s
    # is NOT 'down' (would be with a flat 30s threshold), but 200s is.
    now = time.time()
    assert _mc([_Poller(60)], last_success=now - 100).data_health(30)["status"] != "down"
    assert _mc([_Poller(60)], last_success=now - 220).data_health(30)["status"] == "down"


# ── update_instance ──────────────────────────────────────────────────────
def _mgr(tmp_path):
    cur = {"1": {"name": "_X", "label": "x", "unit": "W", "value": 1.0,
                 "timestamp": "2026-06-17T22:00:00"}}
    return VirtualMeterManager(cur, config_path=str(tmp_path / "vm.yaml"),
                               templates_dir=str(tmp_path / "templates"))


def _template(mgr, tid, port):
    mgr.save_template(tid, {"id": tid, "name": tid.upper(), "byte_order": "big",
                            "port": port, "unit_id": 1, "bind": "0.0.0.0",
                            "registers": [{"addr": "0x0000", "type": "int16",
                                           "source_kind": "const", "source": 1,
                                           "scale": 1, "length": 1, "note": ""}]})


def _mgr_with_instance(tmp_path):
    mgr = _mgr(tmp_path)
    _template(mgr, "m1", 1502)
    mgr.add_instance("m1", port=1502, unit_id=1, enabled=False)
    return mgr


def test_update_instance_persists_disabled(tmp_path):
    mgr = _mgr_with_instance(tmp_path)
    res = mgr.update_instance("m1", stale_after_s=20, update_interval_s=0.25)
    assert res.get("updated") and res["restarted"] is False
    assert res["stale_after_s"] == 20.0 and res["update_interval_s"] == 0.25
    inst = next(i for i in mgr._load_cfg()["instances"] if i["template"] == "m1")
    assert inst["stale_after_s"] == 20.0 and inst["update_interval_s"] == 0.25


def test_update_instance_port_range_and_uniqueness(tmp_path):
    mgr = _mgr_with_instance(tmp_path)
    assert "error" in mgr.update_instance("m1", port=9999)             # out of published range
    _template(mgr, "m2", 1503)
    mgr.add_instance("m2", port=1503, unit_id=1, enabled=False)
    r = mgr.update_instance("m1", port=1503)                            # collides with m2
    assert "error" in r and "already used" in r["error"]
    r = mgr.update_instance("m1", port=1504)                            # free, in range
    assert r.get("updated") and r["port"] == 1504


def test_update_instance_validation(tmp_path):
    mgr = _mgr_with_instance(tmp_path)
    assert "error" in mgr.update_instance("nope", unit_id=1)            # unknown instance
    assert "error" in mgr.update_instance("m1", unit_id=999)            # unit out of range
    assert "error" in mgr.update_instance("m1", stale_after_s=0)        # must be > 0
    assert "error" in mgr.update_instance("m1", update_interval_s=-1)   # must be > 0


# ── query_history input validation ───────────────────────────────────────
def _pub(enabled=True):
    cfg = InfluxDBConfig()
    cfg.enabled = False                       # construct WITHOUT a connection thread
    pub = InfluxDBPublisher(cfg, registers=[])
    pub.config.enabled = enabled              # flip on after init (no connect)
    return pub


def test_query_history_disabled():
    assert "error" in _pub(enabled=False).query_history("_TEMPERATUR")


def test_query_history_rejects_bad_fn():
    assert "fn must be" in _pub().query_history("_TEMPERATUR", fn="bogus")["error"]


def test_query_history_rejects_bad_every():
    assert "every must" in _pub().query_history("_TEMPERATUR", every="5x")["error"]


def test_query_history_rejects_empty_name():
    assert "name required" in _pub().query_history("")["error"]


def test_query_history_rejects_bad_start():
    assert "bad start" in _pub().query_history("_TEMPERATUR", start="garbage; drop")["error"]
