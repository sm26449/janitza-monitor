# Janitza UMG 512-PRO Monitor

🇷🇴 **Română** | [🇬🇧 English](README.en.md)

Monitor profesional pentru analizoarele de calitate a energiei Janitza UMG 512-PRO. Citeste date prin Modbus TCP si le publica in MQTT si/sau InfluxDB.

## Caracteristici

- **Citire Modbus TCP** - Conectare directa la dispozitivul Janitza
- **Publicare MQTT** - Cu suport Home Assistant autodiscovery
- **Publicare InfluxDB** - Pentru stocare time-series
- **Mod "changed"** - Publica doar valorile modificate (reduce traficul)
- **Web UI profesional** - Dashboard, Monitor, Registers, Config
- **WebSocket real-time** - Actualizari live in UI
- **Hot-reload** - Modificari configuratie fara restart container
- **Configurare flexibila** - Topic-uri MQTT si tags InfluxDB per registru
- **Poll Groups** - Intervale diferite pentru diferite tipuri de date
- **Thresholds** - Color coding pentru valori (warning/danger)
- **Unit Scaling** - Conversie automata Wh→kWh, W→kW, VA→kVA pentru vizualizare clara
- **Gauge Widgets** - Min/max/culoare configurabile cu colorare bazata pe thresholds
- **pv-stack Integration** - Template serviciu pentru Docker Services Manager

## Instalare Rapida

### Cu Docker (recomandat)

```bash
# 1. Cloneaza repository
git clone https://github.com/sm26449/janitza-umg512-modbus-mqtt-ui.git
cd janitza-umg512-modbus-mqtt-ui

# 2. Configureaza environment
cp .env.example .env
nano .env  # Editeaza cu valorile tale

# 3. Configureaza registrii (optional - poti face din UI)
cp config/config.example.yaml config/config.yaml
cp config/selected_registers.example.json config/selected_registers.json

# 4. Porneste
docker-compose up -d

# 5. Acceseaza UI
# http://localhost:8080
```

### Cu InfluxDB si Grafana (optional)

```bash
# Porneste cu InfluxDB local
docker-compose --profile influxdb up -d

# Porneste cu Grafana
docker-compose --profile grafana up -d

# Porneste toate
docker-compose --profile influxdb --profile grafana up -d
```

## Configurare

### Fisierul .env

Copiaza `.env.example` in `.env` si editeaza:

```bash
# Modbus - Dispozitivul Janitza
MODBUS_HOST=192.168.1.100
MODBUS_PORT=502
MODBUS_UNIT_ID=1

# MQTT
MQTT_ENABLED=true
MQTT_BROKER=mqtt-broker
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=
MQTT_PREFIX=janitza/umg512
MQTT_PUBLISH_MODE=changed    # "changed" sau "all"

# InfluxDB
INFLUXDB_ENABLED=false
INFLUXDB_URL=http://influxdb:8086
INFLUXDB_TOKEN=your-token
INFLUXDB_ORG=your-org
INFLUXDB_BUCKET=janitza
INFLUXDB_PUBLISH_MODE=changed

# UI
UI_PORT=8080
```

### config/config.yaml

Configuratie YAML (poate fi editata si din UI - Settings):

```yaml
modbus:
  host: 192.168.1.100
  port: 502
  unit_id: 1
  timeout: 3
  retry_attempts: 3

mqtt:
  enabled: true
  broker: mqtt-broker
  port: 1883
  topic_prefix: "janitza/umg512"
  publish_mode: "changed"
  ha_discovery:
    enabled: true
    prefix: "homeassistant"
    device_name: "Janitza UMG 512-PRO"

influxdb:
  enabled: false
  url: "http://influxdb:8086"
  token: "your-token"
  org: "your-org"
  bucket: "janitza"
  publish_mode: "changed"

polling:
  groups:
    realtime:
      interval: 1
      description: "Real-time values"
    normal:
      interval: 5
      description: "Standard measurements"
    slow:
      interval: 60
      description: "Energy counters"
```

> **Nota:** Variabilele ENV au prioritate fata de config.yaml. In UI vei vedea warning cand ENV override-uri sunt active.

### config/selected_registers.json

Registrii selectati pentru monitorizare (se editeaza din UI - Registers):

