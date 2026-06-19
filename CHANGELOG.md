# Changelog

Toate modificarile notabile ale proiectului sunt documentate in acest fisier.

## [2.2.0] - 2026-06-19

### Adaugat
- **Decodare interogari** - click pe orice rand din jurnalul Logs deschide un modal care desface raspunsul Modbus brut in valori ingineresti: adresa (dec+hex), variabila sursa, tip, cuvinte brute, valoare decodata. Cel mai rapid mod de a confirma o mapa.
- **Jurnal de evenimente per contor** - ultimele 50 de evenimente de ciclu-de-viata in RAM (started / crash / restart_failed / wedged / stopped-stale / supervise), afisate in tab-ul Stats & Debug. Vezi *de ce* a tacut un contor, nu doar *ca* a tacut.
- **Endpoint `/health` constient de metere** - 200 pentru ok/degraded, 503 doar cand un contor activat e `down` (crash/pornire esuata). Healthcheck-ul Docker reflecta acum starea meterelor, nu doar „e serverul pornit". O sursa stale = degraded (200), fail-safe corect, fara restart inutil.
- **Stare MQTT completa** - payload-ul `<prefix>/vmeter/<id>/state` include acum conexiunile active (ip:port), req/s, bytes RX/TX, uptime, vechimea datelor, ultima eroare si starea `ok/stale/down` — imaginea completa pentru monitorizare, fara a duplica datele electrice.
- **Autodiscovery Home Assistant pentru contoare virtuale** - fiecare contor apare automat ca device HA (legat de Janitza prin `via_device`) cu entitati: serving, state, req/s, requests, errors, connections, data age, uptime, last error.
- **Indicator vMeter in bara de status** - langa Modbus/MQTT/InfluxDB, un pill `vMeter N/M` arata cate contoare sunt online/total (verde toate ok, gri unele stale, rosu vreunul down); click deschide pagina Virtual Meters.

### Imbunatatit
- **Logs - panou de decodare lateral** - click pe un rand decodeaza in dreapta tabelului (rand evidentiat persistent), in loc de modal; hover + eticheta `decode ›` fac actiunea descoperibila; randurile cu exceptie (EXC) sunt evidentiate subtil rosu pentru depanare rapida a maparilor.
- **Uptime per conexiune** - fiecare conexiune activa afiseaza de cat timp e stabila (`connected_s` in payload-ul MQTT + `up 5m` in cardul Meters). O conexiune care flapeaza (uptime mic, mereu resetat) e un semnal clar ca un consumator se reconecteaza.

## [2.1.0] - 2026-06-18

### Adaugat
- **Monitorizare prin MQTT** - fiecare contor virtual isi publica starea (retained) pe `<prefix>/vmeter/<id>/state` la 10s, pentru alertare externa (ex. alertd: `state != "listening"` sau `var_age()`).
- **Sonda de liveness** - detecteaza un server de contor blocat (thread viu dar care nu mai accepta conexiuni) si il reporneste; pe langa recovery-ul la crash de thread.
- Imagine prebuilt **multi-arch (amd64/arm64)** publicata automat pe GHCR la fiecare release; **manual de utilizare** + **ghid Virtual Meter** bilingve (EN/RO) cu diagrame.

### Reparat
- XSS in pagina Virtual Meters (nume/id sablon, valori string neescapate); poll-urile se opresc cand tab-ul e ascuns; cache-buster pe CSS; accesibilitate acordeon (tastatura/ARIA).
- Scriere **atomica** a config-ului de instante; race la restart de server (thread); freshness watchdog nu mai fabrica prospetime cand lipseste timestamp-ul (fail-safe).
- Dockerfile include `config/` (imaginea prebuilt vine cu sabloanele); curatat IP/org private din `backfill.py`; CORS fara credentiale.

## [2.0.0] - 2026-06-18

### Adaugat
- **Motor de contoare virtuale** - serveste un singur Janitza ca mai multe contoare Modbus definite prin sabloane YAML: Carlo Gavazzi EM24 -> Victron ESS, Fronius Smart Meter TS -> Fronius DataManager (mapa nativa Carlo Gavazzi), si un exemplu generic SunSpec 213.
- **Observabilitate** - pagina cu tab-uri (Meters/Templates/Logs/Stats): jurnal live al ultimelor 1024 cereri Modbus (adresa/count/raspuns/latenta), chart cereri/secunda, registrele cele mai citite, conexiuni client; import/export sabloane YAML.
- **Fiabilitate** - watchdog de prospetime (sursa stale -> opreste = fail-safe consumator), recovery automat la crash de thread, I/O intarit (Modbus client/server, MQTT, InfluxDB).

### Schimbat
- Relicentiat la **PolyForm Noncommercial 1.0.0** (gratis pentru uz personal/necomercial; uz comercial necesita licenta separata).

## [1.5.0] - 2026-06-03

### Adaugat
- **Auto-backfill din memoria contorului** (`janitza/backfill.py`) - recupereaza automat golurile din InfluxDB citind inregistrarea on-board de 1 minut a contorului prin API-ul HTTP `HIST_DATA`. Cand colectorul pierde conexiunea de retea (ex. un dip de tensiune reseteaza switch-ul), stream-ul live - si InfluxDB - ramane cu un gol, dar contorul (alimentat din retea) continua sa logheze in flash-ul propriu. Job-ul scrie punctele lipsa inapoi, cu aceeasi schema masura/field/tag ca publisher-ul live, asa ca graficele se completeaza fara discontinuitate.
  - Moduri: auto (detecteaza golul de la coada si il umple), `--window <ISO_UTC> <ISO_UTC>` (gol istoric), `--dry-run`, `--verbose`
  - Acopera tensiunile L-N si L-L la 1 minut (singurii parametri inregistrati istoric de UMG512; curent/putere/frecventa sunt doar live)
  - Idempotent (puncte pe granite de minut); marcaj `backfilled=1` pentru trasabilitate
  - Rulare: `docker exec pv-stack-janitza-monitor python -m janitza.backfill`; cron `*/10 * * * *`

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
