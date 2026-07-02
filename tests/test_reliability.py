"""Reliability guarantees: InfluxDB store-and-forward buffer, poll-time
timestamps, virtual-meter TCP keepalive, MQTT reconnect ownership.

These tests exercise the no-data-loss paths end to end in-process:
disconnect → buffer → replay (idempotent, original timestamps), failed-batch
recovery, buffer bounds, and the connection-hygiene fixes.
"""
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from janitza.config import InfluxDBConfig, MQTTConfig, SelectedRegister
from janitza.influxdb_publisher import InfluxDBPublisher
from janitza.mqtt_publisher import MQTTPublisher
from janitza.modbus_client import RegisterPoller
from janitza.register_parser import RegisterParser
from janitza.virtual_meter import Template, VirtualMeter


def make_register(address=19000, name="_G_ULN[0]", data_type="float"):
    return SelectedRegister(
        address=address, name=name, label="L1 voltage", unit="V",
        data_type=data_type, poll_group="realtime",
    )


def make_publisher(**cfg_overrides):
    """Publisher with no background thread (enabled=False at init), then
    enabled for the write path so tests control connectivity explicitly."""
    cfg = InfluxDBConfig(enabled=False, url="http://x:8086", token="t",
                         org="o", bucket="b", write_interval=0, **cfg_overrides)
    pub = InfluxDBPublisher(cfg, [make_register()], publish_mode="changed")
    pub.config.enabled = True
    return pub


# ── poll-time timestamps ─────────────────────────────────────────────────────

def test_point_is_stamped_with_poll_time():
    pub = make_publisher()
    ts = 1_700_000_000.123456
    line = pub._build_point(make_register(), 231.7, ts,
                            poll_group="realtime").to_line_protocol()
    assert line.endswith(str(int(ts * 1e9)))


def test_poller_attaches_read_timestamp():
    reg = make_register()
    connection = MagicMock()
    connection.read_registers.return_value = [0x3F80, 0x0000]   # float 1.0
    poller = RegisterPoller("realtime", 1, [reg], connection,
                            RegisterParser(), publish_callback=lambda *a: None)
    before = time.time()
    results = poller._poll_registers()
    item = results[reg.address]
    assert item["value"] == pytest.approx(1.0)
    assert before <= item["ts"] <= time.time()


# ── store-and-forward buffer ─────────────────────────────────────────────────

def test_disconnected_write_goes_to_buffer_not_lost():
    pub = make_publisher()
    assert not pub.connected
    ts = time.time()
    pub.write_register_data("realtime", {
        19000: {"value": 231.7, "register": make_register(), "ts": ts},
    })
    assert len(pub._buffer) == 1
    buf_ts, line = pub._buffer[0]
    assert buf_ts == ts
    assert "231.7" in line and line.endswith(str(int(ts * 1e9)))
    # cache confirmed → the same value is not re-buffered next poll
    pub.write_register_data("realtime", {
        19000: {"value": 231.7, "register": make_register(), "ts": ts + 1},
    })
    assert len(pub._buffer) == 1


def test_rebuffer_batch_recovers_lines_with_their_timestamps():
    pub = make_publisher()
    now_ns = int(time.time() * 1e9)
    ns1, ns2 = now_ns - 60_000_000_000, now_ns   # 60s ago + now (inside bounds)
    batch = (f"voltage,name=a value=1.0 {ns1}\n"
             f"voltage,name=b value=2.0 {ns2}\n").encode()
    assert pub._rebuffer_batch(batch) == 2
    assert [t for t, _ in pub._buffer] == [ns1 / 1e9, ns2 / 1e9]


def test_buffer_bounds_age_and_count():
    pub = make_publisher(buffer_minutes=1, buffer_max_points=3)
    old = time.time() - 120
    pub._buffer_line("old 1", old)
    for i in range(4):
        pub._buffer_line(f"fresh {i}", time.time())
    lines = [l for _, l in pub._buffer]
    assert "old 1" not in lines            # age-pruned
    assert len(lines) == 3                 # count-capped (drop-oldest)
    assert lines[-1] == "fresh 3"
    assert pub.points_dropped >= 2