```json
{
  "version": "1.0",
  "registers": [
    {
      "address": 19000,
      "name": "_G_ULN[0]",
      "label": "Tensiune L1-N",
      "unit": "V",
      "data_type": "float",
      "poll_group": "realtime",
      "mqtt": { "enabled": true, "topic": "voltage/l1_n" },
      "influxdb": { "enabled": true, "measurement": "voltage", "tags": {"phase": "L1"} },
      "ui": { "show_on_dashboard": true, "widget": "value" },
      "thresholds": {
        "enabled": true,
        "dangerLow": 200,
        "warningLow": 210,
        "warningHigh": 245,
        "dangerHigh": 253
      }
    }
  ],
  "poll_groups": {
    "realtime": { "interval": 1 },
    "normal": { "interval": 5 },
    "slow": { "interval": 60 }
  }
}
```

## Web UI

Acceseaza `http://localhost:8080`

### Dashboard

Vizualizare live a tuturor registrilor selectati cu widgets (value, gauge, chart), color coding bazat pe thresholds si view Cards/Table.

![Dashboard](screenshots/dashboard.png)

### Monitor

Grafic real-time cu multiple registre suprapuse. Drag & drop registre din sidebar, zoom/pan pe grafic, statistici min/max/avg.

![Monitor](screenshots/monitor.png)

### Registers

Browser pentru toti cei 4126 registri disponibili. Cautare, filtrare pe categorii, adaugare rapida la monitorizare cu configurare MQTT/InfluxDB/thresholds.

![Registers](screenshots/registers.png)

**Query on-demand** - Citire directa a oricarui registru Modbus cu afisare valoare, descriere, categorie si tip date.

![Query Register](screenshots/registers-query.png)

**Add Register** - Adaugare registru la monitorizare cu configurare completa: poll group, widget, MQTT topic, InfluxDB measurement, thresholds.

![Add Register](screenshots/registers-add.png)

### Config - Settings

Configurare Modbus, MQTT si InfluxDB direct din interfata. Hot-reload cu buton "Apply Configuration" pentru reconectare fara restart. Warning pentru ENV overrides active.

![Config Settings](screenshots/config-settings.png)

### Config - Registers

Lista registrilor monitorizati cu filtrare pe categorii. Editare label, poll group, widget, MQTT topic, InfluxDB measurement si thresholds per registru.

![Config Registers](screenshots/config-registers.png)

**Edit Register** - Configurare detaliata per registru: widget type (value/gauge/chart), MQTT topic, InfluxDB measurement/tags, color thresholds cu auto-detect.

![Edit Register](screenshots/config-edit-register.png)

## API Endpoints

| Endpoint | Metoda | Descriere |
|----------|--------|-----------|
| `/` | GET | Web UI |
| `/api/status` | GET | Status sistem (Modbus, MQTT, InfluxDB) |
| `/api/config` | GET | Configuratie curenta |
| `/api/registers/all` | GET | Toti registrii disponibili |
| `/api/registers/selected` | GET/POST | Registrii monitorizati |
| `/api/values` | GET | Valori curente |
| `/api/values/{address}` | GET | Valoare pentru adresa specifica |
| `/api/query/register` | POST | Query on-demand |
| `/api/query/batch` | POST | Query batch |
| `/api/search?q=...` | GET | Cauta registri |
| `/api/config/modbus` | GET/POST | Config Modbus |
| `/api/config/mqtt` | GET/POST | Config MQTT |
| `/api/config/influxdb` | GET/POST | Config InfluxDB |
| `/api/config/apply` | POST | Aplica configuratie (reconnect) |
| `/api/config/reload-registers` | POST | Reload registri |
| `/ws` | WebSocket | Stream real-time |

## Home Assistant Integration

Cu `ha_discovery.enabled: true`, senzori se creeaza automat in Home Assistant.

Topic-uri MQTT:
- `janitza/umg512/voltage/l1_n` - valoare registru
- `janitza/umg512/status` - online/offline
- `homeassistant/sensor/janitza/...` - autodiscovery configs

## Publish Mode: changed vs all

| Mode | Descriere | Use case |
|------|-----------|----------|
| `changed` | Publica doar cand valoarea se schimba | Reduce trafic, ideal pentru MQTT |
| `all` | Publica toate citirile | Necesar pentru time-series complete |

In UI statusul "Skipped" arata cate mesaje nu au fost publicate (valori neschimbate).

## Structura Proiect

