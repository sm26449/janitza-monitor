"""REST API and WebSocket server for Janitza Monitor."""

import asyncio
import json
import logging
import threading
from typing import Dict, Any, List, Optional, Set
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .mqtt_publisher import MQTTPublisher
from .influxdb_publisher import InfluxDBPublisher

logger = logging.getLogger(__name__)


class RegisterQuery(BaseModel):
    """Request model for register query."""
    address: int
    data_type: str = "float"


class RegisterBatchQuery(BaseModel):
    """Request model for batch register query."""
    registers: List[RegisterQuery]


class ThresholdConfig(BaseModel):
    """Threshold configuration for color coding."""
    enabled: bool = True
    dangerLow: Optional[float] = None
    warningLow: Optional[float] = None
    warningHigh: Optional[float] = None
    dangerHigh: Optional[float] = None


class SelectedRegisterUpdate(BaseModel):
    """Request model for updating selected registers."""
    address: int
    name: str
    label: str
    unit: str = ""
    description: str = ""  # Human-readable description
    data_type: str = "float"
    poll_group: str = "normal"
    mqtt_enabled: bool = True
    mqtt_topic: str = ""
    influxdb_enabled: bool = True
    influxdb_measurement: str = ""
    influxdb_tags: Dict[str, str] = {}
    ui_show_on_dashboard: bool = True
    ui_widget: str = "value"
    ui_config: Dict[str, Any] = {}
    thresholds: Optional[ThresholdConfig] = None


class ModbusConfigUpdate(BaseModel):
    """Request model for Modbus configuration update."""
    host: Optional[str] = None
    port: Optional[int] = None
    unit_id: Optional[int] = None
    timeout: Optional[int] = None
    retry_attempts: Optional[int] = None
    retry_delay: Optional[float] = None


class MQTTConfigUpdate(BaseModel):
    """Request model for MQTT configuration update."""
    enabled: Optional[bool] = None
    broker: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: Optional[str] = None
    retain: Optional[bool] = None
    qos: Optional[int] = None
    publish_mode: Optional[str] = None
    ha_discovery_enabled: Optional[bool] = None
    ha_discovery_prefix: Optional[str] = None
    ha_device_name: Optional[str] = None


class InfluxDBConfigUpdate(BaseModel):
    """Request model for InfluxDB configuration update."""
    enabled: Optional[bool] = None
    url: Optional[str] = None
    token: Optional[str] = None
    org: Optional[str] = None
    bucket: Optional[str] = None
    write_interval: Optional[int] = None
    publish_mode: Optional[str] = None


class WebSocketManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            self.active_connections.add(websocket)
        logger.info(f"WebSocket connected. Active: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            self.active_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Active: {len(self.active_connections)}")

    async def broadcast(self, message: Dict):
        """Broadcast message to all connected clients."""
        if not self.active_connections:
            return

        data = json.dumps(message)
        async with self.lock:
            disconnected = set()
            for connection in self.active_connections:
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.add(connection)

            for conn in disconnected:
                self.active_connections.discard(conn)


