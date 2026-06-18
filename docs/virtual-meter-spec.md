# Virtual Meter Engine — design specification (foundation)

Status: **design / pre-implementation.** This is the "think before we build"
document. Nothing is coded until this schema is agreed — because the engine
feeds control-critical consumers (Victron ESS, Fronius export limiting) and a
wrong register/scale/sign silently mis-controls power. Build clean, design for
100% uptime, no security holes, validate at the end.

## 1. Purpose

Turn the one physical Janitza UMG 512-PRO (class 0.2S) into **one or more
virtual Modbus meters**, each presenting the live Janitza values in the exact
register map a given consumer expects — so a single high-accuracy meter
replaces the three we run today (Janitza + Fronius meter + Victron VE.Can):

- **EM24 (Modbus TCP)** → Victron Venus reads it natively as `com.victronenergy.grid`.
- **Fronius Smart Meter (Modbus TCP / SunSpec)** → the Fronius DataManager reads it.
- + VM-3P75CT, ET340, EM540, Eastron SDM630, **+ user-created templates**.

Everything is **template-driven and UI-editable** — no code change to support
a new meter; you author/import a template.

## 2. Concepts

- **Source values** — the live Janitza readings already held by
  `RegisterParser` / the selected-registers cache. Referenced by their stable
  field key (e.g. `power.active.total`, `voltage.l1_n`, `energy.active.consumed`).
- **Template** — a portable (YAML/JSON) description of one meter model: its
  transport, identification, and register map.
- **Virtual meter (instance)** — a running server = template + transport
  binding (port/unit) + source bindings + enabled flag.
- **Source binding** — how each output register is filled:
  `live(field)` · `const(value)` · `expr(formula)` (safe, sandboxed).

## 3. Template schema

```yaml
template:
  id: carlo_gavazzi_em24            # stable id
  name: "Carlo Gavazzi EM24 (3-phase, 4-wire)"
  kind: flat                        # flat | sunspec
  byte_order: big                   # word/byte order baseline; per-reg override allowed
  direction: import_positive        # import_positive | export_positive
  identification:                   # how the consumer auto-detects this model
    method: model_register          # model_register | sunspec_marker | mdns
    register: 0x000b
    value: 0x.....                  # the AV53 model code (from carlo_gavazzi.py)
  transport:
    type: tcp                       # tcp | udp (rtu later)
    port: 502
    unit_id: 1
    bind: "172.20.0.0/16"           # LAN/docker only — never 0.0.0.0 public
  writable: [0x1002]                # registers the consumer may write (PhaseConfig…)
  registers:
    - { addr: 0x0028, type: s32, order: little, scale: 0.1,  source: live(power.active.total) }
    - { addr: 0x0033, type: u16,                 scale: 0.1,  source: live(frequency) }
    - { addr: 0x0034, type: s32, order: little, scale: 0.1,  source: live(energy.active.consumed) }
    - { addr: 0x004e, type: s32, order: little, scale: 0.1,  source: live(energy.active.delivered) }
    - { addr: 0x0000, type: s32, order: little, scale: 0.1,  source: live(voltage.l1_n) }
    - { addr: 0x0012, type: s32, order: little, scale: 0.1,  source: live(power.active.l1) }
    - { addr: 0x000b, type: u16,                 source: const(0x....) }   # model id
    - { addr: 0x5000, type: string, length: 7,   source: const("JNZ001") } # serial
```

SunSpec kind adds a `sunspec:` block (base 40000, the `SunS` marker, model
ids 201/211/213, and **scale-factor registers** — a register whose value is the
exponent applied to others; the engine resolves `scale_ref: <addr>` instead of
a fixed `scale`):

```yaml
template:
  id: fronius_smart_meter
  kind: sunspec
  sunspec: { base: 40000, models: [1, 211] }     # common + 3-phase meter
  identification: { method: sunspec_marker }      # "SunS" @ base
  registers:
    - { addr: 40071, type: s16, scale_ref: 40076, source: live(power.active.total) }  # W + sunssf
    - ...
```

## 4. Engine architecture (in janitza-monitor)

```
RegisterParser / live cache ──▶ Encoder (inverse of RegisterParser:
   (Janitza field values)         value+type+order+scale → 16-bit regs)
                                       │
                          per-instance Datastore (contiguous block, gaps=0)
                                       │  atomic multi-reg update each cycle
                          ┌────────────┴────────────┐
                     TCP server                 UDP server (+mDNS)
                     (pymodbus)                 (VM-3P75CT)
                          │                          │
                    Victron / Fronius          Victron VM consumers
```

