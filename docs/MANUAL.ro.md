# Manual de utilizare — Monitor Janitza UMG 512-PRO

[🇬🇧 English](MANUAL.md) | 🇷🇴 **Română**

Un ghid pas cu pas: de la o instalare nouă la monitorizare, integrări și servirea
contoarelor virtuale. Pentru analiza aprofundată a arhitecturii motorului de
contoare virtuale vezi **[VIRTUAL-METER.ro.md](VIRTUAL-METER.ro.md)**.

> 🇷🇴 Română. Versiunile localizate sunt binevenite prin PR.

## Cuprins
1. [De ce ai nevoie](#1-de-ce-ai-nevoie)
2. [Instalare (Docker)](#2-instalare-docker)
3. [Prima configurare](#3-prima-configurare)
4. [Interfața Web, tab cu tab](#4-interfața-web-tab-cu-tab)
5. [Contoare virtuale — pas cu pas](#5-contoare-virtuale--pas-cu-pas)
6. [Home Assistant (MQTT)](#6-home-assistant-mqtt)
7. [InfluxDB & Grafana](#7-influxdb--grafana)
8. [Depanare](#8-depanare)

---

## 1. De ce ai nevoie
- Un **Janitza UMG 512-PRO** (sau un UMG compatibil) accesibil în rețea cu
  **Modbus TCP activat** (portul implicit 502). Notează-i IP-ul.
- O gazdă cu **Docker + Docker Compose**.
- *(Opțional)* un broker MQTT (pentru Home Assistant) și/sau InfluxDB (pentru
  Grafana).

---

## 2. Instalare (Docker)

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

Atât pentru o configurare doar de monitorizare. Jurnale: `docker compose logs -f`.

---

## 3. Prima configurare

> **Sfat:** după prima pornire poți seta tot din UI (**Config → Settings**) — se salvează în
> `config/config.yaml` și se aplică fără restart. `.env` e doar o cale comodă de a pre-popula un
> deploy nou. O valoare din env are întâietate și blochează acel câmp în UI.

Editează `.env` (sau setează aceleași variabile în compose-ul tău). Esențialul:

| Variabilă | Ce este | Exemplu |
|-----------|---------|---------|
| `MODBUS_HOST` | IP-ul Janitza | `192.168.1.100` |
| `MODBUS_PORT` | port Modbus TCP | `502` |
| `MODBUS_UNIT_ID` | unitate Modbus | `1` |
| `MQTT_BROKER` / `MQTT_PORT` | broker (opțional) | `192.168.1.100` / `1883` |
| `INFLUXDB_URL` / `INFLUXDB_TOKEN` | InfluxDB (opțional) | — |
| `UI_PORT` | port interfață Web | `8080` |

Repornește după editare: `docker compose up -d`. Punctul **Modbus** din colțul
dreapta-sus al interfeței devine verde când se conectează.

Poți de asemenea configura majoritatea acestor lucruri din interfață → tab-ul
**Config** (fără repornire pentru modificările de registre/poll — ele se reîncarcă
la cald).

---

## 4. Interfața Web, tab cu tab

Deschide `http://<host>:8080`.

- **Dashboard** — carduri KPI live + valorile pe care le-ai fixat. Apasă
  *Customize* pentru a alege carduri; comută între vizualizarea card/tabel.
- **Monitor** — trage orice valoare din lista din stânga pe grafic pentru un
  grafic live, cu zoom. Adaugă mai multe pentru a compara.
- **Registers** — tabelul complet de registre cu valorile curente; căutare/filtrare.
- **Config** — două sub-tab-uri: *Settings* (conexiune Modbus/MQTT/InfluxDB, UI) și
  *Registers* (alege ce registre să interoghezi, setează grupuri de poll, praguri,
  unități, widget-uri de dashboard). Modificările se reîncarcă la cald.
- **Virtual Meters** — servește valorile live ca și contoare standard către alte
  sisteme (vezi pasul 5).

Cele trei puncte din dreapta-sus (Modbus / MQTT / InfluxDB) arată starea
conexiunii — apasă pe unul pentru detalii.

---

## 5. Contoare virtuale — pas cu pas

Scop: a permite unui alt sistem (Victron ESS, un invertor Fronius, orice client
SunSpec) să citească acest unic Janitza ca și contorul pe care *el* îl așteaptă.

> ⚠️ Un contor virtual poate alimenta o buclă de control. Parcurge pașii 5.1→5.3
> (validare în paralel) înainte de a-l face vreodată singurul contor al unui
> consumator.

**5.0 — Publică porturile (o singură dată).** În `docker-compose.yml` intervalul de
porturi al contoarelor este publicat (implicit `1502-1512`, plus `502` pentru
Fronius). Alege porturile instanțelor în interiorul acelui interval. Lărgește
intervalul + recreează containerul dacă ai nevoie de mai multe.

**5.1 — Alege sau creează un șablon.** Mergi la **Virtual Meters → Templates**.
- Livrate: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
  (Fronius Smart Meter → DataManager), `fronius_sunspec_meter` (SunSpec generic).
- *Șablon nou*: definește fiecare registru (adresă, tip, scalare, sursă). Sursa
  poate fi un registru Janitza live, o constantă sau o sumă de registre.
- *Import*: adaugă un `.yaml` partajat de altcineva (validat înainte de salvare).

**5.2 — Adaugă o instanță.** Pe sub-tab-ul **Meters**, alege șablonul, un port
liber, unit id → **Add instance**. Pornește **dezactivată**.

**5.3 — Validează în paralel.** Activează instanța (comutator). Îndreaptă un
consumator de *test* — sau pur și simplu deschide tab-ul **Logs** — către
`host:port`. Urmărește jurnalul de interogări live: vezi exact ce citește
consumatorul, când și ce returnezi. Compară valorile servite cu contorul tău real.
Tab-ul **Stats** arată rata de cereri, erorile și ce registre sunt citite cel mai
mult.

**5.4 — Fă comutarea.** Odată ce ai încredere în el, îndreaptă consumatorul real
către contorul virtual și elimină contorul fizic dedicat al acestuia. **Watchdog-ul
de prospețime** este plasa ta de siguranță: dacă datele Janitza devin învechite,
contorul oprește răspunsul astfel încât fail-safe-ul propriu al consumatorului
pentru pierderea rețelei să se activeze.

**5.5 — Observă și exportă.** Tab-urile **Logs**/**Stats** păstrează ultimele 1024
de cereri + contoare în RAM. **Exportă** un șablon (YAML) pentru a-l partaja sau a
face backup; cardul contorului (acordeon) arată conexiunile active ale clienților
(ip:port).

---

## 6. Home Assistant (MQTT)

Setează `MQTT_BROKER`/`MQTT_PORT` (și credențialele) în `.env`, repornește.
Monitorul publică **autodiscovery MQTT pentru Home Assistant**, astfel încât
entitățile apar automat sub dispozitiv. Un topic Last-Will marchează dispozitivul
ca offline dacă monitorul se oprește. Alege ce registre se publică (și topicurile
lor) în **Config → Registers**.

---

## 7. InfluxDB & Grafana

Setează `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` în
`.env`. Scrierile sunt grupate cu reîncercare/backoff automat și o protecție
anti-NaN. Îndreaptă Grafana către același bucket. Măsurătoarea/tag-urile per
registru sunt configurabile în **Config → Registers**. Profilurile compose
opționale pot porni un InfluxDB + Grafana local (vezi README).

**Garanții de date.** Fiecare punct e ștampilat cu ora *citirii* Modbus, nu a
flush-ului. Dacă InfluxDB devine inaccesibil, punctele intră într-un buffer
store-and-forward în RAM (implicit **10 minute / 50.000 de puncte**, reglabil
prin `influxdb.buffer_minutes` / `buffer_max_points` în `config.yaml`) și sunt
replay-ate cu timestamp-urile originale la reconectare — idempotent, pentru că
InfluxDB deduplică pe măsurătoare+taguri+timestamp, deci fără duplicate.
Batch-urile abandonate de client după ~5 min de reîncercări proprii sunt
recuperate în același buffer. Panele mai lungi decât fereastra bufferului pierd
punctele cele mai vechi (doar RAM — un restart golește bufferul); pentru
tensiuni, înregistrarea internă a contorului le poate reface prin
`python -m janitza.backfill`. Urmărește `buffer_points` / `replayed_total` /
`dropped_total` în **`/api/status`**. MQTT nu se replay-ează intenționat: e o
magistrală live (consumatorii acționează pe „acum”), iar la reconectare se
republică întreaga stare curentă.

---

## 8. Depanare

| Simptom | Verifică |
|---------|----------|
| Punctul Modbus roșu | `MODBUS_HOST`/portul corecte? Modbus TCP activat pe Janitza? firewall? |
| Interfața arată versiunea veche după actualizare | reîmprospătează forțat browserul (bundle-ul aplicației are cache-busting, dar proxy-urile pot stoca în cache) |
| Contor virtual „stale / starting” | sursa Janitza nu este proaspătă — verifică conexiunea Modbus; watchdog-ul nu va servi date învechite, prin design |
| Consumatorul nu poate ajunge la un contor virtual | este portul în interiorul intervalului compose publicat? accesibil din rețeaua consumatorului? verifică tab-ul **Logs** pentru citiri primite |
| Entitățile MQTT lipsesc din HA | brokerul accesibil? autodiscovery activat? urmărește `docker compose logs` |
| Avertismente InfluxDB write-retry | URL/token/bucket InfluxDB corecte? Clientul reîncearcă ~5 min, apoi batch-ul e recuperat în bufferul RAM și replay-at la reconectare — vezi `replayed_total`/`dropped_total` în `/api/status` |

Tot blocat? Deschide un issue — include `docker compose logs` și configurația ta
(cu datele sensibile mascate). Vezi **[VIRTUAL-METER.ro.md](VIRTUAL-METER.ro.md)**
pentru detaliile interne ale motorului și cum să adaugi un nou șablon de contor.
