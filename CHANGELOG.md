# Changelog

Toate modificarile notabile ale proiectului sunt documentate in acest fisier.

## [1.4.0] - 2026-03-19

### Adaugat
- **Unit scaling automat** - Conversie automata Wh→kWh→MWh, W→kW→MW, VA→kVA, var→kvar pentru vizualizare mai clara
- **Gauge Options in UI** - Campuri Min, Max si Color in modalul Edit/Add register, vizibile doar pentru widget-ul Gauge
- **Auto-derive gauge range** - Min/Max se calculeaza automat din thresholds daca nu sunt setate explicit (±15% margine)
- **Gauge threshold colors** - Arcul gauge-ului isi schimba culoarea bazat pe zonele threshold (normal/warning/danger)
- **Screenshots documentatie** - 9 screenshots dark mode pentru toate paginile si modalele UI

### Fixed
- **Widget type change** - Schimbarea tipului de widget (value→gauge) se reflecta instant pe dashboard
- **Auto-save la edit** - Salvarea modificarilor din edit modal triggereaza auto-save pe server si re-render dashboard
- **Auto-reload registre** - Salvarea registrelor din UI reincarca automat pollerii Modbus, MQTT si InfluxDB
- Registrele adaugate din UI nu apareau pe dashboard fara restart container

## [1.3.0] - 2026-03-19

### Adaugat
- **pv-stack integration** - Template `service.yaml` pentru deploy in docker-setup cu dependinte mosquitto/influxdb
- Auto-republish Home Assistant discovery la modificarea registrelor

## [1.2.0] - 2026-01-08

### Adaugat
- **Settings UI** - Configurare Modbus, MQTT, InfluxDB direct din interfata web
- **Hot-reload** - Buton "Apply Configuration" pentru reconectare servicii fara restart
- **ENV override warnings** - Afisare warning in UI cand variabilele ENV suprascriu config.yaml
- **.env file support** - Variabile environment externalizate in fisier .env
- **.env.example** - Template pentru configurare rapida
- **Status hints** - Explicatii pentru mesajele "skipped" in MQTT/InfluxDB status
- **Publish mode display** - Afisare mod publicare in status modals

### Modificat
- docker-compose.yml foloseste acum variabile din .env
- Structura CSS modulara (base, dashboard, monitor, registers, config)
- README.md actualizat cu instructiuni complete de instalare si configurare

### Sters
- ui/index.html (inlocuit cu ui/templates/index.html)
- ui/css/styles.css (inlocuit cu fisiere CSS modulare)

### Fixed
- InfluxDB publisher nu se reconecta dupa enable din UI
- Campuri status InfluxDB (writes_total, writes_failed, writes_skipped)

## [1.1.0] - 2026-01-07

### Adaugat
- **Thresholds per registru** - Color coding pentru valori (danger/warning/normal/success)
- **Threshold templates** - Auto-fill bazat pe tipul de masurare (voltage, frequency, etc.)
- **Dashboard table view** - Vizualizare alternativa tip tabel
- **Monitor page** - Grafic real-time cu multiple registre suprapuse
- **Zoom & Pan** - Control grafic in pagina Monitor
- **Drag & drop** - Adaugare registre in Monitor prin drag & drop

### Modificat
- Structura CSS refactorizata in module separate
- Imbunatatiri performanta pentru liste mari de registri

## [1.0.0] - 2026-01-05

### Adaugat
- **Modbus TCP client** - Conectare la dispozitive Janitza UMG 512-PRO
- **MQTT publisher** - Publicare valori cu Home Assistant autodiscovery
- **InfluxDB publisher** - Stocare time-series
- **Publish mode "changed"** - Publica doar valorile modificate
- **Web UI** - Dashboard, Registers browser, Query on-demand
- **WebSocket** - Actualizari real-time in UI
- **Poll Groups** - Intervale diferite (realtime: 1s, normal: 5s, slow: 60s)
- **REST API** - Endpoints pentru configurare si query
- **Docker support** - Dockerfile si docker-compose.yml
- **4126 registri** - Documentatie completa din manualul Janitza

### Configurare
- config.yaml pentru setari principale
- selected_registers.json pentru registri monitorizati
- Variabile ENV pentru override configuratie
