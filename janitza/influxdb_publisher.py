"""InfluxDB Publisher for Janitza UMG 512-PRO with change detection and custom measurements."""

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
    - Publish-on-change mode
    - Rate limiting per measurement
    - Batched writes
    - Automatic reconnection
    """

    def __init__(self, config: InfluxDBConfig, registers: List[SelectedRegister],
                 publish_mode: str = 'changed'):
        """
        Initialize InfluxDB publisher.

        Args:
            config: InfluxDB configuration
            registers: List of selected registers with InfluxDB configuration
            publish_mode: 'changed' or 'all'
        """
        self.config = config
        self.registers = registers
        self.publish_mode = publish_mode

        self.client = None
        self.write_api = None
        self.connected = False
        self.last_values: Dict[int, Any] = {}
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

        # Reconnection thread
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            self._setup_client_with_retry()
            if not self.connected:
                self._start_reconnect_thread()

    def _setup_client(self):
        """Setup InfluxDB client."""
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

            self.write_api = self.client.write_api(write_options=WriteOptions(
                batch_size=100,
                flush_interval=10_000,
                jitter_interval=2_000,
                retry_interval=5_000,
                max_retries=3
            ))

            # Test connection
            health = self.client.health()
            if health.status == "pass":
                self.connected = True
                logger.info(f"InfluxDB connected to {self.config.url}")
            else:
                logger.warning(f"InfluxDB health check failed: {health.message}")

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

        logger.warning(f"InfluxDB: all {RETRY_MAX_ATTEMPTS} connection attempts failed. Will retry in background.")

    def _start_reconnect_thread(self):
        """Start background reconnection thread."""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return

        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="InfluxDB-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        logger.info("InfluxDB reconnection thread started")

    def _reconnect_loop(self):
        """Background reconnection loop."""
        while not self._stop_reconnect.is_set():
            if not self.connected:
                logger.debug("Attempting InfluxDB reconnection...")
                self._setup_client()

                if self.connected:
                    logger.info("InfluxDB reconnected successfully")
                    break

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
            logger.warning("InfluxDB connection lost, starting reconnection...")
            self.connected = False
            self._start_reconnect_thread()

    def is_enabled(self) -> bool:
        """Check if InfluxDB publishing is enabled and connected."""
        return self.config.enabled and self.connected

    def _should_write(self, address: int, value: Any) -> bool:
        """Check if value should be written based on mode and interval."""
        current_time = time.time()

        # Rate limiting
        if address in self.last_write_time:
            elapsed = current_time - self.last_write_time[address]
            if elapsed < self.config.write_interval:
                return False

        # Change detection
        if self.publish_mode == 'changed':
            with self.lock:
                if address in self.last_values:
                    if self.last_values[address] == value:
                        return False
                self.last_values[address] = value

        self.last_write_time[address] = current_time
        return True

    def _get_measurement(self, register: SelectedRegister) -> str:
        """Get InfluxDB measurement name for a register."""
        if register.influxdb_measurement:
            return register.influxdb_measurement

        # Default: derive from unit or name
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

        # Add custom tags from configuration
        if register.influxdb_tags:
            tags.update(register.influxdb_tags)

        return tags

    def write_register_data(self, poll_group: str, data: Dict[int, Dict]):
        """
        Write register data from a poll group.

        Args:
            poll_group: Name of the poll group
            data: Dict mapping address -> {'value': ..., 'register': SelectedRegister}
        """
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

                # Create point
                measurement = self._get_measurement(register)
                point = Point(measurement)

                # Add tags
                for tag_key, tag_value in self._get_tags(register).items():
                    point = point.tag(tag_key, tag_value)

                # Add poll group as tag
                point = point.tag('poll_group', poll_group)

                # Add value as field
                field_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
                if isinstance(value, (int, float)):
                    point = point.field(field_name, float(value))
                else:
                    point = point.field(field_name, str(value))

                # Also add as 'value' field for simpler queries
                if isinstance(value, (int, float)):
                    point = point.field('value', float(value))

                self.write_api.write(bucket=self.config.bucket, record=point)
                self.writes_total += 1

        except Exception as e:
            self.writes_failed += 1
            logger.error(f"InfluxDB write error: {e}")
            self._handle_write_error(e)

    def write_single(self, register: SelectedRegister, value: Any,
                     extra_tags: Dict[str, str] = None):
        """
        Write a single register value.

        Args:
            register: Register configuration
            value: Value to write
            extra_tags: Additional tags to add
        """
        if not self.is_enabled():
            return

        if not self._should_write(register.address, value):
            self.writes_skipped += 1
            return

        try:
            from influxdb_client import Point

            measurement = self._get_measurement(register)
            point = Point(measurement)

            # Add tags
            for tag_key, tag_value in self._get_tags(register).items():
                point = point.tag(tag_key, tag_value)

            if extra_tags:
                for tag_key, tag_value in extra_tags.items():
                    point = point.tag(tag_key, tag_value)

            # Add value
            field_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
            if isinstance(value, (int, float)):
                point = point.field(field_name, float(value))
                point = point.field('value', float(value))
            else:
                point = point.field(field_name, str(value))

            self.write_api.write(bucket=self.config.bucket, record=point)
            self.writes_total += 1

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
        """
        Reconnect to InfluxDB with current config.
        """
        logger.info("InfluxDB reconnecting...")

        # Close current connection
        self.close()

        # If disabled, don't reconnect
        if not self.config.enabled:
            logger.info("InfluxDB disabled, not reconnecting")
            return False

        # Reconnect with retry
        self._setup_client_with_retry()

        if self.connected:
            logger.info("InfluxDB reconnected successfully")
            return True
        else:
            logger.warning("InfluxDB reconnection failed, starting background retry")
            self._start_reconnect_thread()
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
        }
