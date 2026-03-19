"""InfluxDB Publisher for Janitza UMG 512-PRO with change detection and custom measurements."""

import math
import time
import threading
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
    - Automatic reconnection
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
        self.lock = threading.Lock()

        # Build register lookup by address
        self._register_map: Dict[int, SelectedRegister] = {
            r.address: r for r in registers if r.influxdb_enabled
        }

        # Stats
        self.writes_total = 0
        self.writes_failed = 0
        self.writes_skipped = 0
        self.disconnection_count = 0

        # Reconnection thread
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            self._setup_client_with_retry()
            # Always start persistent monitor thread for proactive health checks
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
        """Callback when InfluxDB batch write fails permanently (all retries exhausted)."""
        logger.error(f"InfluxDB data lost permanently: {exception}")
        self.writes_failed += 1
        self._handle_write_error(exception)

    def _on_write_retry(self, conf, data, exception):
        """Callback when InfluxDB batch write is being retried."""
        logger.warning(f"InfluxDB write retry: {exception}")

    def _setup_client(self):
        """Setup InfluxDB client with proper batching and error callbacks."""
        with self.lock:
            try:
                from influxdb_client import InfluxDBClient, WriteOptions

                # Clean up old connections first
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

                self.client = InfluxDBClient(
                    url=self.config.url,
                    token=self.config.token,
                    org=self.config.org
                )

                self.write_api = self.client.write_api(
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

                # Test connection with ping() (replaces deprecated health())
                if self.client.ping():
                    self.connected = True
                    logger.info(f"InfluxDB connected to {self.config.url}")
                else:
                    logger.warning("InfluxDB ping failed")

            except ImportError:
                logger.warning("influxdb-client not installed. Install with: pip install influxdb-client")
                self.config.enabled = False
            except Exception as e:
                logger.warning(f"InfluxDB connection failed: {e}")
                self.connected = False

    def _setup_client_with_retry(self):
        """Setup InfluxDB client with retry logic."""
        delay = RETRY_INITIAL_DELAY

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            self._setup_client()

            if self.connected:
                return

            if attempt < RETRY_MAX_ATTEMPTS:
                logger.info(f"InfluxDB connection attempt {attempt}/{RETRY_MAX_ATTEMPTS} failed, retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

        logger.warning(f"InfluxDB: all {RETRY_MAX_ATTEMPTS} connection attempts failed. Will continue trying in background.")

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
                    if not client:
                        self.connected = False
                        self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)
                        continue
                    if not client.ping():
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

        with self.lock:
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
        """Update cache after successful write. Prevents data loss on transient failures."""
        with self.lock:
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

    def write_register_data(self, poll_group: str, data: Dict[int, Dict]):
        """Write register data from a poll group."""
        if not self.is_enabled():
            return

        try:
            from influxdb_client import Point

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

                # Create point
                measurement = self._get_measurement(register)
                point = Point(measurement)

                for tag_key, tag_value in self._get_tags(register).items():
                    point = point.tag(tag_key, tag_value)

                point = point.tag('poll_group', poll_group)

                field_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
                if isinstance(safe_val, (int, float)):
                    point = point.field(field_name, float(safe_val))
                    point = point.field('value', float(safe_val))
                else:
                    point = point.field(field_name, str(safe_val))

                self.write_api.write(bucket=self.config.bucket, record=point)
                self.writes_total += 1

                # Confirm write — update cache only after successful enqueue
                self._confirm_write(address, value)

        except Exception as e:
            self.writes_failed += 1
            logger.error(f"InfluxDB write error: {e}")
            self._handle_write_error(e)

    def write_single(self, register: SelectedRegister, value: Any,
                     extra_tags: Dict[str, str] = None):
        """Write a single register value."""
        if not self.is_enabled():
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
            from influxdb_client import Point

            measurement = self._get_measurement(register)
            point = Point(measurement)

            for tag_key, tag_value in self._get_tags(register).items():
                point = point.tag(tag_key, tag_value)

            if extra_tags:
                for tag_key, tag_value in extra_tags.items():
                    point = point.tag(tag_key, tag_value)

            field_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
            if isinstance(safe_val, (int, float)):
                point = point.field(field_name, float(safe_val))
                point = point.field('value', float(safe_val))
            else:
                point = point.field(field_name, str(safe_val))

            self.write_api.write(bucket=self.config.bucket, record=point)
            self.writes_total += 1

            # Confirm write
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
        """Close InfluxDB connection."""
        self._stop_reconnect.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

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
        """Reconnect to InfluxDB with current config."""
        logger.info("InfluxDB reconnecting...")
        self.close()

        if not self.config.enabled:
            logger.info("InfluxDB disabled, not reconnecting")
            return False

        self._setup_client_with_retry()

        if self.connected:
            # Restart monitor thread
            self._start_reconnect_thread()
            logger.info("InfluxDB reconnected successfully")
            return True
        else:
            self._start_reconnect_thread()
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
        }
