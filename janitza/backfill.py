#!/usr/bin/env python3
"""Janitza UMG512-Pro → InfluxDB gap backfill.

Self-heals holes in the InfluxDB bucket by recovering data from the meter's
onboard 1-minute recording. The collector polls live values over Modbus and
publishes them; when its network path drops (e.g. a brief grid dip reboots the
cabinet switch), the live stream — and InfluxDB — gets a hole. But the meter is
powered by the grid it measures and keeps logging to its own flash throughout.
This job reads that flash via the meter's HTTP ``HIST_DATA`` API and writes the
missing points back, matching the live ``InfluxDBPublisher`` schema exactly
(measurement ``voltage``; fields ``uln_n``/``ull_n`` + ``value``; tags
``device/address/name/poll_group/phase`` + ``type``|``connection``) so charts
just fill in. A ``backfilled=1`` field marks recovered points for traceability.

Scope: L-N and L-L voltages at 1-minute resolution — the only parameters the
UMG512 records historically (current/power/frequency are live-only, never in
the recording, so a comms gap loses them permanently — backfill can't invent
them). Voltage is the power-quality record, so it is the right thing to recover.

Config comes from the same env the collector uses: ``INFLUXDB_URL``,
``INFLUXDB_TOKEN``, ``INFLUXDB_ORG``, ``INFLUXDB_BUCKET``, ``MODBUS_HOST`` (the
meter's HTTP API is on that host). Run it inside the collector container so all
of those — plus ``influxdb_client`` and the meter network path — are present.

Modes::

    python -m janitza.backfill                 # auto: detect trailing gap and heal it
    python -m janitza.backfill --window A B     # explicit UTC ISO window (past incident)
    python -m janitza.backfill --dry-run        # fetch + report, write nothing
    python -m janitza.backfill --verbose        # per-series logging

Idempotent: points land on exact minute boundaries keyed by
(measurement, tagset, field, timestamp); re-running overwrites identically.

Cron (host triggers the container every 10 min)::

    */10 * * * * docker exec pv-stack-janitza-monitor python -m janitza.backfill >> /var/log/janitza_backfill.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ── Config (env-overridable; same vars as the collector) ─────────────────────
METER_URL = os.environ.get("JANITZA_METER_URL") or f"http://{os.environ.get('MODBUS_HOST', '192.168.1.100')}"
INFLUX_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "janitza")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "janitza")
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "").strip()
MIN_GAP_SEC = int(os.environ.get("JANITZA_MIN_GAP_SEC", "180"))
MAX_LOOKBACK_H = int(os.environ.get("JANITZA_MAX_LOOKBACK_H", "48"))
TB = 60  # meter recording timebase (seconds) — 1-minute means

# (hist_param, address, register_name, field_name, extra_tags) — measurement is "voltage".
# Mirrors the live registers in config/selected_registers.json (L-N: type, L-L: connection).
PARAMS: list[tuple[str, int, str, str, dict[str, str]]] = [
    ("_ULN[0]", 19000, "_G_ULN[0]", "uln_0", {"phase": "L1", "type": "line_neutral"}),
    ("_ULN[1]", 19002, "_G_ULN[1]", "uln_1", {"phase": "L2", "type": "line_neutral"}),
    ("_ULN[2]", 19004, "_G_ULN[2]", "uln_2", {"phase": "L3", "type": "line_neutral"}),
    ("_ULL[0]", 19006, "_G_ULL[0]", "ull_0", {"phase": "L1", "connection": "line_line"}),
    ("_ULL[1]", 19008, "_G_ULL[1]", "ull_1", {"phase": "L2", "connection": "line_line"}),
    ("_ULL[2]", 19010, "_G_ULL[2]", "ull_2", {"phase": "L3", "connection": "line_line"}),
]


def _http_json(url: str, timeout: int = 12) -> dict:
    raw = urllib.request.urlopen(url, timeout=timeout).read().decode("latin1")
    return json.loads(raw)


def meter_tz_offset() -> int:
    """Device-local epoch = UTC epoch + offset. HIST_DATA timestamps are local."""
    d = _http_json(f"{METER_URL}/lib/get_time_info.html", timeout=8)
    return int(round(d["_SYSTIME"][0] - d["_UTCTIME"][0]))


def fetch_hist(param: str, start_utc: float, end_utc: float, tz: int) -> list[tuple[float, float]]:
    """Return [(value, utc_epoch), ...] from the meter recording for [start, end]."""
    start_local = int(start_utc) + tz
    cnt = int(math.ceil((end_utc - start_utc) / TB)) + 3
    qs = f"val$={param}&start={start_local}&cnt={cnt}&tb={TB}"
    arr = _http_json(f"{METER_URL}/hist_data/HIST_DATA.html?{qs}")[param][0]
    out: list[tuple[float, float]] = []
    for item in arr:
        if not item or len(item) < 2 or item[0] is None:
            continue
        val, local_ts = float(item[0]), float(item[1])
        utc = local_ts - tz
        if start_utc <= utc <= end_utc:
            out.append((val, utc))
    return out


def influx_latest_voltage_utc() -> float | None:
    """UTC epoch of the most recent live L1 L-N voltage point, or None."""
    from influxdb_client import InfluxDBClient

    flux = (
        f'from(bucket:"{INFLUX_BUCKET}") |> range(start:-{MAX_LOOKBACK_H}h) '
        '|> filter(fn:(r)=>r._measurement=="voltage" and r._field=="value" '
        'and r.phase=="L1" and r.type=="line_neutral") |> last() |> keep(columns:["_time"])'
    )
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as c:
        for table in c.query_api().query(flux):
            for rec in table.records:
                return rec.get_time().timestamp()
    return None


def backfill(start_utc: float, end_utc: float, dry: bool, verbose: bool) -> int:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS

    tz = meter_tz_offset()
    written = 0
    client = None if dry else InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        wapi = None if dry else client.write_api(write_options=SYNCHRONOUS)
        for param, addr, name, field, extra in PARAMS:
            pts = fetch_hist(param, start_utc, end_utc, tz)
            if verbose or dry:
                span = (f"{datetime.fromtimestamp(pts[0][1], timezone.utc):%H:%M}"
                        f"–{datetime.fromtimestamp(pts[-1][1], timezone.utc):%H:%M}") if pts else "—"
                print(f"  {param:9} {name:11} -> {len(pts):3} pts  {span}")
            if dry:
                continue
            for val, utc in pts:
                p = (
                    Point("voltage")
                    .tag("device", "janitza_umg512")
                    .tag("address", str(addr))
                    .tag("name", name)
                    .tag("poll_group", "realtime")
                )
                for k, v in extra.items():
                    p = p.tag(k, v)
                p = (p.field(field, float(val))
                       .field("value", float(val))
                       .field("backfilled", 1)
                       .time(int(utc), WritePrecision.S))
                wapi.write(bucket=INFLUX_BUCKET, record=p)
                written += 1
        if wapi:
            wapi.close()
    finally:
        if client:
            client.close()
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill janitza InfluxDB gaps from the meter recording")
    ap.add_argument("--window", nargs=2, metavar=("START", "STOP"),
                    help="explicit UTC ISO window, e.g. 2026-06-03T11:18 2026-06-03T15:22")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not INFLUX_TOKEN:
        print("ERROR: INFLUXDB_TOKEN not set", file=sys.stderr)
        return 2

    def iso(s: str) -> float:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()

    if args.window:
        start_utc, end_utc = iso(args.window[0]), iso(args.window[1])
        print(f"[manual] window {args.window[0]} .. {args.window[1]} UTC")
    else:
        now = time.time()
        latest = influx_latest_voltage_utc()
        if latest is not None:
            gap = now - latest
            if gap <= MIN_GAP_SEC:
                print(f"[auto] no gap (last point {gap:.0f}s ago ≤ {MIN_GAP_SEC}s) — nothing to do")
                return 0
            print(f"[auto] gap detected: {gap / 60:.1f} min since last point "
                  f"({datetime.fromtimestamp(latest, timezone.utc):%Y-%m-%d %H:%M} UTC)")
            start_utc = max(latest - 120, now - MAX_LOOKBACK_H * 3600)
        else:
            print(f"[auto] no recent data in {MAX_LOOKBACK_H}h — backfilling full lookback")
            start_utc = now - MAX_LOOKBACK_H * 3600
        end_utc = now

    n = backfill(start_utc, end_utc, args.dry_run, args.verbose)
    print(f"{'[dry-run] would write' if args.dry_run else 'wrote'} {n} points across {len(PARAMS)} series")
    return 0


if __name__ == "__main__":
    sys.exit(main())
