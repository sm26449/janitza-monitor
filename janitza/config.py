"""Configuration loader for Janitza Monitor."""

import os
import yaml
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ModbusConfig:
    host: str = "192.168.1.100"
    port: int = 502
    unit_id: int = 1
    timeout: int = 3
    retry_attempts: int = 3
    retry_delay: float = 1.0


@dataclass
class MQTTConfig:
    enabled: bool = True
    broker: str = "192.168.1.100"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "janitza/umg512"
    retain: bool = True
    qos: int = 0
    publish_mode: str = "changed"  # "changed" or "all"
    ha_discovery_enabled: bool = True
    ha_discovery_prefix: str = "homeassistant"
    ha_device_name: str = "Janitza UMG 512-PRO"


@dataclass
class InfluxDBConfig:
    enabled: bool = False
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = ""
    bucket: str = "janitza"
    write_interval: int = 5
    publish_mode: str = "changed"  # "changed" or "all"


@dataclass
class UIConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    auth_enabled: bool = False
    auth_username: str = "admin"
    auth_password: str = "admin"


@dataclass
class PollGroup:
    interval: int
    description: str = ""


@dataclass
class SelectedRegister:
    address: int
    name: str
    label: str
    unit: str
    data_type: str
    poll_group: str
    description: str = ""  # Human-readable description from modbus_data.json
    mqtt_enabled: bool = True
    mqtt_topic: str = ""
    influxdb_enabled: bool = True
    influxdb_measurement: str = ""
    influxdb_tags: Dict[str, str] = field(default_factory=dict)
    ui_show_on_dashboard: bool = True
    ui_widget: str = "value"
    ui_config: Dict[str, Any] = field(default_factory=dict)
    thresholds: Optional[Dict[str, Any]] = None  # Color coding thresholds


