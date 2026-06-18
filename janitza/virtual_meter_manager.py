"""Wires virtual meters into the running Janitza monitor.

Reads config/virtual_meters.yaml (which templates to run + their ports), builds
a value provider over the live ``current_values`` cache, and runs one
VirtualMeter per enabled instance. Isolated from the UI/MQTT/InfluxDB paths.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .encoder import RegisterEncoder
from .virtual_meter import VirtualMeter, _parse_source, load_template

logger = logging.getLogger(__name__)

# Filename-safe template id (also blocks path traversal).
_ID_RE = re.compile(r"^[a-z0-9_]+$")
# Types the encoder can emit (mirrors RegisterEncoder/RegisterParser + string).
_VALID_TYPES = ["int16", "uint16", "int32", "uint32", "int64", "uint64",
                "float", "float32", "double", "string"]


def make_provider(current_values: dict):
    """Return provider(name) -> (value, unix_ts) reading the live cache.

    current_values is keyed by Janitza register ADDRESS; each entry carries
    'name', 'value', 'timestamp' (ISO). Sources are bound by register name.
    """
    def provider(name: str) -> Optional[tuple]:
        for info in current_values.values():
            if not isinstance(info, dict) or info.get("name") != name:
                continue
            value = info.get("value")
            if value is None:
                return None
            ts = info.get("timestamp")
            if not ts:
                return value, None          # no timestamp → don't fabricate freshness
            try:
                return value, datetime.fromisoformat(ts).timestamp()
            except Exception:  # noqa: BLE001
                return value, None           # unparseable → treat as not-fresh (fail-safe)
        return None
    return provider


class VirtualMeterManager:
    def __init__(self, current_values: dict,
                 config_path: str = "config/virtual_meters.yaml",
                 templates_dir: str = "config/templates",
                 mqtt_publisher=None):
        self.current_values = current_values
        self.config_path = Path(config_path)
        self.templates_dir = Path(templates_dir)
        self.meters: list[VirtualMeter] = []
        self.mqtt_publisher = mqtt_publisher        # optional: publish state to MQTT
        self._states_stop = threading.Event()
        # Published host port range (docker-compose publishes this once). Instance
        # ports are constrained to it so an added meter is always LAN-reachable.
        self.port_start = self._env_int("VMETER_PORT_START", 1502)
        self.port_end = self._env_int("VMETER_PORT_END", 1512)

    # ── state → MQTT (so alertd rules can monitor the meters) ─────────────
    def _publish_states(self) -> None:
        """Publish each configured meter's health to MQTT (retained) so external
        monitors (e.g. alertd) can alert on a meter going down / stale / erroring."""
        pub = self.mqtt_publisher
        if pub is None:
            return
        # iterate a snapshot of the live meters (no per-tick file I/O, no race
        # with API mutations of self.meters)
        for vm in list(self.meters):
            try:
                st = vm.status()
                running = bool(st.get("running"))
                payload = {
                    "id": st.get("id"), "name": st.get("name"), "port": st.get("port"),
                    "unit_id": vm.t.transport.get("unit_id", 1),
                    "enabled": True, "running": running,
                    "state": "listening" if running else "stale",
                    "connections": st.get("conn_count", 0),
                    "requests": st.get("requests", 0), "errors": st.get("errors", 0),
                    "last_fresh": st.get("last_fresh"), "ts": int(time.time()),
                }
                pub.publish_state(f"vmeter/{st.get('id')}/state", json.dumps(payload))
            except Exception as e:  # noqa: BLE001
                logger.debug("vmeter state publish failed: %s", e)

    def start_state_publisher(self, interval: float = 10.0) -> None:
        """Background thread that publishes meter states to MQTT every `interval`s.
        Idempotent — a second call is a no-op while a publisher is already live."""
        if self.mqtt_publisher is None:
            return
        if getattr(self, "_state_thread", None) and self._state_thread.is_alive():
            return                                    # already running
        self._states_stop.clear()

        def _loop():
            while not self._states_stop.wait(interval):
                try:
                    self._publish_states()
                except Exception as e:  # noqa: BLE001
                    logger.warning("vmeter state publisher error: %s", e)
        self._state_thread = threading.Thread(target=_loop, daemon=True, name="vmeter-state-pub")
        self._state_thread.start()
        logger.info("virtual-meter state publisher started (every %ss)", interval)

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def port_info(self) -> dict:
        """Published range + which ports are taken + the next free one."""
        used = sorted({int(i["port"]) for i in self._load_cfg().get("instances", [])
                       if i.get("port") is not None})
        free = [p for p in range(self.port_start, self.port_end + 1) if p not in used]
        return {"start": self.port_start, "end": self.port_end, "used": used,
                "free": free, "next_free": (free[0] if free else None)}

    def _port_in_range(self, port: int) -> bool:
        return self.port_start <= port <= self.port_end

    def start_all(self) -> None:
        if not self.config_path.exists():
            logger.info("virtual meters: no %s — disabled", self.config_path)
            return
        try:
            cfg = yaml.safe_load(self.config_path.read_text()) or {}
        except Exception as e:  # noqa: BLE001
            logger.error("virtual meters: bad config %s: %s", self.config_path, e)
            return
        for inst in cfg.get("instances", []):
            if not inst.get("enabled", True):
                continue
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                logger.error("virtual meter instance %s failed to start: %s",
                             inst.get("template"), e)

    def _load_cfg(self) -> dict:
        if not self.config_path.exists():
            return {"instances": []}
        try:
            return yaml.safe_load(self.config_path.read_text()) or {"instances": []}
        except Exception:  # noqa: BLE001
            return {"instances": []}

    def _save_cfg(self, cfg: dict) -> None:
        # atomic write — this file decides which meters run (can feed an ESS), so
        # never leave it half-written or let a concurrent reader see a torn file.
        tmp = self.config_path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(cfg, sort_keys=False))
        os.replace(tmp, self.config_path)

    def overview(self) -> list[dict]:
        """All configured instances merged with live running status + preview."""
        running = {vm.t.id: vm for vm in self.meters}
        out = []
        for inst in self._load_cfg().get("instances", []):
            tid = inst.get("template")
            vm = running.get(tid)
            name = tid
            if vm:
                name = vm.t.name
            else:                                    # load template for the friendly name
                try:
                    name = load_template(str(self.templates_dir / f"{tid}.yaml")).name
                except Exception:  # noqa: BLE001
                    pass
            row = {"template": tid, "enabled": bool(inst.get("enabled", True)),
                   "port": inst.get("port"), "unit_id": inst.get("unit_id", 1),
                   "running": vm is not None and vm._running,
                   "name": name,
                   "preview": vm.preview() if vm else {}}
            if vm:
                row.update(vm.status())
            out.append(row)
        return out

    def list_templates(self) -> list[dict]:
        """Available templates (config/templates/*.yaml) for the UI dropdown."""
        out = []
        for p in sorted(self.templates_dir.glob("*.yaml")):
            try:
                t = load_template(str(p))
                out.append({"id": t.id, "name": t.name, "kind": t.kind,
                            "registers": len(t.registers)})
            except Exception:  # noqa: BLE001
                pass
        return out

    # ── template-level operations (the UI editor) ─────────────────────────
    @staticmethod
    def valid_types() -> list[str]:
        return list(_VALID_TYPES)

    def list_sources(self) -> list[dict]:
        """Janitza register names available as live sources (name/label/unit/value)."""
        seen: dict[str, dict] = {}
        for info in self.current_values.values():
            if not isinstance(info, dict):
                continue
            name = info.get("name")
            if not name or name in seen:
                continue
            seen[name] = {"name": name, "label": info.get("label", ""),
                          "unit": info.get("unit", ""), "value": info.get("value")}
        return sorted(seen.values(), key=lambda s: s["name"])

    def _template_path(self, template_id: str) -> Optional[Path]:
        """Resolve a template id to its YAML path, guarding against traversal."""
        if not template_id or not _ID_RE.match(template_id):
            return None
        p = (self.templates_dir / f"{template_id}.yaml").resolve()
        try:
            p.relative_to(self.templates_dir.resolve())
        except ValueError:
            return None
        return p

    def get_template(self, template_id: str) -> dict:
        """Full editor-shaped view of a template (raw fields per register)."""
        path = self._template_path(template_id)
        if path is None:
            return {"error": "invalid template id"}
        if not path.exists():
            return {"error": f"unknown template {template_id}"}
        try:
            d = (yaml.safe_load(path.read_text()) or {})["template"]
        except Exception as e:  # noqa: BLE001
            return {"error": f"cannot parse template: {e}"}
        byte_order = d.get("byte_order", "big")
        tr = d.get("transport", {}) or {}
        regs = []
        for r in d.get("registers", []):
            kind, src = _parse_source(r)
            order = r.get("order", byte_order)
            regs.append({"addr": int(r["addr"]), "type": r["type"],
                         "scale": float(r.get("scale", 1)),
                         "order": "inherit" if order == byte_order else order,
                         "source_kind": kind, "source": src,
                         "length": int(r.get("length", 1)), "note": r.get("note", "")})
        cfg = self._load_cfg().get("instances", [])
        return {"id": d["id"], "name": d.get("name", d["id"]),
                "kind": d.get("kind", "flat"), "byte_order": byte_order,
                "transport": {"type": tr.get("type", "tcp"), "port": tr.get("port", 1502),
                              "unit_id": tr.get("unit_id", 1), "bind": tr.get("bind", "0.0.0.0")},
                "registers": regs,
                "running": any(vm.t.id == template_id and vm._running for vm in self.meters),
                "in_use": any(i.get("template") == template_id for i in cfg)}

    def _normalize_registers(self, raw_regs: list, byte_order: str):
        """Validate + coerce editor register rows. Returns (registers, error)."""
        norm, spans = [], []
        for idx, r in enumerate(raw_regs):
            tag = f"register #{idx + 1}"
            a = r.get("addr")
            try:
                addr = int(a, 0) if isinstance(a, str) else int(a)
            except (TypeError, ValueError):
                return None, f"{tag}: bad address {a!r}"
            if not (0 <= addr <= 0xffff):
                return None, f"{tag}: address out of range (0..65535)"
            typ = str(r.get("type", "")).lower()
            if typ not in _VALID_TYPES:
                return None, f"{tag}: invalid type {typ!r}"
            kind = r.get("source_kind", "const")
            if kind not in ("const", "const_str", "live"):
                return None, f"{tag}: invalid source kind {kind!r}"
            src = r.get("source")
            if kind == "live":
                if not (isinstance(src, str) and src.strip()):
                    return None, f"{tag}: live source register name required"
                src = src.strip()
            elif kind == "const_str":
                src = "" if src is None else str(src)
            else:  # const number
                try:
                    f = float(src)
                except (TypeError, ValueError):
                    return None, f"{tag}: const must be a number"
                src = int(f) if f == int(f) else f
            try:
                scale = float(r.get("scale", 1)) or 1.0
            except (TypeError, ValueError):
                return None, f"{tag}: bad scale"
            order = r.get("order", "inherit")
            if order not in ("inherit", "big", "little"):
                order = "inherit"
            length = max(1, int(r.get("length", 1) or 1))
            count = length if typ == "string" else RegisterEncoder.REGISTER_COUNTS.get(typ, 2)
            spans.append((addr, addr + count, idx))
            norm.append({"addr": addr, "type": typ, "scale": scale, "order": order,
                         "source_kind": kind, "source": src, "length": length,
                         "note": (r.get("note") or "").strip()})
        spans.sort()
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1]:
                return None, (f"registers overlap: 0x{spans[i][0]:04x} starts inside "
                              f"0x{spans[i - 1][0]:04x}'s range")
        return norm, None

    def save_template(self, template_id: str, payload: dict) -> dict:
        """Create or overwrite a template YAML (atomic) + reload a live instance."""
        path = self._template_path(template_id)
        if path is None:
            return {"error": "invalid template id (use a-z 0-9 _)"}
        name = (payload.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}
        byte_order = payload.get("byte_order", "big")
        if byte_order not in ("big", "little"):
            return {"error": "byte_order must be big or little"}
        try:
            port, unit_id = int(payload.get("port", 1502)), int(payload.get("unit_id", 1))
        except (TypeError, ValueError):
            return {"error": "port/unit must be integers"}
        if not (1 <= port <= 65535):
            return {"error": "port must be 1..65535"}
        if not (0 <= unit_id <= 255):
            return {"error": "unit must be 0..255"}
        bind = (payload.get("bind") or "0.0.0.0").strip()
        raw_regs = payload.get("registers") or []
        if not raw_regs:
            return {"error": "at least one register is required"}
        norm, err = self._normalize_registers(raw_regs, byte_order)
        if err:
            return {"error": err}
        text = self._dump_template(template_id, name, byte_order,
                                   {"type": "tcp", "port": port, "unit_id": unit_id,
                                    "bind": bind}, norm)
        try:
            yaml.safe_load(text)                       # belt-and-braces: must reparse
        except Exception as e:  # noqa: BLE001
            return {"error": f"internal: produced invalid YAML: {e}"}
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)                          # atomic — never a torn template
        reloaded = self._reload_instance(template_id)
        return {"saved": True, "id": template_id, "registers": len(norm), "reloaded": reloaded}

    def delete_template(self, template_id: str) -> dict:
        path = self._template_path(template_id)
        if path is None:
            return {"error": "invalid template id"}
        if not path.exists():
            return {"error": f"unknown template {template_id}"}
        if any(i.get("template") == template_id for i in self._load_cfg().get("instances", [])):
            return {"error": "template in use by an instance — remove the instance first"}
        path.unlink()
        return {"deleted": True, "id": template_id}

    # ── observability + portability ──────────────────────────────────────
    def get_stats(self, template_id: str, limit: int = 200) -> dict:
        """Live in-RAM observability snapshot (query log + counters + rate)."""
        vm = next((m for m in self.meters if m.t.id == template_id), None)
        if vm is None:
            return {"error": "meter not running", "running": False, "id": template_id}
        snap = vm.stats.snapshot(limit)
        snap.update({"running": bool(vm._running), "id": template_id,
                     "name": vm.t.name, "port": vm.t.transport.get("port"),
                     "unit_id": vm.t.transport.get("unit_id", 1)})
        return snap

    def export_template(self, template_id: str) -> dict:
        """Return the raw template YAML for download."""
        path = self._template_path(template_id)
        if path is None or not path.exists():
            return {"error": "unknown template"}
        return {"id": template_id, "filename": f"{template_id}.yaml", "yaml": path.read_text()}

    def import_template(self, text: str, overwrite: bool = False) -> dict:
        """Validate (parse + structural load) then atomically save an uploaded YAML."""
        if not text or not text.strip():
            return {"error": "empty file"}
        try:
            doc = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001
            return {"error": f"invalid YAML: {e}"}
        if not isinstance(doc, dict) or "template" not in doc:
            return {"error": "not a template (missing 'template:' root)"}
        tid = str((doc.get("template") or {}).get("id", "")).strip()
        path = self._template_path(tid)
        if path is None:
            return {"error": "invalid template id (use a-z 0-9 _)"}
        if path.exists() and not overwrite:
            return {"error": f"template '{tid}' already exists", "exists": True, "id": tid}
        # full structural validation: must load as a Template with >= 1 register
        tmpname = None
        try:
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                tf.write(text)
                tmpname = tf.name
            t = load_template(tmpname)
        except Exception as e:  # noqa: BLE001
            return {"error": f"template failed validation: {e}"}
        finally:
            if tmpname:
                try:
                    os.unlink(tmpname)
                except OSError:
                    pass
        if not t.registers:
            return {"error": "template has no registers"}
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)                          # atomic
        return {"imported": True, "id": tid, "name": t.name, "registers": len(t.registers)}

    def _reload_instance(self, template_id: str) -> bool:
        """If a live instance runs this template, restart it to pick up edits."""
        running = [m for m in self.meters if m.t.id == template_id]
        if not running:
            return False
        inst = next((i for i in self._load_cfg().get("instances", [])
                     if i.get("template") == template_id), None)
        for vm in running:
            vm.stop()
            self.meters.remove(vm)
        if inst and inst.get("enabled"):
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                logger.error("reload %s failed to restart: %s", template_id, e)
        return True

    @staticmethod
    def _dump_template(tid: str, name: str, byte_order: str, transport: dict,
                       regs: list[dict]) -> str:
        """Render a clean, human-readable template YAML (hex addrs, one reg/line)."""
        lines = ["# Managed by the Virtual Meter template editor — UI saves overwrite this file.",
                 "template:", f"  id: {tid}", f'  name: "{name}"', "  kind: flat",
                 f"  byte_order: {byte_order}",
                 f'  transport: {{ type: {transport["type"]}, port: {transport["port"]}, '
                 f'unit_id: {transport["unit_id"]}, bind: "{transport["bind"]}" }}',
                 "  registers:"]
        for r in regs:
            parts = [f"addr: 0x{r['addr']:04x}", f"type: {r['type']}"]
            if r["type"] != "string" and abs(r["scale"] - 1.0) > 1e-12:
                parts.append(f"scale: {r['scale']:g}")
            if r["order"] not in ("inherit", byte_order):
                parts.append(f"order: {r['order']}")
            if r["type"] == "string":
                parts.append(f"length: {max(1, r['length'])}")
            if r["source_kind"] == "live":
                parts.append(f'source: {{ live: "{r["source"]}" }}')
            elif r["source_kind"] == "const_str":
                parts.append(f'source: {{ const_str: "{r["source"]}" }}')
            else:
                parts.append(f'source: {{ const: {r["source"]} }}')
            if r["note"]:
                parts.append(f'note: "{r["note"].replace(chr(34), chr(39))}"')
            lines.append("    - { " + ", ".join(parts) + " }")
        return "\n".join(lines) + "\n"

    def add_instance(self, template_id: str, port: int, unit_id: int = 1,
                     stale_after_s: float = 15.0, enabled: bool = False) -> dict:
        """Add a new virtual-meter instance (persist + optionally start)."""
        tmpl_path = self.templates_dir / f"{template_id}.yaml"
        if not tmpl_path.exists():
            return {"error": f"unknown template {template_id}"}
        cfg = self._load_cfg()
        cfg.setdefault("instances", [])
        if any(i.get("template") == template_id for i in cfg["instances"]):
            return {"error": f"instance for {template_id} already exists"}
        port = int(port)
        if not self._port_in_range(port):
            return {"error": f"port {port} is outside the published range "
                             f"{self.port_start}-{self.port_end} — pick a free port in "
                             f"range, or widen VMETER_PORT_END and recreate the container"}
        if any(int(i.get("port", -1)) == port for i in cfg["instances"]):
            return {"error": f"port {port} is already used by another instance"}
        inst = {"template": template_id, "enabled": bool(enabled),
                "port": port, "unit_id": int(unit_id),
                "stale_after_s": float(stale_after_s)}
        cfg["instances"].append(inst)
        self._save_cfg(cfg)
        if enabled:
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                return {"error": f"added but failed to start: {e}"}
        return {"template": template_id, "added": True, "enabled": bool(enabled)}

    def remove_instance(self, template_id: str) -> dict:
        """Stop + remove a virtual-meter instance."""
        cfg = self._load_cfg()
        before = len(cfg.get("instances", []))
        cfg["instances"] = [i for i in cfg.get("instances", []) if i.get("template") != template_id]
        if len(cfg["instances"]) == before:
            return {"error": f"no instance for {template_id}"}
        self._save_cfg(cfg)
        for vm in [m for m in self.meters if m.t.id == template_id]:
            vm.stop()
            self.meters.remove(vm)
        return {"template": template_id, "removed": True}

    def set_enabled(self, template_id: str, on: bool) -> dict:
        """Persist enabled flag + start/stop the instance live."""
        cfg = self._load_cfg()
        inst = next((i for i in cfg.get("instances", []) if i.get("template") == template_id), None)
        if inst is None:
            return {"error": f"no instance for template {template_id}"}
        inst["enabled"] = bool(on)
        self._save_cfg(cfg)
        running = {vm.t.id: vm for vm in self.meters}
        if on and template_id not in running:
            self._start_one(inst)
        elif not on and template_id in running:
            vm = running[template_id]
            vm.stop()
            self.meters.remove(vm)
        return {"template": template_id, "enabled": bool(on)}

    def _start_one(self, inst: dict) -> None:
        provider = make_provider(self.current_values)
        tmpl_path = self.templates_dir / f"{inst['template']}.yaml"
        template = load_template(str(tmpl_path))
        if "port" in inst:
            template.transport["port"] = int(inst["port"])
        if "unit_id" in inst:
            template.transport["unit_id"] = int(inst["unit_id"])
        if "bind" in inst:
            template.transport["bind"] = inst["bind"]
        vm = VirtualMeter(template, provider,
                          stale_after_s=float(inst.get("stale_after_s", 15)),
                          update_interval_s=float(inst.get("update_interval_s", 1.0)),
                          debug_reads=bool(inst.get("debug_reads", False)))
        vm.start()
        self.meters.append(vm)
        logger.info("virtual meter '%s' started (port=%s)", template.id,
                    template.transport.get("port"))

    def stop_all(self) -> None:
        self._states_stop.set()                       # stop the MQTT state publisher
        for vm in self.meters:
            try:
                vm.stop()
            except Exception:  # noqa: BLE001
                pass
        self.meters.clear()

    def status(self) -> list[dict]:
        return [vm.status() for vm in self.meters]
