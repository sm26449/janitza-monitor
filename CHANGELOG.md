# Changelog

Toate modificarile notabile ale proiectului sunt documentate in acest fisier.

## [2.6.1] - 2026-06-26

### Adaugat
- **Onboarding prim-run** - pe un deploy nou/neconfigurat (Modbus nu s-a conectat niciodata) apare un modal „Connect your meter" cu buton direct spre **Config → Settings**. Apare doar dupa o pauza de gratie si **doar** cand meterul nu s-a conectat niciodata - **nu** deranjeaza un sistem configurat aflat intr-o pana temporara, si se inchide singur cand prima citire reuseste. Tradus EN/RO.

### Schimbat
- **Config editabil din UI, autoritar** - documentatia (README + manuale) duce acum cu „seteaza din UI" (Config → Settings, persistat in `config.yaml`, aplicat fara restart); variabilele `.env`/env devin optionale (bootstrap) si au intaietate cand sunt setate.

## [2.6.0] - 2026-06-22

### Adaugat
- **Interfata multi-limba (i18n)** - selector de limba in bara de titlu, **implicit English** (mai international). Limbile sunt fisiere `ui/languages/<cod>.json` **descoperite dinamic**: copiezi `en.json` -> `es.json`, traduci valorile si limba apare in selector la urmatorul reload, fara modificari de cod si fara rebuild (`ui/` e servit live). `en.json` e **sursa de adevar si fallback** - English e mereu incarcat, apoi limba selectata e suprapusa, deci **traducerile partiale functioneaza** (orice cheie lipsa cade pe English). Alegerea persista in `localStorage`. Romana inclusa (`ro.json`). Ghid de contributie in `ui/languages/README.md`. Endpoint-uri `GET /api/languages` (listeaza limbile din director) si `GET /api/languages/{cod}`.

## [2.5.0] - 2026-06-22

### Adaugat
- **Vedere Energy (energie lunara)** - alegi o luna -> totaluri **consum (import)**, **injectie (export)**, **reactiva**, **aparenta** (delta contoarelor cumulative pe luna) + grafic cu defalcare zilnica import/export, citite din InfluxDB. Endpoint `GET /api/energy/monthly?year=&month=`.

### Reparat
- **History fara InfluxDB** - cand InfluxDB nu e activat, History afiseaza acum un mesaj clar („not configured") in loc de un grafic gol/rupt (`/api/history/registers` raporteaza `influx_enabled`).
- **Securitate: injectie Flux** prin `start`/`stop` (regex RFC3339 ne-ancorat lasa sa treaca un payload) si prin `measurement` (curata doar `"`) - acum validare ancorata + whitelist de caractere.
- **query_history robust** - client cu **timeout** rulat **in afara event loop-ului** (un InfluxDB blocat nu mai ingheata tot API-ul) si client scurt dedicat (fara race cu clientul de scriere inchis de thread-ul de reconectare).
- **update_instance** - **rollback** la configul anterior daca repornirea instantei esueaza (nu mai persista un port/unit invalid).
- **Wrapper-ul de fetch al cheii API** - robust la `Headers`/`Request`, nu mai muta obiectul de optiuni al apelantului.

## [2.4.0] - 2026-06-22

### Adaugat
- **Editare instanta vMeter din UI** - port / unit_id / stale_after_s / update_interval_s ale unei instante existente se modifica acum dintr-un dialog (buton „Edit settings" pe fiecare contor), nu doar editand `virtual_meters.yaml` manual. Endpoint `PATCH /api/virtual-meters/{template}`; schimbarea portului/unit reporneste contorul (avertisment in dialog ca scapa consumatorii conectati).
- **Sanatate achizitie date + istoric dropout-uri** - `modbus_client` tine acum un inel de evenimente (esecuri de citire, cu timestamp) + ora ultimei citiri reusite si prospetimea per poll-group, expuse in `/api/status`, in `/health` (bloc `modbus`; `status` = cel mai prost dintre vmeter si modbus) si pe MQTT (`<prefix>/data_health`, pentru alertd). `/health` ramane probe-safe: o sursa Janitza stale degradeaza `status` dar intoarce **HTTP 200** (nu reporneste containerul — restart-ul nu repara un dispozitiv inaccesibil). Config `modbus.stale_after_s` / env `MODBUS_STALE_AFTER_S` (implicit 30s).
- **Vedere istoric/trend (tab History)** - citeste datele **inapoi** din InfluxDB (pana acum app-ul doar scria). Selector de registri cautabil, grupat pe categorii, cu click pentru adaugare/scoatere (punct colorat = culoarea liniei); suprapune **mai multi registri** pe axa Y comuna; banda min/max pentru un singur registru; **hover** cu crosshair + tooltip ce listeaza valoarea fiecarei serii la momentul cel mai apropiat; ora locala. Endpoint `GET /api/history` (+ `GET /api/history/registers`).

## [2.3.1] - 2026-06-21

### Reparat
- **Interval realtime afisat in bara de status** - bara afisa mereu `realtime: 1s` (text hardcodat in template), indiferent de intervalul configurat. Acum se randeaza dinamic din `poll_groups` (ex. `realtime: 250ms`), mereu sincron cu config-ul; etichetele cu interval hardcodat din dropdown-urile de poll-group au fost eliminate.

## [2.3.0] - 2026-06-21

### Adaugat
- **IP-uri clienti in starea publicata (`peers`)** - payload-ul `<prefix>/vmeter/<id>/state` include acum `peers`, un CSV cu IP-urile clientilor conectati. Permite unui monitor (alertd) sa potriveasca un consumator anume cu `contains()` — alerta cand un IP asteptat se deconecteaza sau cand apare unul neasteptat — fara a parsa lista de conexiuni.

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