class Config:
    """Configuration manager for Janitza Monitor."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = Path(config_path)
        self.registers_path = self.config_path.parent / "selected_registers.json"
        self.all_registers_path = Path("docs/modbus_data.json")

        self.modbus = ModbusConfig()
        self.mqtt = MQTTConfig()
        self.influxdb = InfluxDBConfig()
        self.ui = UIConfig()
        self.poll_groups: Dict[str, PollGroup] = {
            "realtime": PollGroup(interval=1, description="Real-time values"),
            "normal": PollGroup(interval=5, description="Standard measurements"),
            "slow": PollGroup(interval=60, description="Energy counters"),
        }
        self.selected_registers: List[SelectedRegister] = []
        self.all_registers: Dict = {}

        self.load()

    def load(self):
        """Load configuration from files."""
        self._load_yaml_config()
        self._load_selected_registers()
        self._load_all_registers()
        self._apply_env_overrides()

    def _load_yaml_config(self):
        """Load main YAML configuration."""
        if not self.config_path.exists():
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            return

        try:
            with open(self.config_path, 'r') as f:
                data = yaml.safe_load(f) or {}

            # Modbus
            if 'modbus' in data:
                m = data['modbus']
                self.modbus = ModbusConfig(
                    host=m.get('host', self.modbus.host),
                    port=m.get('port', self.modbus.port),
                    unit_id=m.get('unit_id', self.modbus.unit_id),
                    timeout=m.get('timeout', self.modbus.timeout),
                    retry_attempts=m.get('retry_attempts', self.modbus.retry_attempts),
                    retry_delay=m.get('retry_delay', self.modbus.retry_delay),
                )

            # MQTT
            if 'mqtt' in data:
                m = data['mqtt']
                ha = m.get('ha_discovery', {})
                self.mqtt = MQTTConfig(
                    enabled=m.get('enabled', self.mqtt.enabled),
                    broker=m.get('broker', self.mqtt.broker),
                    port=m.get('port', self.mqtt.port),
                    username=m.get('username', self.mqtt.username),
                    password=m.get('password', self.mqtt.password),
                    topic_prefix=m.get('topic_prefix', self.mqtt.topic_prefix),
                    retain=m.get('retain', self.mqtt.retain),
                    qos=m.get('qos', self.mqtt.qos),
                    publish_mode=m.get('publish_mode', self.mqtt.publish_mode),
                    ha_discovery_enabled=ha.get('enabled', self.mqtt.ha_discovery_enabled),
                    ha_discovery_prefix=ha.get('prefix', self.mqtt.ha_discovery_prefix),
                    ha_device_name=ha.get('device_name', self.mqtt.ha_device_name),
                )

            # InfluxDB
            if 'influxdb' in data:
                i = data['influxdb']
                self.influxdb = InfluxDBConfig(
                    enabled=i.get('enabled', self.influxdb.enabled),
                    url=i.get('url', self.influxdb.url),
                    token=i.get('token', self.influxdb.token),
                    org=i.get('org', self.influxdb.org),
                    bucket=i.get('bucket', self.influxdb.bucket),
                    write_interval=i.get('write_interval', self.influxdb.write_interval),
                    publish_mode=i.get('publish_mode', self.influxdb.publish_mode),
                )

            # UI
            if 'ui' in data:
                u = data['ui']
                auth = u.get('auth', {})
                self.ui = UIConfig(
                    host=u.get('host', self.ui.host),
                    port=u.get('port', self.ui.port),
                    auth_enabled=auth.get('enabled', self.ui.auth_enabled),
                    auth_username=auth.get('username', self.ui.auth_username),
                    auth_password=auth.get('password', self.ui.auth_password),
                )

            # Poll groups
            if 'polling' in data and 'groups' in data['polling']:
                for name, group in data['polling']['groups'].items():
                    self.poll_groups[name] = PollGroup(
                        interval=group.get('interval', 5),
                        description=group.get('description', ''),
                    )

            logger.info(f"Loaded config from {self.config_path}")

        except Exception as e:
            logger.error(f"Error loading config: {e}")

    def _load_selected_registers(self):
        """Load selected registers configuration."""
        if not self.registers_path.exists():
            logger.warning(f"Selected registers file not found: {self.registers_path}")
            return

        try:
            with open(self.registers_path, 'r') as f:
                data = json.load(f)

            # Poll groups from registers file
            if 'poll_groups' in data:
                for name, group in data['poll_groups'].items():
                    self.poll_groups[name] = PollGroup(
                        interval=group.get('interval', 5),
                        description=group.get('description', ''),
                    )

            # Registers
            self.selected_registers = []
            for reg in data.get('registers', []):
                mqtt = reg.get('mqtt', {})
                influx = reg.get('influxdb', {})
                ui = reg.get('ui', {})

                self.selected_registers.append(SelectedRegister(
                    address=reg['address'],
                    name=reg['name'],
                    label=reg.get('label', reg['name']),
                    unit=reg.get('unit', ''),
                    data_type=reg.get('data_type', 'float'),
                    poll_group=reg.get('poll_group', 'normal'),
                    description=reg.get('description', ''),
                    mqtt_enabled=mqtt.get('enabled', True),
                    mqtt_topic=mqtt.get('topic', ''),
                    influxdb_enabled=influx.get('enabled', True),
                    influxdb_measurement=influx.get('measurement', ''),
                    influxdb_tags=influx.get('tags', {}),
                    ui_show_on_dashboard=ui.get('show_on_dashboard', True),
                    ui_widget=ui.get('widget', 'value'),
                    ui_config=ui,
                    thresholds=reg.get('thresholds'),
                ))

            logger.info(f"Loaded {len(self.selected_registers)} selected registers")

        except Exception as e:
            logger.error(f"Error loading selected registers: {e}")

    def _load_all_registers(self):
        """Load all available registers from modbus_data.json."""
        if not self.all_registers_path.exists():
            logger.warning(f"All registers file not found: {self.all_registers_path}")
            return

        try:
            with open(self.all_registers_path, 'r') as f:
                self.all_registers = json.load(f)
            logger.info(f"Loaded all registers from {self.all_registers_path}")
        except Exception as e:
            logger.error(f"Error loading all registers: {e}")

    def _apply_env_overrides(self):
        """Apply environment variable overrides."""
        # Modbus
        if os.getenv('MODBUS_HOST'):
            self.modbus.host = os.getenv('MODBUS_HOST')
        if os.getenv('MODBUS_PORT'):
            self.modbus.port = int(os.getenv('MODBUS_PORT'))
        if os.getenv('MODBUS_UNIT_ID'):
            self.modbus.unit_id = int(os.getenv('MODBUS_UNIT_ID'))

        # MQTT
        if os.getenv('MQTT_ENABLED'):
            self.mqtt.enabled = os.getenv('MQTT_ENABLED').lower() == 'true'
        if os.getenv('MQTT_BROKER'):
            self.mqtt.broker = os.getenv('MQTT_BROKER')
        if os.getenv('MQTT_PORT'):
            self.mqtt.port = int(os.getenv('MQTT_PORT'))
        if os.getenv('MQTT_USERNAME'):
            self.mqtt.username = os.getenv('MQTT_USERNAME')
        if os.getenv('MQTT_PASSWORD'):
            self.mqtt.password = os.getenv('MQTT_PASSWORD')
        if os.getenv('MQTT_PREFIX'):
            self.mqtt.topic_prefix = os.getenv('MQTT_PREFIX')
        if os.getenv('MQTT_PUBLISH_MODE'):
            self.mqtt.publish_mode = os.getenv('MQTT_PUBLISH_MODE')

        # InfluxDB
        if os.getenv('INFLUXDB_ENABLED'):
            self.influxdb.enabled = os.getenv('INFLUXDB_ENABLED').lower() == 'true'
        if os.getenv('INFLUXDB_URL'):
            self.influxdb.url = os.getenv('INFLUXDB_URL')
        if os.getenv('INFLUXDB_TOKEN'):
            self.influxdb.token = os.getenv('INFLUXDB_TOKEN')
        if os.getenv('INFLUXDB_ORG'):
            self.influxdb.org = os.getenv('INFLUXDB_ORG')
        if os.getenv('INFLUXDB_BUCKET'):
            self.influxdb.bucket = os.getenv('INFLUXDB_BUCKET')
        if os.getenv('INFLUXDB_PUBLISH_MODE'):
            self.influxdb.publish_mode = os.getenv('INFLUXDB_PUBLISH_MODE')

        # UI
        if os.getenv('UI_PORT'):
            self.ui.port = int(os.getenv('UI_PORT'))

    def save_selected_registers(self, registers: List[Dict]):
        """Save selected registers to file."""
        data = {
            "version": "1.0",
            "registers": registers,
            "poll_groups": {
                name: {"interval": group.interval, "description": group.description}
                for name, group in self.poll_groups.items()
            }
        }

        with open(self.registers_path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Reload
        self._load_selected_registers()
        logger.info(f"Saved {len(registers)} selected registers")

    def get_registers_by_poll_group(self) -> Dict[str, List[SelectedRegister]]:
        """Group selected registers by poll group."""
        groups = {}
        for reg in self.selected_registers:
            if reg.poll_group not in groups:
                groups[reg.poll_group] = []
            groups[reg.poll_group].append(reg)
        return groups

    def to_dict(self) -> Dict:
        """Export config as dictionary."""
        return {
            "modbus": {
                "host": self.modbus.host,
                "port": self.modbus.port,
                "unit_id": self.modbus.unit_id,
                "timeout": self.modbus.timeout,
                "retry_attempts": self.modbus.retry_attempts,
                "retry_delay": self.modbus.retry_delay,
            },
            "mqtt": {
                "enabled": self.mqtt.enabled,
                "broker": self.mqtt.broker,
                "port": self.mqtt.port,
                "username": self.mqtt.username,
                "topic_prefix": self.mqtt.topic_prefix,
                "retain": self.mqtt.retain,
                "qos": self.mqtt.qos,
                "publish_mode": self.mqtt.publish_mode,
                "ha_discovery_enabled": self.mqtt.ha_discovery_enabled,
                "ha_discovery_prefix": self.mqtt.ha_discovery_prefix,
                "ha_device_name": self.mqtt.ha_device_name,
            },
            "influxdb": {
                "enabled": self.influxdb.enabled,
                "url": self.influxdb.url,
                "org": self.influxdb.org,
                "bucket": self.influxdb.bucket,
                "write_interval": self.influxdb.write_interval,
                "publish_mode": self.influxdb.publish_mode,
            },
            "poll_groups": {
                name: {"interval": g.interval, "description": g.description}
                for name, g in self.poll_groups.items()
            },
            "selected_registers_count": len(self.selected_registers),
        }

    def get_env_overrides(self) -> Dict[str, str]:
        """Return dict of environment variable overrides that are currently set."""
        overrides = {}
        env_mappings = {
            'MODBUS_HOST': 'modbus.host',
            'MODBUS_PORT': 'modbus.port',
            'MODBUS_UNIT_ID': 'modbus.unit_id',
            'MQTT_ENABLED': 'mqtt.enabled',
            'MQTT_BROKER': 'mqtt.broker',
            'MQTT_PORT': 'mqtt.port',
            'MQTT_USERNAME': 'mqtt.username',
            'MQTT_PASSWORD': 'mqtt.password',
            'MQTT_PREFIX': 'mqtt.topic_prefix',
            'MQTT_PUBLISH_MODE': 'mqtt.publish_mode',
            'INFLUXDB_ENABLED': 'influxdb.enabled',
            'INFLUXDB_URL': 'influxdb.url',
            'INFLUXDB_TOKEN': 'influxdb.token',
            'INFLUXDB_ORG': 'influxdb.org',
            'INFLUXDB_BUCKET': 'influxdb.bucket',
            'INFLUXDB_PUBLISH_MODE': 'influxdb.publish_mode',
            'UI_PORT': 'ui.port',
        }
        for env_var, config_path in env_mappings.items():
            if os.getenv(env_var):
                overrides[config_path] = os.getenv(env_var)
        return overrides

    def save_yaml_config(self):
        """Save current configuration to YAML file."""
        data = {
            'modbus': {
                'host': self.modbus.host,
                'port': self.modbus.port,
                'unit_id': self.modbus.unit_id,
                'timeout': self.modbus.timeout,
                'retry_attempts': self.modbus.retry_attempts,
                'retry_delay': self.modbus.retry_delay,
            },
            'mqtt': {
                'enabled': self.mqtt.enabled,
                'broker': self.mqtt.broker,
                'port': self.mqtt.port,
                'username': self.mqtt.username,
                'password': self.mqtt.password,
                'topic_prefix': self.mqtt.topic_prefix,
                'retain': self.mqtt.retain,
                'qos': self.mqtt.qos,
                'publish_mode': self.mqtt.publish_mode,
                'ha_discovery': {
                    'enabled': self.mqtt.ha_discovery_enabled,
                    'prefix': self.mqtt.ha_discovery_prefix,
                    'device_name': self.mqtt.ha_device_name,
                }
            },
            'influxdb': {
                'enabled': self.influxdb.enabled,
                'url': self.influxdb.url,
                'token': self.influxdb.token,
                'org': self.influxdb.org,
                'bucket': self.influxdb.bucket,
                'write_interval': self.influxdb.write_interval,
                'publish_mode': self.influxdb.publish_mode,
            },
            'ui': {
                'host': self.ui.host,
                'port': self.ui.port,
                'auth': {
                    'enabled': self.ui.auth_enabled,
                    'username': self.ui.auth_username,
                    'password': self.ui.auth_password,
                }
            },
            'polling': {
                'groups': {
                    name: {'interval': g.interval, 'description': g.description}
                    for name, g in self.poll_groups.items()
                }
            }
        }

        # Ensure config directory exists
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.config_path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logger.info(f"Saved config to {self.config_path}")

    def update_modbus(self, host: str = None, port: int = None, unit_id: int = None,
                      timeout: int = None, retry_attempts: int = None, retry_delay: float = None):
        """Update Modbus configuration."""
        if host is not None:
            self.modbus.host = host
        if port is not None:
            self.modbus.port = port
        if unit_id is not None:
            self.modbus.unit_id = unit_id
        if timeout is not None:
            self.modbus.timeout = timeout
        if retry_attempts is not None:
            self.modbus.retry_attempts = retry_attempts
        if retry_delay is not None:
            self.modbus.retry_delay = retry_delay

    def update_mqtt(self, enabled: bool = None, broker: str = None, port: int = None,
                    username: str = None, password: str = None, topic_prefix: str = None,
                    retain: bool = None, qos: int = None, publish_mode: str = None,
                    ha_discovery_enabled: bool = None, ha_discovery_prefix: str = None,
                    ha_device_name: str = None):
        """Update MQTT configuration."""
        if enabled is not None:
            self.mqtt.enabled = enabled
        if broker is not None:
            self.mqtt.broker = broker
        if port is not None:
            self.mqtt.port = port
        if username is not None:
            self.mqtt.username = username
        if password is not None:
            self.mqtt.password = password
        if topic_prefix is not None:
            self.mqtt.topic_prefix = topic_prefix
        if retain is not None:
            self.mqtt.retain = retain
        if qos is not None:
            self.mqtt.qos = qos
        if publish_mode is not None:
            self.mqtt.publish_mode = publish_mode
        if ha_discovery_enabled is not None:
            self.mqtt.ha_discovery_enabled = ha_discovery_enabled
        if ha_discovery_prefix is not None:
            self.mqtt.ha_discovery_prefix = ha_discovery_prefix
        if ha_device_name is not None:
            self.mqtt.ha_device_name = ha_device_name

    def update_influxdb(self, enabled: bool = None, url: str = None, token: str = None,
                        org: str = None, bucket: str = None, write_interval: int = None,
                        publish_mode: str = None):
        """Update InfluxDB configuration."""
        if enabled is not None:
            self.influxdb.enabled = enabled
        if url is not None:
            self.influxdb.url = url
        if token is not None:
            self.influxdb.token = token
        if org is not None:
            self.influxdb.org = org
        if bucket is not None:
            self.influxdb.bucket = bucket
        if write_interval is not None:
            self.influxdb.write_interval = write_interval
        if publish_mode is not None:
            self.influxdb.publish_mode = publish_mode