def test_drain_replays_in_order_then_clears():
    pub = make_publisher()
    for i in range(3):
        pub._buffer_line(f"line {i}", time.time())
    wapi = MagicMock()
    client = MagicMock()
    client.write_api.return_value = wapi
    pub.client = client
    pub.connected = True
    pub._drain_buffer()
    assert wapi.write.call_count == 1
    assert wapi.write.call_args.kwargs["record"] == "line 0\nline 1\nline 2"
    assert len(pub._buffer) == 0
    assert pub.points_replayed == 3


def test_drain_failure_requeues_in_original_order():
    pub = make_publisher()
    for i in range(3):
        pub._buffer_line(f"line {i}", time.time())
    wapi = MagicMock()
    wapi.write.side_effect = ConnectionError("boom")
    client = MagicMock()
    client.write_api.return_value = wapi
    pub.client = client
    pub.connected = True
    pub._drain_buffer()
    assert [l for _, l in pub._buffer] == ["line 0", "line 1", "line 2"]
    assert pub.points_replayed == 0


def test_write_path_never_blocks_on_client_lock():
    """Regression: while the monitor thread reconnects (slow DNS/ping), it may
    hold pub.lock. The Modbus pollers write through this publisher in their hot
    path — if that path ever takes pub.lock, polling stalls, the live cache
    goes stale and the virtual meters drop their consumers. Observed live
    during an InfluxDB restart; must never happen again."""
    pub = make_publisher()
    done = threading.Event()

    def poller_write():
        pub.write_register_data("realtime", {
            19000: {"value": 231.7, "register": make_register(), "ts": time.time()},
        })
        done.set()

    with pub.lock:                       # monitor thread mid-reconnect
        t = threading.Thread(target=poller_write, daemon=True)
        t.start()
        assert done.wait(timeout=2), "write path blocked on client lock"
    assert len(pub._buffer) == 1         # point still safely buffered


def test_enqueue_failure_falls_back_to_buffer():
    pub = make_publisher()
    pub.connected = True
    pub.write_api = MagicMock()
    pub.write_api.write.side_effect = OSError("socket closed")
    ts = time.time()
    pub._deliver(pub._build_point(make_register(), 5.0, ts), ts)
    assert len(pub._buffer) == 1


# ── virtual meter TCP keepalive ──────────────────────────────────────────────

def make_vmeter():
    t = Template(id="t", name="t", transport={"port": 1502})
    return VirtualMeter(t, lambda n: None)


def tcp_socketpair():
    """Real TCP pair (AF_UNIX from socketpair() rejects TCP_KEEP* options)."""
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    a = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    a.connect(lst.getsockname())
    b, _ = lst.accept()
    lst.close()
    return a, b


def test_keepalive_applied_to_active_connections():
    vm = make_vmeter()
    a, b = tcp_socketpair()
    try:
        transport = MagicMock()
        transport.get_extra_info.return_value = a
        handler = SimpleNamespace(transport=transport)
        vm._server = SimpleNamespace(active_connections={"c1": handler})
        vm._apply_keepalive()
        assert a.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert a.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 60
            assert a.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            assert a.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
    finally:
        a.close()
        b.close()


def test_keepalive_survives_closed_socket_and_no_server():
    vm = make_vmeter()
    vm._server = None
    vm._apply_keepalive()                                  # no-op, no raise
    a, b = tcp_socketpair()
    a.close()
    b.close()
    transport = MagicMock()
    transport.get_extra_info.return_value = a
    vm._server = SimpleNamespace(
        active_connections={"c1": SimpleNamespace(transport=transport)})
    vm._apply_keepalive()                                  # closed FD, no raise


# ── MQTT reconnect ownership ─────────────────────────────────────────────────

def test_on_disconnect_records_state_and_spawns_no_thread():
    pub = MQTTPublisher(MQTTConfig(enabled=False), [], publish_mode="changed")
    pub.connected = True
    pub._on_disconnect(None, None, None, reason_code=7)
    assert not pub.connected
    assert pub.last_disconnect_ts is not None
    assert pub._reconnect_thread is None                   # paho owns recovery
    stats = pub.get_stats()
    assert stats["disconnected_for_s"] is not None
    assert stats["messages_failed"] == 0