def create_api(config, modbus_client, mqtt_publisher, influxdb_publisher) -> FastAPI:
    """
    Create FastAPI application.

    Args:
        config: Application configuration
        modbus_client: ModbusClient instance
        mqtt_publisher: MQTTPublisher instance
        influxdb_publisher: InfluxDBPublisher instance

    Returns:
        FastAPI application
    """
    # WebSocket manager
    ws_manager = WebSocketManager()

    # Store current values for dashboard
    current_values: Dict[int, Dict] = {}
    last_update = {"timestamp": None}

    # Store event loop reference for thread-safe async calls
    main_loop = {"loop": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        main_loop["loop"] = asyncio.get_running_loop()
        logger.info("API started, event loop captured")
        yield
        # Shutdown (cleanup if needed)
        logger.info("API shutting down")

    app = FastAPI(
        title="Janitza UMG 512-PRO Monitor",
        description="Monitor and query Janitza power quality analyzer",
        version="1.0.0",
        lifespan=lifespan
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def data_callback(poll_group: str, data: Dict[int, Dict]):
        """Callback from Modbus poller to update values and publish."""
        nonlocal current_values, last_update

        # Update current values
        for address, item in data.items():
            current_values[address] = {
                'value': item.get('value'),
                'name': item.get('register').name if item.get('register') else '',
                'label': item.get('register').label if item.get('register') else '',
                'unit': item.get('register').unit if item.get('register') else '',
                'poll_group': poll_group,
                'timestamp': datetime.now().isoformat(),
            }

        last_update['timestamp'] = datetime.now().isoformat()

        # Publish to MQTT
        if mqtt_publisher:
            mqtt_publisher.publish_register_data(poll_group, data)

        # Publish to InfluxDB
        if influxdb_publisher:
            influxdb_publisher.write_register_data(poll_group, data)

        # Broadcast via WebSocket (thread-safe async call)
        if main_loop["loop"]:
            asyncio.run_coroutine_threadsafe(
                ws_manager.broadcast({
                    'type': 'data',
                    'poll_group': poll_group,
                    'values': {
                        str(addr): {
                            'value': item.get('value'),
                            'name': item.get('register').name if item.get('register') else '',
                        }
                        for addr, item in data.items()
                    },
                    'timestamp': last_update['timestamp'],
                }),
                main_loop["loop"]
            )

    # Set the callback on modbus client
    if modbus_client:
        modbus_client.publish_callback = data_callback

    # --- Routes ---

    @app.get("/")
    async def root():
        """Serve main UI."""
        return FileResponse("ui/templates/index.html")

    @app.get("/api/status")
    async def get_status():
        """Get system status."""
        return {
            "modbus": modbus_client.get_stats() if modbus_client else {},
            "mqtt": mqtt_publisher.get_stats() if mqtt_publisher else {},
            "influxdb": influxdb_publisher.get_stats() if influxdb_publisher else {},
            "websocket_clients": len(ws_manager.active_connections),
            "last_update": last_update['timestamp'],
        }

    @app.get("/api/config")
    async def get_config():
        """Get current configuration."""
        return config.to_dict()

    @app.get("/api/registers/all")
    async def get_all_registers():
        """Get all available registers from modbus_data.json."""
        return config.all_registers

    @app.get("/api/registers/selected")
    async def get_selected_registers():
        """Get currently selected registers."""
        return {
            "registers": [
                {
                    "address": r.address,
                    "name": r.name,
                    "description": r.description,
                    "label": r.label,
                    "unit": r.unit,
                    "data_type": r.data_type,
                    "poll_group": r.poll_group,
                    "mqtt_enabled": r.mqtt_enabled,
                    "mqtt_topic": r.mqtt_topic,
                    "influxdb_enabled": r.influxdb_enabled,
                    "influxdb_measurement": r.influxdb_measurement,
                    "influxdb_tags": r.influxdb_tags,
                    "ui_show_on_dashboard": r.ui_show_on_dashboard,
                    "ui_widget": r.ui_widget,
                    "ui_config": r.ui_config,
                    "thresholds": r.thresholds if hasattr(r, 'thresholds') else None,
                }
                for r in config.selected_registers
            ],
            "poll_groups": {
                name: {"interval": g.interval, "description": g.description}
                for name, g in config.poll_groups.items()
            }
        }

    @app.post("/api/registers/selected")
    async def update_selected_registers(registers: List[SelectedRegisterUpdate]):
        """Update selected registers configuration."""
        try:
            reg_list = [
                {
                    "address": r.address,
                    "name": r.name,
                    "description": r.description,
                    "label": r.label,
                    "unit": r.unit,
                    "data_type": r.data_type,
                    "poll_group": r.poll_group,
                    "mqtt": {
                        "enabled": r.mqtt_enabled,
                        "topic": r.mqtt_topic,
                    },
                    "influxdb": {
                        "enabled": r.influxdb_enabled,
                        "measurement": r.influxdb_measurement,
                        "tags": r.influxdb_tags,
                    },
                    "ui": {
                        "show_on_dashboard": r.ui_show_on_dashboard,
                        "widget": r.ui_widget,
                        **r.ui_config,
                    },
                    "thresholds": r.thresholds.dict() if r.thresholds else None,
                }
                for r in registers
            ]

            config.save_selected_registers(reg_list)

            # Auto-reload pollers with new registers
            if modbus_client:
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reload_registers()

            if mqtt_publisher:
                mqtt_publisher.update_registers(config.selected_registers)
                if config.mqtt.ha_discovery_enabled:
                    mqtt_publisher.publish_ha_discovery()

            if influxdb_publisher:
                influxdb_publisher.update_registers(config.selected_registers)

            return {"status": "ok", "count": len(reg_list)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/values")
    async def get_current_values():
        """Get all current values."""
        return {
            "values": current_values,
            "timestamp": last_update['timestamp'],
        }

    @app.get("/api/values/{address}")
    async def get_value(address: int):
        """Get current value for a specific register."""
        if address in current_values:
            return current_values[address]
        raise HTTPException(status_code=404, detail=f"Register {address} not found")

    @app.post("/api/query/register")
    async def query_register(query: RegisterQuery):
        """Query a single register on-demand."""
        if not modbus_client:
            raise HTTPException(status_code=503, detail="Modbus client not available")

        value = modbus_client.read_register(query.address, query.data_type)
        if value is not None:
            return {
                "address": query.address,
                "value": value,
                "data_type": query.data_type,
                "timestamp": datetime.now().isoformat(),
            }
        raise HTTPException(status_code=500, detail="Failed to read register")

    @app.post("/api/query/batch")
    async def query_batch(query: RegisterBatchQuery):
        """Query multiple registers on-demand."""
        if not modbus_client:
            raise HTTPException(status_code=503, detail="Modbus client not available")

        registers = [{"address": r.address, "data_type": r.data_type} for r in query.registers]
        results = modbus_client.read_registers_batch(registers)

        return {
            "values": {
                str(addr): value for addr, value in results.items()
            },
            "timestamp": datetime.now().isoformat(),
        }

    @app.get("/api/search")
    async def search_registers(
        q: str = Query(..., min_length=1, description="Search query"),
        category: Optional[str] = Query(None, description="Filter by category")
    ):
        """Search available registers."""
        results = []
        query = q.lower()

        measurements = config.all_registers.get('measurements', {})

        for cat_name, cat_data in measurements.items():
            if category and cat_name != category:
                continue

            # Check entries
            if 'entries' in cat_data:
                for entry in cat_data['entries']:
                    if _matches_query(entry, query):
                        results.append({**entry, 'category': cat_name})

            # Check subtypes
            if 'subtypes' in cat_data:
                for subtype_name, subtype_data in cat_data['subtypes'].items():
                    for entry in subtype_data.get('entries', []):
                        if _matches_query(entry, query):
                            results.append({
                                **entry,
                                'category': cat_name,
                                'subtype': subtype_name
                            })

        return {"results": results[:100], "total": len(results)}

    def _matches_query(entry: Dict, query: str) -> bool:
        """Check if entry matches search query."""
        name = entry.get('name', '').lower()
        unit = entry.get('unit', '').lower()
        address = str(entry.get('address', ''))

        return query in name or query in unit or query == address

    @app.get("/api/poll-groups")
    async def get_poll_groups():
        """Get poll group configurations."""
        return {
            name: {"interval": g.interval, "description": g.description}
            for name, g in config.poll_groups.items()
        }

    # --- Config Management ---

    @app.get("/api/config/env-overrides")
    async def get_env_overrides():
        """Get environment variable overrides currently in effect."""
        return config.get_env_overrides()

    @app.get("/api/config/modbus")
    async def get_modbus_config():
        """Get Modbus configuration."""
        return {
            "host": config.modbus.host,
            "port": config.modbus.port,
            "unit_id": config.modbus.unit_id,
            "timeout": config.modbus.timeout,
            "retry_attempts": config.modbus.retry_attempts,
            "retry_delay": config.modbus.retry_delay,
        }

    @app.post("/api/config/modbus")
    async def update_modbus_config(update: ModbusConfigUpdate):
        """Update Modbus configuration."""
        try:
            config.update_modbus(
                host=update.host,
                port=update.port,
                unit_id=update.unit_id,
                timeout=update.timeout,
                retry_attempts=update.retry_attempts,
                retry_delay=update.retry_delay,
            )
            config.save_yaml_config()
            return {"status": "ok", "message": "Modbus config updated. Apply to reconnect."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/config/mqtt")
    async def get_mqtt_config():
        """Get MQTT configuration."""
        return {
            "enabled": config.mqtt.enabled,
            "broker": config.mqtt.broker,
            "port": config.mqtt.port,
            "username": config.mqtt.username,
            "topic_prefix": config.mqtt.topic_prefix,
            "retain": config.mqtt.retain,
            "qos": config.mqtt.qos,
            "publish_mode": config.mqtt.publish_mode,
            "ha_discovery_enabled": config.mqtt.ha_discovery_enabled,
            "ha_discovery_prefix": config.mqtt.ha_discovery_prefix,
            "ha_device_name": config.mqtt.ha_device_name,
        }

    @app.post("/api/config/mqtt")
    async def update_mqtt_config(update: MQTTConfigUpdate):
        """Update MQTT configuration."""
        try:
            config.update_mqtt(
                enabled=update.enabled,
                broker=update.broker,
                port=update.port,
                username=update.username,
                password=update.password,
                topic_prefix=update.topic_prefix,
                retain=update.retain,
                qos=update.qos,
                publish_mode=update.publish_mode,
                ha_discovery_enabled=update.ha_discovery_enabled,
                ha_discovery_prefix=update.ha_discovery_prefix,
                ha_device_name=update.ha_device_name,
            )
            config.save_yaml_config()
            return {"status": "ok", "message": "MQTT config updated. Apply to reconnect."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/config/influxdb")
    async def get_influxdb_config():
        """Get InfluxDB configuration."""
        return {
            "enabled": config.influxdb.enabled,
            "url": config.influxdb.url,
            "org": config.influxdb.org,
            "bucket": config.influxdb.bucket,
            "write_interval": config.influxdb.write_interval,
            "publish_mode": config.influxdb.publish_mode,
        }

    @app.post("/api/config/influxdb")
    async def update_influxdb_config(update: InfluxDBConfigUpdate):
        """Update InfluxDB configuration."""
        try:
            config.update_influxdb(
                enabled=update.enabled,
                url=update.url,
                token=update.token,
                org=update.org,
                bucket=update.bucket,
                write_interval=update.write_interval,
                publish_mode=update.publish_mode,
            )
            config.save_yaml_config()
            return {"status": "ok", "message": "InfluxDB config updated. Apply to reconnect."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/config/apply")
    async def apply_config():
        """Apply configuration changes by reconnecting all services."""
        nonlocal mqtt_publisher, influxdb_publisher

        results = {"modbus": False, "mqtt": False, "influxdb": False}

        try:
            # Reconnect Modbus
            if modbus_client:
                modbus_client.update_config(config.modbus)
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                results["modbus"] = modbus_client.reconnect()

            # Handle MQTT - create if needed
            if config.mqtt.enabled:
                if mqtt_publisher:
                    mqtt_publisher.update_config(config.mqtt)
                    mqtt_publisher.update_registers(config.selected_registers)
                    results["mqtt"] = mqtt_publisher.reconnect()
                else:
                    # Create new MQTT publisher
                    mqtt_publisher = MQTTPublisher(
                        config=config.mqtt,
                        registers=config.selected_registers,
                        publish_mode=config.mqtt.publish_mode
                    )
                    # Connect in background
                    def connect_mqtt():
                        if mqtt_publisher.connect():
                            logger.info("MQTT connected after enable")
                            if config.mqtt.ha_discovery_enabled:
                                mqtt_publisher.publish_ha_discovery()
                    threading.Thread(target=connect_mqtt, daemon=True).start()
                    results["mqtt"] = True
            elif mqtt_publisher:
                # Disable MQTT
                mqtt_publisher.disconnect()
                results["mqtt"] = True

            # Handle InfluxDB - create if needed
            if config.influxdb.enabled:
                if influxdb_publisher:
                    influxdb_publisher.update_config(config.influxdb)
                    influxdb_publisher.update_registers(config.selected_registers)
                    results["influxdb"] = influxdb_publisher.reconnect()
                else:
                    # Create new InfluxDB publisher
                    influxdb_publisher = InfluxDBPublisher(
                        config=config.influxdb,
                        registers=config.selected_registers,
                        publish_mode=config.influxdb.publish_mode
                    )
                    results["influxdb"] = influxdb_publisher.connected
                    logger.info(f"InfluxDB publisher created, connected: {influxdb_publisher.connected}")
            elif influxdb_publisher:
                # Disable InfluxDB
                influxdb_publisher.close()
                results["influxdb"] = True

            return {
                "status": "ok",
                "results": results,
                "message": "Configuration applied"
            }
        except Exception as e:
            logger.error(f"Error applying config: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/config/reload-registers")
    async def reload_registers():
        """Reload registers without full reconnect."""
        try:
            # Reload config
            config._load_selected_registers()

            # Update clients
            if modbus_client:
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reload_registers()

            if mqtt_publisher:
                mqtt_publisher.update_registers(config.selected_registers)

            if influxdb_publisher:
                influxdb_publisher.update_registers(config.selected_registers)

            return {
                "status": "ok",
                "count": len(config.selected_registers),
                "message": "Registers reloaded"
            }
        except Exception as e:
            logger.error(f"Error reloading registers: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # --- WebSocket ---

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time data."""
        await ws_manager.connect(websocket)
        try:
            # Send initial data
            await websocket.send_json({
                'type': 'init',
                'values': current_values,
                'timestamp': last_update['timestamp'],
            })

            # Keep connection alive
            while True:
                try:
                    # Wait for messages (ping/pong handled automatically)
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=30)

                    # Handle client messages
                    try:
                        msg = json.loads(data)
                        if msg.get('type') == 'ping':
                            await websocket.send_json({'type': 'pong'})
                        elif msg.get('type') == 'subscribe':
                            # Client can subscribe to specific addresses
                            pass
                    except json.JSONDecodeError:
                        pass

                except asyncio.TimeoutError:
                    # Send ping
                    await websocket.send_json({'type': 'ping'})

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            await ws_manager.disconnect(websocket)

    # --- Static files ---

    # Mount static files last
    app.mount("/static", StaticFiles(directory="ui"), name="static")

    return app, ws_manager
