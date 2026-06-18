# User Manual — Janitza UMG 512-PRO Monitor

🇬🇧 **English** | [🇷🇴 Română](MANUAL.ro.md)

A step-by-step guide: from a fresh install to monitoring, integrations, and
serving virtual meters. For the architecture deep-dive of the virtual-meter
engine see **[VIRTUAL-METER.md](VIRTUAL-METER.md)**.

> 🇬🇧 English. Localized versions welcome via PR.

## Contents
1. [What you need](#1-what-you-need)
2. [Install (Docker)](#2-install-docker)
3. [First configuration](#3-first-configuration)
4. [The Web UI, tab by tab](#4-the-web-ui-tab-by-tab)
5. [Virtual Meters — step by step](#5-virtual-meters--step-by-step)
6. [Home Assistant (MQTT)](#6-home-assistant-mqtt)
7. [InfluxDB & Grafana](#7-influxdb--grafana)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. What you need
- A **Janitza UMG 512-PRO** (or compatible UMG) reachable over the network with
  **Modbus TCP enabled** (default port 502). Note its IP.
- A host with **Docker + Docker Compose**.
- *(Optional)* an MQTT broker (for Home Assistant) and/or InfluxDB (for Grafana).

---

## 2. Install (Docker)

```bash
# 1) Get the code
git clone https://github.com/sm26449/janitza-monitor.git
cd janitza-monitor

# 2) Create your environment file
cp .env.example .env
#    edit .env — at minimum set MODBUS_HOST to your Janitza's IP (see step 3)

# 3) Start it
docker compose up -d

# 4) Open the UI
#    http://<host>:8080
```

That's it for a monitor-only setup. Logs: `docker compose logs -f`.

---

## 3. First configuration

Edit `.env` (or set the same vars in your compose). The essentials:

| Variable | What it is | Example |
|----------|-----------|---------|
| `MODBUS_HOST` | Janitza IP | `192.168.1.100` |
| `MODBUS_PORT` | Modbus TCP port | `502` |
| `MODBUS_UNIT_ID` | Modbus unit | `1` |
| `MQTT_BROKER` / `MQTT_PORT` | broker (optional) | `192.168.1.100` / `1883` |
| `INFLUXDB_URL` / `INFLUXDB_TOKEN` | InfluxDB (optional) | — |
| `UI_PORT` | Web UI port | `8080` |

Restart after editing: `docker compose up -d`. The **Modbus** dot in the UI
top-right turns green when it connects.

You can also configure most of this from the UI → **Config** tab (no restart for
register/poll changes — they hot-reload).

---

## 4. The Web UI, tab by tab

Open `http://<host>:8080`.

- **Dashboard** — live KPI cards + the values you pinned. Click *Customize* to
  choose cards; toggle card/table view.
- **Monitor** — drag any value from the left list onto the graph for a live,
  zoomable chart. Add several to compare.
- **Registers** — the full register table with current values; search/filter.
- **Config** — two sub-tabs: *Settings* (Modbus/MQTT/InfluxDB connection, UI) and
  *Registers* (pick which registers to poll, set poll groups, thresholds, units,
  dashboard widgets). Changes hot-reload.
- **Virtual Meters** — serve the live values as standard meters to other systems
  (see step 5).

The three dots top-right (Modbus / MQTT / InfluxDB) show connection health — click
one for details.

---

## 5. Virtual Meters — step by step

Goal: let another system (Victron ESS, a Fronius inverter, any SunSpec client)
read this one Janitza as the meter *it* expects.

> ⚠️ A virtual meter can feed a control loop. Do steps 5.1→5.3 (validate in
> parallel) before you ever make it a consumer's only meter.

**5.0 — Publish the ports (once).** In `docker-compose.yml` the meter port range
is published (default `1502-1512`, plus `502` for Fronius). Pick instance ports
inside that range. Widen the range + recreate the container if you need more.

**5.1 — Pick or create a template.** Go to **Virtual Meters → Templates**.
- Shipped: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
  (Fronius Smart Meter → DataManager), `fronius_sunspec_meter` (generic SunSpec).
- *New template*: define each register (address, type, scale, source). Source can
  be a live Janitza register, a constant, or a sum of registers.
- *Import*: drop in a `.yaml` someone shared (validated before saving).

**5.2 — Add an instance.** On the **Meters** sub-tab, choose the template, a free
port, unit id → **Add instance**. It starts **disabled**.

**5.3 — Validate in parallel.** Enable the instance (toggle). Point a *test*
consumer — or just open the **Logs** tab — at `host:port`. Watch the live query
log: you see exactly what the consumer reads, when, and what you return. Compare
the served values against your real meter. The **Stats** tab shows request rate,
errors, and which registers are read most.

**5.4 — Cut over.** Once you trust it, point the real consumer at the virtual
meter and remove its dedicated physical meter. The **freshness watchdog** is your
safety net: if the Janitza data goes stale, the meter stops responding so the
consumer's own grid-loss fail-safe engages.

**5.5 — Observe & export.** The **Logs**/**Stats** tabs keep the last 1024
requests + counters in RAM. **Export** a template (YAML) to share it or back it
up; the meter card (accordion) shows active client connections (ip:port).

---

## 6. Home Assistant (MQTT)

Set `MQTT_BROKER`/`MQTT_PORT` (and credentials) in `.env`, restart. The monitor
publishes **Home Assistant MQTT autodiscovery**, so entities appear automatically
under the device. A Last-Will topic marks the device offline if the monitor stops.
Pick which registers publish (and their topics) in **Config → Registers**.

---

## 7. InfluxDB & Grafana

Set `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` in `.env`.
Writes are batched with automatic retry/backoff and a NaN guard. Point Grafana at
the same bucket. Per-register measurement/tags are configurable in **Config →
Registers**. The optional compose profiles can start a local InfluxDB + Grafana
(see README).

---

## 8. Troubleshooting

| Symptom | Check |
|---------|-------|
| Modbus dot red | `MODBUS_HOST`/port correct? Janitza Modbus TCP enabled? firewall? |
| UI shows old version after update | hard-refresh the browser (the app bundle is cache-busted, but proxies can cache) |
| Virtual meter "stale / starting" | the Janitza source isn't fresh — check the Modbus connection; the watchdog won't serve stale data by design |
| Consumer can't reach a virtual meter | is the port inside the published compose range? reachable from the consumer's network? check the **Logs** tab for incoming reads |
| MQTT entities missing in HA | broker reachable? autodiscovery enabled? watch `docker compose logs` |
| InfluxDB "data lost" warning | InfluxDB URL/token/bucket correct? it retries 10× before giving up |

Still stuck? Open an issue — include `docker compose logs` and your (redacted)
config. See **[VIRTUAL-METER.md](VIRTUAL-METER.md)** for the engine internals and
how to add a new meter template.
