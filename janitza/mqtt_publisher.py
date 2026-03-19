"""MQTT Publisher for Janitza UMG 512-PRO with change detection and custom topics."""

import math
import time
import json
import threading
from typing import Dict, Any, Optional, List, Union

import paho.mqtt.client as mqtt

from .config import MQTTConfig, SelectedRegister

import logging
logger = logging.getLogger(__name__)

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_INITIAL_DELAY = 2
RETRY_MAX_DELAY = 60
RETRY_BACKOFF_FACTOR = 2
RECONNECT_CHECK_INTERVAL = 30

# Home Assistant device classes
HA_DEVICE_CLASSES = {
    'V': 'voltage',
    'A': 'current',
    'W': 'power',
    'VA': 'apparent_power',
    'var': 'reactive_power',
    'Wh': 'energy',
    'kWh': 'energy',
    'Hz': 'frequency',
    '%': None,
    '°C': 'temperature',
}

HA_STATE_CLASSES = {
    'Wh': 'total_increasing',
    'kWh': 'total_increasing',
    'varh': 'total_increasing',
    'VAh': 'total_increasing',
}


class MQTTPublisher:
    """
    MQTT Publisher for Janitza data.

    Features:
    - Custom topic per register from configuration
    - Two-phase cache: check before publish, confirm after success
    - NaN guard to prevent phantom republishes
    - Cache clear on reconnect to force re-publish current state
    - Home Assistant MQTT autodiscovery
    - Automatic reconnection
    """

    def __init__(self, config: MQTTConfig, registers: List[SelectedRegister],
                 publish_mode: str = 'changed'):
        self.config = config
        self.registers = registers
        self.publish_mode = publish_mode

        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.last_values: Dict[str, Any] = {}
        self.lock = threading.Lock()

        # Build register lookup by address
        self._register_map: Dict[int, SelectedRegister] = {
            r.address: r for r in registers if r.mqtt_enabled
        }

        # Stats
        self.messages_published = 0
        self.messages_skipped = 0
        self.connection_count = 0

        # Reconnection thread
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            self._setup_client()

    def _setup_client(self):
        """Setup MQTT client with callbacks."""
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)

        # Last Will Testament
        status_topic = f"{self.config.topic_prefix}/status"
        self.client.will_set(status_topic, payload="offline", qos=1, retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Handle connection established."""
        if reason_code == 0:
            self.connected = True
            self.connection_count += 1
            logger.info(f"MQTT connected to {self.config.broker}:{self.config.port}")

            # Clear cache to force re-publish of all current values
            # (broker restart loses retained messages)
            with self.lock:
                self.last_values.clear()

            # Re-publish online status
            status_topic = f"{self.config.topic_prefix}/status"
            try:
                self.client.publish(status_topic, "online", qos=1, retain=True)
            except Exception:
                pass

            # Re-publish HA discovery on reconnect
            if self.config.ha_discovery_enabled:
                self.publish_ha_discovery()
        else:
            self.connected = False
            logger.error(f"MQTT connection failed: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Handle disconnection."""
        self.connected = False
        if reason_code != 0:
            logger.warning(f"MQTT disconnected unexpectedly: {reason_code}")
            self._start_reconnect_thread()

    def _try_connect(self) -> bool:
        """Attempt a single connection."""
        try:
            self.client.connect(self.config.broker, self.config.port, keepalive=60)
            self.client.loop_start()

            for _ in range(10):
                if self.connected:
                    break
                time.sleep(0.1)

            return self.connected
        except Exception as e:
            logger.warning(f"MQTT connection failed: {e}")
            return False

    def connect(self) -> bool:
        """Connect to MQTT broker with retry logic."""
        if not self.config.enabled:
            logger.info("MQTT publishing disabled")
            return False

        delay = RETRY_INITIAL_DELAY

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            if self._try_connect():
                return True

            if attempt < RETRY_MAX_ATTEMPTS:
                logger.info(f"MQTT connection attempt {attempt}/{RETRY_MAX_ATTEMPTS} failed, retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

        logger.warning(f"MQTT: all {RETRY_MAX_ATTEMPTS} connection attempts failed. Will retry in background.")
        self._start_reconnect_thread()
        return False

    def _start_reconnect_thread(self):
        """Start background reconnection thread."""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return

        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="MQTT-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        logger.info("MQTT reconnection thread started")

    def _reconnect_loop(self):
        """Background reconnection loop."""
        while not self._stop_reconnect.is_set():
            if not self.connected:
                logger.debug("Attempting MQTT reconnection...")
                if self._try_connect():
                    logger.info("MQTT reconnected successfully")
                    break
            self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)

    def disconnect(self):
        """Disconnect from MQTT broker."""
        self._stop_reconnect.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

        if self.client:
            self.publish_status("offline")
            self.client.loop_stop()
            self.client.disconnect()
        self.connected = False
        logger.info("MQTT disconnected")

    def _build_topic(self, register: SelectedRegister) -> str:
        """Build MQTT topic for a register."""
        if register.mqtt_topic:
            return f"{self.config.topic_prefix}/{register.mqtt_topic}"
        else:
            safe_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
            return f"{self.config.topic_prefix}/{safe_name}"

    def _should_publish(self, topic: str, value: Any) -> bool:
        """
        Check if value should be published based on mode.
        Does NOT update cache — cache is updated after successful publish
        via _confirm_publish() to prevent data loss.
        """
        if self.publish_mode == 'all':
            return True

        # NaN guard: NaN != NaN is always True, would bypass change detection
        if isinstance(value, float) and math.isnan(value):
            return False

        with self.lock:
            if topic not in self.last_values:
                return True

            if self.last_values[topic] != value:
                return True

            return False

    def _confirm_publish(self, topic: str, value: Any):
        """
        Update cache after successful publish.
        Store rounded float to match what was actually sent as payload,
        preventing phantom re-publishes from floating-point drift.
        """
        with self.lock:
            if isinstance(value, float):
                self.last_values[topic] = round(value, 3)
            else:
                self.last_values[topic] = value

    def _publish(self, topic: str, payload: str, retain: bool = None) -> bool:
        """Internal publish method."""
        if not self.connected:
            return False

        if retain is None:
            retain = self.config.retain

        try:
            result = self.client.publish(topic, payload, qos=self.config.qos, retain=retain)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.messages_published += 1
                return True
            elif result.rc in (mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_CONN_LOST):
                logger.warning("MQTT connection lost during publish")
                self.connected = False
                self._start_reconnect_thread()
            return False
        except Exception as e:
            error_str = str(e).lower()
            if any(err in error_str for err in ['connection', 'socket', 'broken pipe']):
                logger.warning(f"MQTT connection error during publish: {e}")
                self.connected = False
                self._start_reconnect_thread()
            else:
                logger.error(f"MQTT publish error: {e}")
            return False

    def publish(self, topic: str, value: Any, retain: bool = None) -> bool:
        """Publish a value to topic."""
        if isinstance(value, (dict, list)):
            payload = json.dumps(value)
        elif isinstance(value, float):
            payload = str(round(value, 3))
        else:
            payload = str(value)

        return self._publish(topic, payload, retain)

    def publish_if_changed(self, topic: str, value: Any, retain: bool = None) -> bool:
        """Publish only if value changed. Two-phase: check → publish → confirm."""
        if self._should_publish(topic, value):
            if self.publish(topic, value, retain):
                self._confirm_publish(topic, value)
                return True
            return False

        self.messages_skipped += 1
        return False

    def publish_register_data(self, poll_group: str, data: Dict[int, Dict]):
        """Publish register data from a poll group."""
        if not self.connected:
            return

        for address, item in data.items():
            register = item.get('register')
            value = item.get('value')

            if register is None or not register.mqtt_enabled:
                continue

            topic = self._build_topic(register)
            self.publish_if_changed(topic, value)

    def publish_status(self, status: str):
        """Publish application status."""
        topic = f"{self.config.topic_prefix}/status"
        self.publish(topic, status, retain=True)

    def publish_ha_discovery(self) -> int:
        """Publish Home Assistant MQTT autodiscovery configs."""
        if not self.connected or not self.config.ha_discovery_enabled:
            return 0

        count = 0
        device_info = self._build_ha_device_info()

        for register in self.registers:
            if not register.mqtt_enabled:
                continue

            config = self._build_ha_sensor_config(register, device_info)
            if config:
                safe_id = f"{register.address}_{register.name.lower().replace('[', '_').replace(']', '')}"
                discovery_topic = f"{self.config.ha_discovery_prefix}/sensor/janitza/{safe_id}/config"

                if self._publish(discovery_topic, json.dumps(config), retain=True):
                    count += 1

        logger.info(f"Published {count} HA discovery configs")
        return count

    def _build_ha_device_info(self) -> Dict:
        """Build Home Assistant device info block."""
        return {
            "identifiers": ["janitza_umg512"],
            "name": self.config.ha_device_name,
            "manufacturer": "Janitza electronics GmbH",
            "model": "UMG 512-PRO",
            "sw_version": "1.4.0",
        }

    def _build_ha_sensor_config(self, register: SelectedRegister, device_info: Dict) -> Dict:
        """Build Home Assistant sensor discovery config."""
        topic = self._build_topic(register)

        safe_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
        unique_id = f"janitza_umg512_{register.address}_{safe_name}"

        config = {
            "name": register.label,
            "state_topic": topic,
            "availability_topic": f"{self.config.topic_prefix}/status",
            "unique_id": unique_id,
            "device": device_info,
        }

        if register.unit:
            config["unit_of_measurement"] = register.unit

        device_class = HA_DEVICE_CLASSES.get(register.unit)
        if device_class:
            config["device_class"] = device_class

        state_class = HA_STATE_CLASSES.get(register.unit, "measurement")
        if state_class:
            config["state_class"] = state_class

        return config

    def update_config(self, new_config: MQTTConfig):
        """Update MQTT configuration."""
        self.config = new_config
        self.publish_mode = new_config.publish_mode
        logger.info(f"MQTT config updated: {new_config.broker}:{new_config.port}")

    def update_registers(self, registers: List[SelectedRegister]):
        """Update register list."""
        self.registers = registers
        self._register_map = {r.address: r for r in registers if r.mqtt_enabled}
        logger.info(f"MQTT registers updated: {len(self._register_map)} enabled")

    def reconnect(self) -> bool:
        """Reconnect to MQTT broker with current config."""
        logger.info("MQTT reconnecting...")
        self.disconnect()

        if not self.config.enabled:
            logger.info("MQTT disabled, not reconnecting")
            return False

        self._setup_client()

        if self._try_connect():
            logger.info("MQTT reconnected successfully")
            return True
        else:
            logger.warning("MQTT reconnection failed, starting background retry")
            self._start_reconnect_thread()
            return False

    def get_stats(self) -> Dict:
        """Return publisher statistics."""
        return {
            'enabled': self.config.enabled,
            'connected': self.connected,
            'broker': self.config.broker,
            'port': self.config.port,
            'prefix': self.config.topic_prefix,
            'messages_published': self.messages_published,
            'messages_skipped': self.messages_skipped,
            'publish_mode': self.publish_mode,
            'connection_count': self.connection_count,
            'registered_topics': len(self._register_map),
        }