- New module `janitza/virtual_meter.py` (engine) + `janitza/encoder.py`
  (mirror of `RegisterParser`). Templates in `config/templates/*.yaml`;
  instances in `config/virtual_meters.yaml`.
- Integrated into `main.py` lifespan as an **independent task** — isolated
  from the MQTT/InfluxDB/UI paths.
- Multiple instances run concurrently (Janitza→Victron AND →Fronius at once).

## 5. Non-functional requirements (non-negotiable)

### Uptime (it's a control reference)
- The Modbus server runs isolated from the UI/MQTT/Influx tasks — a UI crash
  must never stop metering.
- **Freshness watchdog**: if the underlying Janitza read goes stale (>N s),
  the engine must NOT keep serving silently-stale values to ESS. Configurable
  policy per instance: (a) mark registers invalid / error code, or (b) stop
  responding → the consumer's own grid-meter-loss fail-safe triggers. Default =
  the safe one. Never feed stale data into a control loop.
- Config reload is hot + non-disruptive where possible; on failure it keeps
  the last-good config running (no crash-on-bad-edit).
- Atomic register updates (no word-tearing on s32/float reads).

### Security
- Bind servers to the **LAN/docker network only** — never `0.0.0.0`/public.
  Optional consumer-IP allowlist.
- Read-mostly: writes accepted ONLY for the template's declared `writable`
  registers, validated; everything else write → rejected.
- `expr` bindings use a **sandboxed evaluator** (simpleeval-style, no `eval`,
  no imports, no attribute access) — a template can never execute code.
- The configuration UI is **auth-protected** (it controls a control-critical
  device); template import is validated (schema + ranges + types) before it can
  be enabled.

### Safety / correctness
- **Validation before enable**: types, addresses (no overlaps), scale/sign,
  identification register present, required consumer registers covered.
- **Live preview / "what the consumer sees"**: decode each register back and
  show the value the consumer will read — catch scale/sign errors pre-cutover.
- **Parallel validation**: run the virtual meter alongside the existing real
  meter and compare per-phase P/V/I + energy + sign before removing the real
  one. Never cut over unvalidated.

## 6. Starter template library

Authoritative sources (no guessing): Venus `dbus-modbus-client`
(`carlo_gavazzi.py`, `victron_em.py`), the SunSpec model spec, the Fronius
Modbus/SunSpec docs + this repo's own `register_parser` (it already reads the
Fronius/Janitza maps), EVCC's template library as cross-reference.

Ship: EM24 (AV53), ET340, EM540, **SunSpec/Fronius Smart Meter**, VM-3P75CT,
Eastron SDM630.

## 7. Phasing & test plan

1. **Engine + encoder + 2 templates** (EM24 flat, Fronius-SM SunSpec), config
   files, multiple-instance. *Test:* unit-test the encoder (value↔reg round-trip
   per type/order/scale); conformance-test each template (a Modbus client reads
   it and decodes expected values); end-to-end **in parallel** with the real
   meters in a controlled window (Victron sees grid, Fronius sees its meter),
   compare readings, verify grid-loss fail-safe — **before** decommissioning
   anything.
2. **UI**: template editor + create-new + presets + live preview + validation +
   start/stop + import/export.
3. **Transports/extras**: UDP+mDNS (VM-3P75CT), RTU (Eastron/serial), more presets.

## 8. Decisions (confirmed 2026-06-17)

1. **Fronius consumer = CONFIRMED.** Janitza becomes the meter the **Fronius
   inverters read** (replaces the physical Fronius meter, for export limiting).
   So the engine consolidates all three: Victron grid (EM24) + Fronius meter
   (Fronius Smart Meter / SunSpec) + Janitza monitoring, on one device.
2. **Stale-data policy = STOP RESPONDING.** On Janitza staleness past the
   watchdog threshold, the virtual server STOPS answering → each consumer's own
   grid-meter-loss fail-safe triggers (Victron ESS + Fronius export-limit). We
   never serve silently-stale values into a control loop.
3. **EM24 variant = AV53** (3-phase 4-wire, 230/400 V) — model code taken from
   `carlo_gavazzi.py`.

## 9. Licensing gate (before any GitHub publish)

janitza-monitor's license must change from **MIT** to **free-for-personal /
non-commercial** (recommended: PolyForm Noncommercial 1.0.0) BEFORE the first
push that includes this engine. Build + test happen locally / in-stack; no
push until the license is swapped (LICENSE + both READMEs).