```
janitza-umg512-modbus-mqtt-ui/
├── config/                    # Fisiere configurare
│   ├── config.example.yaml
│   └── selected_registers.example.json
├── docs/                      # Documentatie Modbus
│   ├── modbus_data.json      # 4126 registri structurati
│   └── extract_pdf.py        # Script extractie din PDF
├── janitza/                   # Pachet Python
│   ├── __init__.py
│   ├── config.py             # Loader configuratie
│   ├── modbus_client.py      # Client Modbus TCP
│   ├── mqtt_publisher.py     # Publisher MQTT
│   ├── influxdb_publisher.py # Publisher InfluxDB
│   ├── register_parser.py    # Parser tipuri date
│   └── api.py                # REST API + WebSocket
├── ui/                        # Frontend
│   ├── templates/
│   │   ├── index.html
│   │   ├── base.html
│   │   └── partials/
│   ├── css/
│   │   ├── base.css
│   │   ├── dashboard.css
│   │   ├── monitor.css
│   │   ├── registers.css
│   │   └── config.css
│   └── js/
│       └── app.js
├── main.py                    # Entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example               # Template environment
├── CHANGELOG.md
└── README.md
```

## Adrese Frecvent Utilizate

| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19000 | _G_ULN[0] | V | Tensiune L1-N |
| 19002 | _G_ULN[1] | V | Tensiune L2-N |
| 19004 | _G_ULN[2] | V | Tensiune L3-N |
| 19006 | _G_ULL[0] | V | Tensiune L1-L2 |
| 19008 | _G_ULL[1] | V | Tensiune L2-L3 |
| 19010 | _G_ULL[2] | V | Tensiune L3-L1 |
| 19012 | _G_ILN[0] | A | Curent L1 |
| 19014 | _G_ILN[1] | A | Curent L2 |
| 19016 | _G_ILN[2] | A | Curent L3 |
| 19026 | _G_P_SUM3 | W | Putere activa totala |
| 19034 | _G_S_SUM3 | VA | Putere aparenta totala |
| 19042 | _G_Q_SUM3 | var | Putere reactiva totala |
| 19050 | _G_FREQ | Hz | Frecventa |
| 19052 | _G_COSPHI | - | Factor putere |
| 19060 | _G_WH_SUML13 | Wh | Energie activa totala |

Vezi `docs/modbus_data.json` pentru lista completa cu 4126 registri.

## Integrare pv-stack (Docker Services Manager)

Pentru deploy in stack-ul pv-stack cu mosquitto si influxdb partajate:

```bash
# Copiaza fisierele in templates
cp -r janitza-umg512-modbus-mqtt-ui/* docker-setup/templates/janitza-monitor/

# Deploy prin docker-compose
docker compose -f docker-compose.pv-stack.yml build janitza-monitor
docker compose -f docker-compose.pv-stack.yml up -d janitza-monitor
```

Variabilele se configureaza in `.env` cu prefix `JANITZA_`:

```bash
JANITZA_MODBUS_HOST=192.168.1.100
JANITZA_MQTT_BROKER=mosquitto
JANITZA_INFLUXDB_ENABLED=true
JANITZA_INFLUXDB_URL=http://influxdb:8086
JANITZA_INFLUXDB_BUCKET=janitza
JANITZA_UI_PORT=8080
```

Vezi `service.yaml` pentru lista completa de variabile si dependinte.

## Dezvoltare

```bash
# Cloneaza
git clone https://github.com/sm26449/janitza-umg512-modbus-mqtt-ui.git
cd janitza-umg512-modbus-mqtt-ui

# Virtual environment
python3 -m venv venv
source venv/bin/activate

# Instaleaza dependente
pip install -r requirements.txt

# Ruleaza local
python main.py --debug

# Rebuild Docker
docker-compose up --build -d

# Vezi logs
docker-compose logs -f
```

## Troubleshooting

### Modbus nu se conecteaza
- Verifica IP-ul dispozitivului Janitza
- Asigura-te ca portul 502 este accesibil
- Verifica Unit ID (default: 1)

### MQTT nu publica
- Verifica broker-ul este accesibil
- Verifica username/password
- Check logs: `docker-compose logs -f | grep MQTT`

### InfluxDB skipped messages
- Normal pentru `publish_mode: changed` - valori neschimbate nu se scriu
- Schimba la `publish_mode: all` daca vrei toate datele

### ENV override warning in UI
- Variabilele ENV au prioritate fata de config.yaml
- Sterge variabila din .env daca vrei sa folosesti valoarea din UI

## Contributing

Found a bug or have a feature request? Please open an issue on [GitHub Issues](https://github.com/sm26449/janitza-umg512-modbus-mqtt-ui/issues).

## Authors

**Stefan M** - [sm26449@diysolar.ro](mailto:sm26449@diysolar.ro)

**Claude** (Anthropic) - Pair programming partner

## License

MIT License - Free and open source software.

Copyright (c) 2024-2026 Stefan M <sm26449@diysolar.ro>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

**Disclaimer**: This software is provided "as is", without warranty of any kind. Use at your own risk when monitoring critical energy systems.
