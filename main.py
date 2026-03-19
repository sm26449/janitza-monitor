#!/usr/bin/env python3
"""Janitza UMG 512-PRO Monitor - Main Application."""

import asyncio
import signal
import sys
import logging
import argparse
import threading
from pathlib import Path

import uvicorn

from janitza.config import Config
from janitza.modbus_client import ModbusClient
from janitza.mqtt_publisher import MQTTPublisher
from janitza.influxdb_publisher import InfluxDBPublisher
from janitza.api import create_api

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class JanitzaMonitor:
    """Main application class."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = config_path
        self.config = None
        self.modbus_client = None
        self.mqtt_publisher = None
        self.influxdb_publisher = None
        self.app = None
        self.ws_manager = None
        self.running = False

    def setup(self):
        """Initialize all components."""
        logger.info("Janitza UMG 512-PRO Monitor starting...")

        # Load configuration
        self.config = Config(self.config_path)
        logger.info(f"Loaded config from {self.config_path}")
        logger.info(f"Selected registers: {len(self.config.selected_registers)}")

        # Initialize MQTT publisher (connection will be done in background)
        if self.config.mqtt.enabled:
            self.mqtt_publisher = MQTTPublisher(
                config=self.config.mqtt,
                registers=self.config.selected_registers,
                publish_mode=self.config.mqtt.publish_mode
            )
            logger.info("MQTT publisher initialized (connecting in background)")

        # Initialize InfluxDB publisher
        if self.config.influxdb.enabled:
            self.influxdb_publisher = InfluxDBPublisher(
                config=self.config.influxdb,
                registers=self.config.selected_registers,
                publish_mode=self.config.influxdb.publish_mode
            )
            logger.info("InfluxDB publisher initialized")

        # Initialize Modbus client
        self.modbus_client = ModbusClient(
            config=self.config.modbus,
            registers=self.config.selected_registers,
            poll_groups=self.config.poll_groups,
        )

        # Create API
        self.app, self.ws_manager = create_api(
            config=self.config,
            modbus_client=self.modbus_client,
            mqtt_publisher=self.mqtt_publisher,
            influxdb_publisher=self.influxdb_publisher,
        )

        logger.info("API server initialized")

    def _connect_mqtt_background(self):
        """Connect to MQTT in background thread."""
        if self.mqtt_publisher:
            # Wait for network to be ready (Docker networking delay)
            import time
            import socket
            broker = self.config.mqtt.broker
            port = self.config.mqtt.port
            for i in range(30):  # Wait up to 30 seconds for network
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect((broker, port))
                    s.close()
                    logger.info(f"Network ready, MQTT broker reachable at {broker}:{port}")
                    break
                except Exception as e:
                    if i < 29:
                        time.sleep(1)
                    else:
                        logger.warning(f"MQTT broker {broker}:{port} not reachable after 30s")
            logger.info("Attempting MQTT connection in background...")
            if self.mqtt_publisher.connect():
                logger.info("MQTT connected successfully")
                # Publish Home Assistant discovery
                if self.config.mqtt.ha_discovery_enabled:
                    self.mqtt_publisher.publish_ha_discovery()
            else:
                logger.warning("MQTT connection failed - will retry automatically")

    def _connect_modbus_background(self):
        """Connect to Modbus in background thread."""
        if self.modbus_client:
            # Wait for network to be ready
            import time
            import socket
            host = self.config.modbus.host
            port = self.config.modbus.port
            for i in range(30):  # Wait up to 30 seconds for network
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect((host, port))
                    s.close()
                    logger.info(f"Network ready, Modbus device reachable at {host}:{port}")
                    break
                except Exception as e:
                    if i < 29:
                        time.sleep(1)
                    else:
                        logger.warning(f"Modbus device {host}:{port} not reachable after 30s")
            logger.info("Attempting Modbus connection...")
            if self.modbus_client.connect():
                self.modbus_client.start_polling()
                logger.info("Modbus connected and polling started")
            else:
                logger.warning("Modbus connection failed - will retry")

    def start(self):
        """Start all components."""
        self.running = True

        # Connect MQTT in background thread (non-blocking)
        if self.mqtt_publisher:
            mqtt_thread = threading.Thread(
                target=self._connect_mqtt_background,
                name="MQTT-Init",
                daemon=True
            )
            mqtt_thread.start()

        # Connect Modbus in background thread (non-blocking)
        modbus_thread = threading.Thread(
            target=self._connect_modbus_background,
            name="Modbus-Init",
            daemon=True
        )
        modbus_thread.start()

        logger.info(f"Starting web server on {self.config.ui.host}:{self.config.ui.port}")

    def stop(self):
        """Stop all components."""
        self.running = False
        logger.info("Shutting down...")

        if self.modbus_client:
            self.modbus_client.disconnect()

        if self.mqtt_publisher:
            self.mqtt_publisher.disconnect()

        if self.influxdb_publisher:
            self.influxdb_publisher.close()

        logger.info("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="Janitza UMG 512-PRO Monitor")
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="Path to configuration file"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override UI host"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override UI port"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Create application
    monitor = JanitzaMonitor(args.config)
    monitor.setup()

    # Override host/port if specified
    host = args.host or monitor.config.ui.host
    port = args.port or monitor.config.ui.port

    # Setup signal handlers
    def signal_handler(sig, frame):
        monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start components
    monitor.start()

    # Run uvicorn server
    uvicorn.run(
        monitor.app,
        host=host,
        port=port,
        log_level="info" if not args.debug else "debug",
    )


if __name__ == "__main__":
    main()
