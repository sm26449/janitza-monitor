"""REST API and WebSocket server for Janitza Monitor."""

import asyncio
import hmac
import json
import logging
import os
import threading
from typing import Dict, Any, List, Optional, Set
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Body
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
        version="2.2.0",
        lifespan=lifespan
    )

    # Expose the live value cache so the virtual-meter engine can read it.
    app.state.current_values = current_values

    # CORS — the UI is same-origin so it needs no CORS; the wildcard only eases
    # read-only third-party access. Credentials are OFF (wildcard + credentials is
    # spec-invalid and a CSRF liability). NOTE: the API is UNAUTHENTICATED, incl.
    # control endpoints — run on a trusted LAN / behind an auth proxy. See README.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional write protection (opt-in, defense-in-depth for a LAN appliance):
    # if API_KEY (or JANITZA_API_KEY) is set, every state-changing request
    # (POST/PUT/PATCH/DELETE) must carry a matching X-API-Key header. Read-only
    # telemetry (GET) and the on-demand query POSTs stay open so the UI works
    # without a key. Unset => fully open (default, backward-compatible).
    _api_key = os.getenv("API_KEY") or os.getenv("JANITZA_API_KEY") or ""
    _open_writes = {"/api/query/register", "/api/query/batch"}  # POST but read-only

    @app.middleware("http")
    async def _write_guard(request, call_next):
        if (_api_key and request.method in ("POST", "PUT", "PATCH", "DELETE")
                and request.url.path not in _open_writes):
            if not hmac.compare_digest(request.headers.get("X-API-Key", ""), _api_key):
                return JSONResponse({"detail": "missing or invalid API key"}, status_code=401)
        return await call_next(request)

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

    @app.get("/health")
    async def health():
        """Health for the container probe + external monitors.

        Body ``status`` = worst of (virtual-meter health, Modbus acquisition
        health) and includes a ``modbus`` block (freshness of the upstream data).
        The HTTP CODE is deliberately 503 ONLY when an enabled virtual meter is
        genuinely ``down`` (a real fault a restart may clear). A stale/dead
        Modbus source degrades the body ``status`` but returns HTTP 200 —
        restarting the container cannot fix an unreachable meter, and we must not
        restart-loop on an upstream-device problem (the vmeter freshness watchdog
        already fail-safes the consumers)."""
        rank = {"ok": 0, "degraded": 1, "down": 2}
        mgr = getattr(app.state, "vmeter_manager", None)
        vh = mgr.health() if mgr else {"status": "ok", "enabled_meters": 0, "meters": []}
        threshold = getattr(config.modbus, "stale_after_s", 30)
        mh = modbus_client.data_health(threshold) if modbus_client else {"status": "ok"}
        body = dict(vh)
        body["modbus"] = mh
        body["status"] = max([vh.get("status", "ok"), mh.get("status", "ok")],
                             key=lambda s: rank.get(s, 0))
        vmeter_down = vh.get("status") == "down"
        return JSONResponse(content=body, status_code=503 if vmeter_down else 200)

    _LANG_DIR = os.path.join("ui", "languages")

    @app.get("/api/languages")
    async def list_languages():
        """Available UI languages — scans ui/languages/*.json, so dropping a new
        file (copy en.json -> xx.json and translate) adds a language with no code change."""
        out = []
        try:
            for fn in sorted(os.listdir(_LANG_DIR)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                        meta = (json.load(f) or {}).get("_meta", {})
                    code = meta.get("code") or fn[:-5]
                    out.append({"code": code, "name": meta.get("name", code),
                                "nativeName": meta.get("nativeName", meta.get("name", code)),
                                "flag": meta.get("flag", "")})
                except Exception:  # noqa: BLE001
                    pass
        except FileNotFoundError:
            pass
        return {"languages": out, "default": "en"}

    @app.get("/api/languages/{code}")
    async def get_language(code: str):
        """Return one language file's translation map."""
        if not (code.isalpha() and code.islower() and 2 <= len(code) <= 8):
            raise HTTPException(status_code=400, detail="bad language code")
        path = os.path.join(_LANG_DIR, f"{code}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="unknown language")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e))

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

    @app.get("/api/history/registers")
    async def history_registers():
        """Registers with InfluxDB enabled — for the history view's picker.
        Also reports whether InfluxDB is actually enabled, so the UI can show a
        clear 'not configured' message instead of an empty/broken chart."""
        regs = [{"name": r.name, "label": getattr(r, "label", "") or r.name,
                 "unit": getattr(r, "unit", "")}
                for r in getattr(config, "selected_registers", [])
                if getattr(r, "influxdb_enabled", False)]
        influx_on = bool(influxdb_publisher and getattr(influxdb_publisher.config, "enabled", False))
        return {"registers": regs, "influx_enabled": influx_on}

    @app.get("/api/history")
    async def get_history(name: str = Query(...),
                          start: str = Query("-6h"), stop: str = Query("now()"),
                          every: str = Query("1m"), fn: str = Query("mean"),
                          measurement: Optional[str] = Query(None)):
        """Aggregated history for a register, read back from InfluxDB.
        fn='all' returns mean/min/max series (for a band)."""
        if influxdb_publisher is None or not influxdb_publisher.config.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB not enabled")
        # off the event loop: a slow/hung InfluxDB must not stall the whole API
        res = await asyncio.to_thread(influxdb_publisher.query_history,
                                      name, start, stop, every, fn, measurement)
        if "error" in res:
            err = res["error"]
            code = 503 if ("disabled" in err or "unavailable" in err) else 400
            raise HTTPException(status_code=code, detail=err)
        return res

    @app.get("/api/energy/monthly")
    async def energy_monthly(year: int = Query(...), month: int = Query(..., ge=1, le=12)):
        """Energy for a calendar month: import/export/reactive/apparent totals
        (deltas of the cumulative counters) + a per-day breakdown, from InfluxDB."""
        if influxdb_publisher is None or not influxdb_publisher.config.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB not enabled")
        regs = [
            {"name": "_WH_V[4]", "label": "Consumption (import)", "unit": "kWh", "div": 1000},
            {"name": "_WH_Z[4]", "label": "Injection (export)", "unit": "kWh", "div": 1000},
            {"name": "_QH[4]", "label": "Reactive", "unit": "kvarh", "div": 1000},
            {"name": "_WH_S[4]", "label": "Apparent", "unit": "kVAh", "div": 1000},
        ]
        res = await asyncio.to_thread(influxdb_publisher.energy_report, year, month, regs)
        if "error" in res:
            err = res["error"]
            code = 503 if ("disabled" in err or "unavailable" in err) else 400
            raise HTTPException(status_code=code, detail=err)
        return res

    @app.get("/api/virtual-meters")
    async def list_virtual_meters():
        """Configured virtual meters + live running status + served values."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            return {"instances": []}
        return {"instances": mgr.overview(), "port_range": mgr.port_info()}

    @app.get("/api/virtual-meters/templates")
    async def list_vm_templates():
        """Available meter templates (for the 'add instance' dropdown)."""
        mgr = getattr(app.state, "vmeter_manager", None)
        return {"templates": mgr.list_templates() if mgr else []}

    @app.post("/api/virtual-meters")
    async def add_virtual_meter(payload: dict = Body(...)):
        """Add a new virtual-meter instance from a template."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        try:
            res = mgr.add_instance(
                template_id=payload["template"], port=int(payload["port"]),
                unit_id=int(payload.get("unit_id", 1)),
                stale_after_s=float(payload.get("stale_after_s", 15)),
                enabled=bool(payload.get("enabled", False)))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"bad payload: {e}")
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    @app.delete("/api/virtual-meters/{template}")
    async def delete_virtual_meter(template: str):
        """Remove a virtual-meter instance."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.remove_instance(template)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @app.get("/api/virtual-meters/sources")
    async def vm_sources():
        """Live Janitza registers (for the editor source picker) + valid types."""
        mgr = getattr(app.state, "vmeter_manager", None)
        return {"sources": mgr.list_sources() if mgr else [],
                "types": mgr.valid_types() if mgr else [],
                "port_range": mgr.port_info() if mgr else None}

    @app.get("/api/virtual-meters/template/{template_id}")
    async def vm_get_template(template_id: str):
        """Full editor view of a template (per-register fields)."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.get_template(template_id)
        if "error" in res:
            raise HTTPException(status_code=404 if "unknown" in res["error"] else 400,
                                detail=res["error"])
        return res

    @app.put("/api/virtual-meters/template/{template_id}")
    async def vm_save_template(template_id: str, payload: dict = Body(...)):
        """Create or overwrite a template from the editor."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.save_template(template_id, payload)
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    @app.delete("/api/virtual-meters/template/{template_id}")
    async def vm_delete_template(template_id: str):
        """Delete a template file (refused while an instance uses it)."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.delete_template(template_id)
        if "error" in res:
            code = (404 if "unknown" in res["error"]
                    else 409 if "in use" in res["error"] else 400)
            raise HTTPException(status_code=code, detail=res["error"])
        return res

    @app.post("/api/virtual-meters/templates/import")
    async def vm_import_template(payload: dict = Body(...)):
        """Import a template from uploaded YAML (validated before save)."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.import_template(str(payload.get("yaml", "")), bool(payload.get("overwrite", False)))
        if "error" in res:
            raise HTTPException(status_code=409 if res.get("exists") else 400, detail=res["error"])
        return res

    @app.get("/api/virtual-meters/template/{template_id}/export")
    async def vm_export_template(template_id: str):
        """Export a template's raw YAML (for download / sharing)."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.export_template(template_id)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @app.get("/api/virtual-meters/{template}/stats")
    async def vm_stats(template: str, limit: int = Query(200, ge=1, le=1024)):
        """Live observability: query log (last 1024), counters, rate, per-register."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        return mgr.get_stats(template, limit)

    @app.get("/api/virtual-meters/{template}/decode")
    async def vm_decode(template: str, addr: int = Query(...), count: int = Query(1, ge=1, le=125)):
        """Decode a register range -> values + the source variable each maps to."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.decode_range(template, addr, count)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @app.post("/api/virtual-meters/{template}/toggle")
    async def toggle_virtual_meter(template: str, on: bool = Query(True)):
        """Enable/disable a virtual meter (persists + starts/stops live)."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.set_enabled(template, on)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @app.patch("/api/virtual-meters/{template}")
    async def edit_virtual_meter(template: str, payload: dict = Body(...)):
        """Edit an existing instance (port / unit_id / stale_after_s /
        update_interval_s — partial). Restarts the meter live if running."""
        mgr = getattr(app.state, "vmeter_manager", None)
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.update_instance(
            template_id=template,
            port=payload.get("port"), unit_id=payload.get("unit_id"),
            stale_after_s=payload.get("stale_after_s"),
            update_interval_s=payload.get("update_interval_s"))
        if "error" in res:
            code = 404 if "no instance" in res["error"] else 400
            raise HTTPException(status_code=code, detail=res["error"])
        return res

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
