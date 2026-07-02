"""InfluxDB Publisher for Janitza UMG 512-PRO with change detection and custom measurements."""

import math
import re
import time
import threading
from collections import deque
from typing import Dict, Any, Optional, List

from .config import InfluxDBConfig, SelectedRegister

import logging
logger = logging.getLogger(__name__)

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_INITIAL_DELAY = 2
RETRY_MAX_DELAY = 60
RETRY_BACKOFF_FACTOR = 2
RECONNECT_CHECK_INTERVAL = 30


class InfluxDBPublisher:
    """
    InfluxDB Publisher for Janitza data.

    Features:
    - Custom measurement name per register
    - Custom tags per register
    - Two-phase cache: check before write, confirm after success
    - NaN/Infinity guard to protect InfluxDB batches
    - Proactive health checks via ping()
    - Batched writes with error/retry callbacks
    - Automatic reconnection (background — never blocks application boot)
    - Points are stamped with the Modbus poll time, not the flush time
    - Store-and-forward: points that cannot be delivered (InfluxDB down, batch
      retries exhausted) go to a bounded RAM buffer and are replayed with their
      original timestamps on reconnect. InfluxDB dedupes on (measurement, tags,
      timestamp), so replay is idempotent — no duplicates by construction.
    """

    def __init__(self, config: InfluxDBConfig, registers: List[SelectedRegister],
                 publish_mode: str = 'changed'):
        self.config = config
        self.registers = registers
        self.publish_mode = publish_mode

        self.client = None
        self.write_api = None
        self._connected = threading.Event()
        self.last_values: Dict[int, Dict] = {}
        self.last_write_time: Dict[int, float] = {}
        # lock guards the client/write_api REFERENCES only — it must never be
        # held across network I/O, or the Modbus poller threads (which take it
        # per point in the hot path) stall behind a slow reconnect, the live
        # cache goes stale and the virtual meters drop their consumers.
        self.lock = threading.Lock()
        # the change-detection cache gets its own lock so the hot path never
        # contends with client lifecycle at all
        self._cache_lock = threading.Lock()

        # Build register lookup by address
        self._register_map: Dict[int, SelectedRegister] = {
            r.address: r for r in registers if r.influxdb_enabled
        }

        # Stats
        self.writes_total = 0
        self.writes_failed = 0
        self.writes_skipped = 0
        self.disconnection_count = 0

        # Store-and-forward replay buffer: (poll_epoch_s, line_protocol) tuples,
        # bounded by age (buffer_minutes) and count (buffer_max_points).
        self._buffer: deque = deque()
        self._buf_lock = threading.Lock()
        self.points_buffered = 0
        self.points_replayed = 0
        self.points_dropped = 0

        # Reconnection thread
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            # Non-blocking startup: the monitor thread performs the first
            # connect (and every reconnect) in the background, so a down
            # InfluxDB can never stall application boot. Points produced
            # before the first connect land in the replay buffer.
            self._start_reconnect_thread()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @connected.setter
    def connected(self, value: bool):
        if value:
            self._connected.set()
        else:
            if self._connected.is_set():
                self.disconnection_count += 1
            self._connected.clear()

    def _on_write_error(self, conf, data, exception):
        """Callback when an InfluxDB batch write fails permanently (all client
        retries exhausted). The batch is NOT lost: its line-protocol payload is
        recovered into the replay buffer and re-delivered on reconnect."""
        self.writes_failed += 1
        recovered = self._rebuffer_batch(data)
        logger.error(f"InfluxDB batch failed permanently ({exception}) — "
                     f"recovered {recovered} points into the replay buffer")
        self._handle_write_error(exception)

    def _rebuffer_batch(self, data) -> int:
        """Recover a failed batch (bytes/str/list of line protocol) into the
        replay buffer, preserving each point's own timestamp. Never raises."""
        try:
            if isinstance(data, bytes):
                data = data.decode('utf-8', 'replace')
            if isinstance(data, str):
                lines = data.splitlines()
            elif isinstance(data, (list, tuple)):
                lines = [str(l) for l in data]
            else:
                lines = [str(data)]
        except Exception:  # noqa: BLE001
            return 0
        now = time.time()
        recovered = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                # trailing token of a line-protocol record is the ns timestamp
                ts = int(line.rsplit(' ', 1)[1]) / 1e9
            except Exception:  # noqa: BLE001
                ts = now
            self._buffer_line(line, ts)
            recovered += 1
        return recovered

    def _on_write_retry(self, conf, data, exception):
        """Callback when InfluxDB batch write is being retried."""
        logger.warning(f"InfluxDB write retry: {exception}")

    def _setup_client(self):
        """Setup InfluxDB client with proper batching and error callbacks.

        All network I/O (ping, bucket check) happens WITHOUT holding self.lock;
        only the final reference swap is locked. Holding the lock across a slow
        connect (DNS timeouts while the server is down take seconds per try)
        would stall every Modbus poller behind it — observed live as the
        virtual meters dropping their consumers mid-reconnect."""
        try:
            from influxdb_client import InfluxDBClient, WriteOptions

            new_client = InfluxDBClient(
                url=self.config.url,
                token=self.config.token,
                org=self.config.org,
                timeout=10_000,
            )

            # Test connection with ping() (replaces deprecated health())
            if not new_client.ping():
                logger.warning("InfluxDB ping failed")
                try:
                    new_client.close()
                except Exception:  # noqa: BLE001
                    pass
                return

            new_write_api = new_client.write_api(
                write_options=WriteOptions(
                    batch_size=100,
                    flush_interval=10_000,
                    jitter_interval=2_000,
                    retry_interval=5_000,
                    max_retries=10,
                    max_retry_time=300_000,
                    exponential_base=2,
                ),
                error_callback=self._on_write_error,
                success_callback=None,
                retry_callback=self._on_write_retry,
            )

            with self.lock:                       # swap references only
                old_write_api, old_client = self.write_api, self.client
                self.client, self.write_api = new_client, new_write_api

            self.connected = True
            logger.info(f"InfluxDB connected to {self.config.url}")
            self._ensure_bucket()                 # auto-create if missing

            # Retire old objects after the swap, outside the lock: closing a
            # batching write_api can block while it flushes/abandons retries
            # (its dead batches come back through _on_write_error → buffer).
            for obj in (old_write_api, old_client):
                if obj:
                    try:
                        obj.close()
                    except Exception:  # noqa: BLE001
                        pass

        except ImportError:
            logger.warning("influxdb-client not installed. Install with: pip install influxdb-client")
            self.config.enabled = False
        except Exception as e:
            logger.warning(f"InfluxDB connection failed: {e}")
            self.connected = False

    def _ensure_bucket(self):
        """Auto-create bucket if it doesn't exist."""
        try:
            buckets_api = self.client.buckets_api()
            bucket = buckets_api.find_bucket_by_name(self.config.bucket)
            if bucket:
                logger.info(f"InfluxDB bucket '{self.config.bucket}' exists")
                return
            # Create bucket with 90 day retention
            from influxdb_client import BucketRetentionRules
            retention = BucketRetentionRules(type="expire", every_seconds=90 * 86400)
            org = self.client.organizations_api().find_organizations(org=self.config.org)[0]
            buckets_api.create_bucket(
                bucket_name=self.config.bucket,
                retention_rules=retention,
                org_id=org.id
            )
            logger.info(f"InfluxDB bucket '{self.config.bucket}' created (90d retention)")
        except Exception as e:
            logger.warning(f"Bucket auto-create failed (non-fatal): {e}")

    # ── store-and-forward buffer ───────────────────────────────────────────
    def _buffer_line(self, line: str, ts: float) -> None:
        """Queue one line-protocol record for replay. Bounded: drop-oldest by
        age (buffer_minutes) and count (buffer_max_points)."""
        with self._buf_lock:
            self._buffer.append((ts, line))
            self.points_buffered += 1
            self._prune_buffer_locked()

    def _prune_buffer_locked(self) -> None:
        """Enforce buffer bounds. Caller holds _buf_lock."""
        cutoff = time.time() - getattr(self.config, 'buffer_minutes', 10) * 60
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
            self.points_dropped += 1
        overflow = len(self._buffer) - getattr(self.config, 'buffer_max_points', 50000)
        if overflow > 0:
            for _ in range(overflow):
                self._buffer.popleft()
            self.points_dropped += overflow

    def _drain_buffer(self) -> None:
        """Replay buffered points to InfluxDB in original order, in chunks,
        via a synchronous write (so success is known before points are let go
        of). A failed chunk goes back to the FRONT of the buffer and draining
        stops until the next monitor tick. Runs on the monitor thread."""
        with self._buf_lock:
            self._prune_buffer_locked()
            pending = len(self._buffer)
        if not pending:
            return
        with self.lock:
            client = self.client
        if client is None:
            return
        logger.info(f"InfluxDB replaying {pending} buffered points...")
        try:
            from influxdb_client.client.write_api import SYNCHRONOUS
            wapi = client.write_api(write_options=SYNCHRONOUS)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"InfluxDB replay unavailable: {e}")
            return
        try:
            while True:
                with self._buf_lock:
                    chunk = [self._buffer.popleft()
                             for _ in range(min(len(self._buffer), 5000))]
                if not chunk:
                    break
                try:
                    wapi.write(bucket=self.config.bucket,
                               record="\n".join(line for _, line in chunk))
                    self.points_replayed += len(chunk)
                except Exception as e:  # noqa: BLE001
                    with self._buf_lock:
                        self._buffer.extendleft(reversed(chunk))
                    logger.warning(f"InfluxDB replay failed ({e}) — will retry")
                    self._handle_write_error(e)
                    return
            logger.info(f"InfluxDB replay complete: {pending} points delivered")
        finally:
            try:
                wapi.close()
            except Exception:  # noqa: BLE001
                pass

    def _start_reconnect_thread(self):
        """Start background thread for reconnection and health monitoring."""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return

        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="InfluxDB-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        logger.info("InfluxDB monitor thread started")

    def _reconnect_loop(self):
        """
        Persistent background loop that monitors and reconnects to InfluxDB.

        When connected: performs periodic ping() health checks to detect
        disconnections faster than waiting for batch retry exhaustion (up to 5 min).
        When disconnected: attempts reconnection every RECONNECT_CHECK_INTERVAL.
        """
        while not self._stop_reconnect.is_set():
            if self.connected:
                # Proactive health check
                try:
                    with self.lock:
                        client = self.client
                    if not client or not client.ping():
                        logger.warning("InfluxDB ping failed")
                        self.connected = False
                except Exception as e:
                    logger.warning(f"InfluxDB health check failed: {e}")
                    self.connected = False
            else:
                logger.debug("Attempting InfluxDB reconnection...")
                self._setup_client()

                if self.connected:
                    logger.info("InfluxDB reconnected successfully")

            # Deliver anything the outage left behind (no-op when empty).
            if self.connected:
                try:
                    self._drain_buffer()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"InfluxDB buffer drain error: {e}")

            self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)

    def _handle_write_error(self, error: Exception):
        """Handle write errors and trigger reconnection if needed."""
        error_str = str(error).lower()
        connection_errors = [
            'connection refused', 'connection reset', 'connection closed',
            'no route to host', 'network is unreachable', 'timeout',
            'timed out', 'broken pipe', 'connection aborted',
        ]

        is_connection_error = any(err in error_str for err in connection_errors)

        if is_connection_error and self.connected:
            logger.warning("InfluxDB connection lost, monitor thread will reconnect")
            self.connected = False

    def is_enabled(self) -> bool:
        """Check if InfluxDB publishing is enabled and connected."""
        return self.config.enabled and self.connected

    def _safe_float(self, value) -> Optional[float]:
        """Convert to float, returning None for NaN/Infinity to protect InfluxDB batches."""
        try:
            val = float(value)
            if math.isfinite(val):
                return val
            logger.warning(f"Skipping non-finite value: {value}")
            return None
        except (ValueError, TypeError):
            return None

    def _should_write(self, address: int, value: Any) -> bool:
        """
        Check if value should be written based on mode and interval.
        Does NOT update cache — call _confirm_write() after successful write.
        """
        current_time = time.time()

        with self._cache_lock:
            # Rate limiting
            if address in self.last_write_time:
                elapsed = current_time - self.last_write_time[address]
                if elapsed < self.config.write_interval:
                    return False

            # Change detection
            if self.publish_mode == 'changed':
                if address in self.last_values:
                    if self.last_values[address] == value:
                        return False

            return True

    def _confirm_write(self, address: int, value: Any):
        """Update the change-detection cache once a point is on a guaranteed
        path: either enqueued to the batching client (whose permanent failures
        are recovered into the replay buffer via _on_write_error) or placed in
        the replay buffer directly."""
        with self._cache_lock:
            self.last_values[address] = value
            self.last_write_time[address] = time.time()

    def _get_measurement(self, register: SelectedRegister) -> str:
        """Get InfluxDB measurement name for a register."""
        if register.influxdb_measurement:
            return register.influxdb_measurement

        unit = register.unit.lower() if register.unit else ''
        if 'v' in unit and 'var' not in unit:
            return 'voltage'
        elif 'a' in unit and 'va' not in unit:
            return 'current'
        elif unit == 'w':
            return 'power_active'
        elif 'va' in unit and 'var' not in unit:
            return 'power_apparent'
        elif 'var' in unit:
            return 'power_reactive'
        elif 'wh' in unit:
            return 'energy_active'
        elif 'varh' in unit:
            return 'energy_reactive'
        elif 'hz' in unit:
            return 'frequency'
        elif '%' in unit:
            return 'percentage'
        else:
            return 'janitza'

    def _get_tags(self, register: SelectedRegister) -> Dict[str, str]:
        """Get InfluxDB tags for a register."""
        tags = {
            'device': 'janitza_umg512',
            'address': str(register.address),
            'name': register.name,
        }

        if register.influxdb_tags:
            tags.update(register.influxdb_tags)

        return tags

    def _build_point(self, register: SelectedRegister, safe_val: Any, ts: float,
                     poll_group: Optional[str] = None,
                     extra_tags: Dict[str, str] = None):
        """Build a Point stamped with the Modbus poll time (not the flush time),
        so batching latency never skews the series and buffered replay lands the
        point exactly where it was measured."""
        from influxdb_client import Point, WritePrecision

        point = Point(self._get_measurement(register))
        for tag_key, tag_value in self._get_tags(register).items():
            point = point.tag(tag_key, tag_value)
        if extra_tags:
            for tag_key, tag_value in extra_tags.items():
                point = point.tag(tag_key, tag_value)
        if poll_group:
            point = point.tag('poll_group', poll_group)

        field_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
        if isinstance(safe_val, (int, float)):
            point = point.field(field_name, float(safe_val))
            point = point.field('value', float(safe_val))
        else:
            point = point.field(field_name, str(safe_val))
        return point.time(int(ts * 1e9), WritePrecision.NS)

    def _deliver(self, point, ts: float) -> None:
        """Route one point: enqueue to the batching client when connected,
        otherwise (or on enqueue failure) into the replay buffer. Either path
        guarantees eventual delivery within the buffer bounds."""
        if self.connected and self.write_api:
            try:
                self.write_api.write(bucket=self.config.bucket, record=point)
                self.writes_total += 1
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"InfluxDB enqueue failed, buffering point: {e}")
                self._handle_write_error(e)
        self._buffer_line(point.to_line_protocol(), ts)

    def write_register_data(self, poll_group: str, data: Dict[int, Dict]):
        """Write register data from a poll group. Works whether or not InfluxDB
        is reachable — points produced during an outage go to the replay buffer."""
        if not self.config.enabled:
            return

        try:
            for address, item in data.items():
                register = item.get('register')
                value = item.get('value')

                if register is None or not register.influxdb_enabled:
                    continue

                if not self._should_write(address, value):
                    self.writes_skipped += 1
                    continue

                # Validate value
                if isinstance(value, (int, float)):
                    safe_val = self._safe_float(value)
                    if safe_val is None:
                        continue
                else:
                    safe_val = value

                ts = item.get('ts') or time.time()
                point = self._build_point(register, safe_val, ts, poll_group=poll_group)
                self._deliver(point, ts)
                self._confirm_write(address, value)

        except Exception as e:
            self.writes_failed += 1
            logger.error(f"InfluxDB write error: {e}")
            self._handle_write_error(e)

    def write_single(self, register: SelectedRegister, value: Any,
                     extra_tags: Dict[str, str] = None, ts: float = None):
        """Write a single register value."""
        if not self.config.enabled:
            return

        if not self._should_write(register.address, value):
            self.writes_skipped += 1
            return

        # Validate value
        if isinstance(value, (int, float)):
            safe_val = self._safe_float(value)
            if safe_val is None:
                return
        else:
            safe_val = value

        try:
            ts = ts or time.time()
            point = self._build_point(register, safe_val, ts, extra_tags=extra_tags)
            self._deliver(point, ts)
            self._confirm_write(register.address, value)

        except Exception as e:
            self.writes_failed += 1
            logger.error(f"InfluxDB write error: {e}")
            self._handle_write_error(e)

    def flush(self):
        """Flush pending writes."""
        if self.write_api:
            try:
                self.write_api.flush()
            except Exception as e:
                logger.error(f"InfluxDB flush error: {e}")

    def close(self):
        """Close InfluxDB connection. Best-effort final drain of the replay
        buffer (RAM-only — whatever cannot be delivered now is gone)."""
        self._stop_reconnect.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

        if self.connected:
            try:
                self._drain_buffer()
            except Exception:  # noqa: BLE001
                pass

        if self.write_api:
            try:
                self.write_api.close()
            except Exception:
                pass

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.connected = False
        logger.info("InfluxDB connection closed")

    def update_config(self, new_config: InfluxDBConfig):
        """Update InfluxDB configuration."""
        self.config = new_config
        self.publish_mode = new_config.publish_mode
        logger.info(f"InfluxDB config updated: {new_config.url}")

    def update_registers(self, registers: List[SelectedRegister]):
        """Update register list."""
        self.registers = registers
        self._register_map = {r.address: r for r in registers if r.influxdb_enabled}
        logger.info(f"InfluxDB registers updated: {len(self._register_map)} enabled")

    def reconnect(self) -> bool:
        """Reconnect to InfluxDB with current config (one immediate attempt;
        the monitor thread keeps retrying in the background either way)."""
        logger.info("InfluxDB reconnecting...")
        self.close()

        if not self.config.enabled:
            logger.info("InfluxDB disabled, not reconnecting")
            return False

        self._setup_client()
        self._start_reconnect_thread()

        if self.connected:
            logger.info("InfluxDB reconnected successfully")
            return True
        logger.warning("InfluxDB reconnection failed, monitor thread will keep trying")
        return False

    def get_stats(self) -> Dict:
        """Return publisher statistics."""
        return {
            'enabled': self.config.enabled,
            'connected': self.connected,
            'url': self.config.url,
            'bucket': self.config.bucket,
            'writes_total': self.writes_total,
            'writes_failed': self.writes_failed,
            'writes_skipped': self.writes_skipped,
            'publish_mode': self.publish_mode,
            'registered_addresses': len(self._register_map),
            'disconnection_count': self.disconnection_count,
            'buffer_points': len(self._buffer),
            'buffered_total': self.points_buffered,
            'replayed_total': self.points_replayed,
            'dropped_total': self.points_dropped,
            'buffer_minutes': getattr(self.config, 'buffer_minutes', 10),
        }

    # ── read-back for the UI history/trend view ───────────────────────────
    _EVERY_RE = re.compile(r"^\d+[smhd]$")
    _RANGE_RE = re.compile(r"^-\d+[smhdw]$")                     # relative: must be negative
    _RFC3339_RE = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.Z+-]*$")   # anchored, safe chars only
    _VALID_FN = {"mean", "min", "max", "last", "first"}

    def query_history(self, name: str, start: str = "-6h", stop: str = "now()",
                      every: str = "1m", fn: str = "mean",
                      measurement: Optional[str] = None) -> Dict:
        """Read aggregated history for a register (matched by its ``name`` tag)
        back from InfluxDB. Returns ``{name, every, fn, series:[{t,v}]}`` (UTC
        ISO timestamps), or ``{series_mean/min/max}`` when ``fn=='all'`` (for a
        min/max band), or ``{"error": ...}``. Inputs are validated/escaped because
        the register name is a user-supplied tag value flowing into Flux."""
        if not self.config.enabled:
            return {"error": "influxdb disabled"}
        safe_name = str(name).replace("\\", "").replace('"', "")
        if not safe_name:
            return {"error": "name required"}
        if not self._EVERY_RE.match(str(every)):
            return {"error": "every must look like 30s / 5m / 1h / 1d"}
        fns = ["mean", "min", "max"] if fn == "all" else [fn]
        for f in fns:
            if f not in self._VALID_FN:
                return {"error": f"fn must be one of {sorted(self._VALID_FN)} or 'all'"}

        def _tok(t, allow_now=True):
            t = str(t)
            if (allow_now and t == "now()") or self._RANGE_RE.match(t) or self._RFC3339_RE.match(t):
                return t
            return None
        s = _tok(start, allow_now=False)
        if s is None:
            return {"error": "bad start (use -6h / -7d / RFC3339)"}
        e = _tok(stop) or "now()"
        meas_filter = ""
        if measurement:
            sm = re.sub(r"[^A-Za-z0-9_]", "", str(measurement))   # whitelist — no Flux injection
            if sm:
                meas_filter = f'  |> filter(fn: (r) => r["_measurement"] == "{sm}")\n'

        # Always use a short-lived client WITH a timeout: avoids racing the write
        # client (closed/replaced under lock by the reconnect thread) and bounds
        # the query so a hung InfluxDB can't stall the API event loop.
        try:
            from influxdb_client import InfluxDBClient
            client = InfluxDBClient(url=self.config.url, token=self.config.token,
                                    org=self.config.org, timeout=10_000)
        except Exception as ex:  # noqa: BLE001
            return {"error": f"influxdb client unavailable: {ex}"}

        def _run(f):
            flux = (f'from(bucket: "{self.config.bucket}")\n'
                    f'  |> range(start: {s}, stop: {e})\n'
                    f'  |> filter(fn: (r) => r["name"] == "{safe_name}")\n'
                    f'  |> filter(fn: (r) => r["_field"] == "value")\n'
                    f'{meas_filter}'
                    f'  |> aggregateWindow(every: {every}, fn: {f}, createEmpty: false)\n'
                    f'  |> keep(columns: ["_time", "_value"])')
            out = []
            for table in client.query_api().query(flux, org=self.config.org):
                for rec in table.records:
                    v = rec.get_value()
                    out.append({"t": rec.get_time().isoformat().replace("+00:00", "Z"),
                                "v": round(v, 4) if isinstance(v, (int, float)) else v})
            return out

        try:
            if fn == "all":
                return {"name": safe_name, "every": every, "fn": "all",
                        "series_mean": _run("mean"), "series_min": _run("min"),
                        "series_max": _run("max")}
            return {"name": safe_name, "every": every, "fn": fn, "series": _run(fn)}
        except Exception as ex:  # noqa: BLE001
            return {"error": f"query failed: {ex}"}
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    _NAME_RE = re.compile(r"[^A-Za-z0-9_\[\]]")   # register names may contain [ ]

    def energy_report(self, year: int, month: int, regs: list,
                      tz: str = "Europe/Bucharest") -> Dict:
        """Energy used in a local calendar month: for each cumulative-counter
        register, the delta over the month (end-start) plus a per-day breakdown.
        ``regs`` = list of ``{name,label,unit,div}`` (div scales the raw unit, e.g.
        Wh→kWh with div=1000). Returns ``{year,month,totals:[...],daily:[...]}`` or
        ``{"error": ...}``."""
        if not self.config.enabled:
            return {"error": "influxdb disabled"}
        from datetime import datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
            loc, utc = ZoneInfo(tz), ZoneInfo("UTC")
        except Exception as ex:  # noqa: BLE001
            return {"error": f"timezone unavailable: {ex}"}
        try:
            y, m = int(year), int(month)
            if not (1 <= m <= 12):
                return {"error": "month must be 1..12"}
            start_l = datetime(y, m, 1, tzinfo=loc)
            end_l = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1, tzinfo=loc)
            s = start_l.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = end_l.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as ex:  # noqa: BLE001
            return {"error": f"bad month: {ex}"}
        try:
            from influxdb_client import InfluxDBClient
            client = InfluxDBClient(url=self.config.url, token=self.config.token,
                                    org=self.config.org, timeout=15_000)
        except Exception as ex:  # noqa: BLE001
            return {"error": f"influxdb client unavailable: {ex}"}

        def _q(flux):
            return [r for t in client.query_api().query(flux, org=self.config.org) for r in t.records]

        totals, daily = [], []
        try:
            for spec in regs:
                nm = self._NAME_RE.sub("", str(spec.get("name", "")))
                if not nm:
                    continue
                div = float(spec.get("div", 1)) or 1.0
                label, unit = spec.get("label", nm), spec.get("unit", "")
                base = (f'b = from(bucket:"{self.config.bucket}") |> range(start:{s}, stop:{e}) '
                        f'|> filter(fn:(r)=> r["name"]=="{nm}" and r["_field"]=="value")\n')
                # total = last - first over the month
                rows = _q(base + 'union(tables:[b|>first()|>set(key:"k",value:"f"), '
                                 'b|>last()|>set(key:"k",value:"l")]) |> keep(columns:["k","_value"])')
                vals = {r.values.get("k"): r.get_value() for r in rows}
                delta = None
                if vals.get("f") is not None and vals.get("l") is not None:
                    delta = round((vals["l"] - vals["f"]) / div, 3)
                totals.append({"name": spec.get("name"), "label": label, "unit": unit, "delta": delta})
                # per-day: cumulative value at each local day end, diffed in Python
                drows = _q('import "timezone"\noption location = timezone.location(name:"' + tz + '")\n'
                           + base + 'b |> aggregateWindow(every:1d, fn:last, createEmpty:false) '
                           '|> keep(columns:["_time","_value"])')
                pts = [(r.get_time().astimezone(loc), r.get_value()) for r in drows if r.get_value() is not None]
                days = []
                for i in range(1, len(pts)):
                    # value stamped at window stop (next local midnight) => belongs to the day before
                    day = (pts[i][0] - timedelta(seconds=1)).date().isoformat()
                    days.append({"date": day, "delta": round((pts[i][1] - pts[i - 1][1]) / div, 3)})
                daily.append({"name": spec.get("name"), "label": label, "unit": unit, "days": days})
        except Exception as ex:  # noqa: BLE001
            return {"error": f"query failed: {ex}"}
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return {"year": y, "month": m, "start": s, "stop": e, "totals": totals, "daily": daily}
