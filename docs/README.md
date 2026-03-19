# Janitza UMG 512-PRO - Documentatie Modbus

Acest director contine documentatia extrasa din manualul Janitza UMG 512-PRO pentru adresele Modbus.

## Sursa

- **PDF Original**: `janitza-umg512-manual.pdf`
- **Document**: Modbus Address and Formulary
- **Producator**: Janitza electronics GmbH
- **Model**: UMG 512-PRO (Power Quality Analyser)

## Fisiere Generate

### Informatii Generale
| Fisier | Descriere |
|--------|-----------|
| `00-general-info.txt` | Parametri Modbus, functii, formate date, timpuri mediere |

### Date Structurate (JSON)
| Fisier | Descriere |
|--------|-----------|
| `modbus_data.json` | Toate adresele Modbus structurate pe tipuri de masuratori (1.2MB, 4126 adrese) |

### Fisiere TXT (per categorie)
| Fisier | Intrari | Descriere |
|--------|---------|-----------|
| `01-basic.txt` | 92 | Adrese de baza (_G_), tensiuni, curenti, puteri, energie |
| `02-measured-200ms.txt` | 206 | Masuratori la 200ms |
| `03-mean-values.txt` | 346 | Valori medii (_AVG) |
| `04-min-values.txt` | 196 | Valori minime (_MIN) |
| `05-max-values.txt` | 706 | Valori maxime (_MAX) |
| `06-energy.txt` | 95 | Contoare energie (Wh, varh, VAh) |
| `07-fft-harmonics.txt` | 1890 | FFT si armonice |
| `08-config-other.txt` | 595 | Configurari si altele |

### Fisiere CSV (pentru import in Excel/DB)
Acelasi continut ca fisierele TXT, in format CSV:
- `basic.csv`, `measured_200ms.csv`, `mean_values.csv`, `min_values.csv`
- `max_values.csv`, `energy.csv`, `fft_harmonics.csv`, `config_other.csv`

## Script Extractie

### `extract_pdf.py`

Script Python pentru extragerea datelor din PDF-ul Janitza.

#### Cerinte
```bash
pip install pymupdf
```

#### Utilizare
```bash
# Extractie completa (JSON + CSV + TXT)
python3 extract_pdf.py janitza-umg512-manual.pdf

# Doar JSON
python3 extract_pdf.py janitza-umg512-manual.pdf --format json

# Doar CSV
python3 extract_pdf.py janitza-umg512-manual.pdf --format csv

# Doar TXT
python3 extract_pdf.py janitza-umg512-manual.pdf --format txt

# Output in alt director
python3 extract_pdf.py janitza-umg512-manual.pdf -o ./output

# Vezi textul unei pagini specifice (pentru debug)
python3 extract_pdf.py janitza-umg512-manual.pdf --page 15
```

## Structura JSON (`modbus_data.json`)

```
{
  "device": {
    "model": "UMG 512-PRO",
    "manufacturer": "Janitza electronics GmbH"
  },
  "modbus": {
    "protocol": {
      "slave_functions": [...],
      "byte_order": "Big-Endian",
      "update_rate_ms": 200
    },
    "transfer": {
      "baud_rates_kbps": [9.6, 19.2, 38.4, 57.6, 115.2, 921.6],
      "data_bits": 8,
      "parity": "none",
      "stop_bits": 2
    }
  },
  "data_types": { ... },
  "averaging_times": [ ... ],
  "statistics": {
    "total_addresses": 4126,
    "by_type": { ... }
  },
  "measurements": {
    "voltage": {
      "subtypes": {
        "line_neutral": { "entries": [...] },
        "line_line": { "entries": [...] },
        "sequence": { "entries": [...] }
      }
    },
    "current": { ... },
    "power": {
      "subtypes": {
        "active": { ... },
        "reactive": { ... },
        "apparent": { ... },
        "distortion": { ... }
      }
    },
    "energy": { ... },
    "power_factor": { ... },
    "frequency": { ... },
    "thd_harmonics": {
      "subtypes": {
        "thd_voltage": { ... },
        "thd_current": { ... },
        "fft_voltage": { ... },
        "fft_current": { ... },
        "interharmonics": { ... }
      }
    },
    "quality": {
      "subtypes": {
        "flicker": { ... },
        "symmetry": { ... },
        "crest_factor": { ... },
        "peak_values": { ... }
      }
    },
    "statistics": {
      "subtypes": {
        "mean": { ... },
        "min": { ... },
        "max": { ... }
      }
    },
    "time": { ... },
    "config": { ... }
  }
}
```

## Utilizare JSON in Python

```python
import json

# Incarca datele
with open('modbus_data.json') as f:
    data = json.load(f)

# Acces la tensiuni L-N
voltages_ln = data['measurements']['voltage']['subtypes']['line_neutral']['entries']
for v in voltages_ln:
    print(f"Addr: {v['address']}, Name: {v['name']}, Unit: {v['unit']}")

# Acces la puteri active
powers = data['measurements']['power']['subtypes']['active']['entries']

# Acces la energie
energy = data['measurements']['energy']['subtypes']['active']['entries']

# Acces la frecventa
freq = data['measurements']['frequency']['entries']

# Gaseste o adresa specifica
def find_by_address(addr):
    for mtype in data['measurements'].values():
        if 'entries' in mtype:
            for e in mtype['entries']:
                if e['address'] == addr:
                    return e
        elif 'subtypes' in mtype:
            for st in mtype['subtypes'].values():
                for e in st['entries']:
                    if e['address'] == addr:
                        return e
    return None

# Exemplu: gaseste adresa 19000
entry = find_by_address(19000)
print(entry)  # {'address': 19000, 'name': '_G_ULN[0]', 'unit': 'V', ...}
```

## Informatii Tehnice Modbus

### Functii Modbus Slave (suportate de UMG 512-PRO)
| Cod | Hex | Functie |
|-----|-----|---------|
| 03 | 03 | Read Holding Registers |
| 04 | 04 | Read Input Registers |
| 06 | 06 | Preset Single Register |
| 16 | 10 | Preset Multiple Registers |
| 23 | 17 | Read/Write 4X Registers |

### Parametri Transfer
- **Baud rate**: 9.6, 19.2, 38.4, 57.6, 115.2, 921.6 kbps
- **Data bits**: 8
- **Parity**: none
- **Stop bits**: 2
- **Byte order**: Big-Endian (pentru Little-Endian adauga 32768 la adresa)
- **Update rate**: 200ms

### Formate Date
| Tip | Biti | Range |
|-----|------|-------|
| char | 8 | 0 - 255 |
| byte | 8 | -128 - 127 |
| short | 16 | -32768 - 32767 |
| int | 32 | signed |
| uint | 32 | unsigned |
| long64 | 64 | signed |
| float | 32 | IEEE 754 |
| double | 64 | IEEE 754 |

### Timpuri de Mediere
| n | Secunde | Minute |
|---|---------|--------|
| 0 | 5 | - |
| 1 | 10 | - |
| 2 | 15 | 0.25 |
| 3 | 30 | 0.5 |
| 4 | 60 | 1 |
| 5 | 300 | 5 |
| 6 | 480 | 8 |
| 7 | 600 | 10 |
| 8 | 900 | 15 |

## Adrese Frecvent Utilizate

### Tensiuni (Voltage)
| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19000 | _G_ULN[0] | V | Tensiune L1-N |
| 19002 | _G_ULN[1] | V | Tensiune L2-N |
| 19004 | _G_ULN[2] | V | Tensiune L3-N |
| 19006 | _G_ULL[0] | V | Tensiune L1-L2 |
| 19008 | _G_ULL[1] | V | Tensiune L2-L3 |
| 19010 | _G_ULL[2] | V | Tensiune L3-L1 |

### Curenti (Current)
| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19012 | _G_ILN[0] | A | Curent L1 |
| 19014 | _G_ILN[1] | A | Curent L2 |
| 19016 | _G_ILN[2] | A | Curent L3 |
| 19018 | _G_I_SUM3 | A | Suma vectoriala I1+I2+I3 |

### Puteri (Power)
| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19020 | _G_PLN[0] | W | Putere activa L1 |
| 19022 | _G_PLN[1] | W | Putere activa L2 |
| 19024 | _G_PLN[2] | W | Putere activa L3 |
| 19026 | _G_P_SUM3 | W | Putere activa totala |
| 19028 | _G_SLN[0] | VA | Putere aparenta L1 |
| 19034 | _G_S_SUM3 | VA | Putere aparenta totala |
| 19036 | _G_QLN[0] | var | Putere reactiva L1 |
| 19042 | _G_Q_SUM3 | var | Putere reactiva totala |

### Factor de Putere
| Adresa | Nume | Descriere |
|--------|------|-----------|
| 19044 | _G_COS_PHI[0] | CosPhi L1 |
| 19046 | _G_COS_PHI[1] | CosPhi L2 |
| 19048 | _G_COS_PHI[2] | CosPhi L3 |

### Frecventa
| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19050 | _G_FREQ | Hz | Frecventa masurata |

### Energie
| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19054-19058 | _G_WH[0-2] | Wh | Energie activa L1-L3 |
| 19060 | _G_WH_SUML13 | Wh | Energie activa totala |
| 19062-19066 | _G_WH_V[0-2] | Wh | Energie consumata L1-L3 |
| 19070-19074 | _G_WH_Z[0-2] | Wh | Energie livrata L1-L3 |

### THD
| Adresa | Nume | Unitate | Descriere |
|--------|------|---------|-----------|
| 19110-19114 | _G_THD_ULN[0-2] | % | THD Tensiune L1-L3 |
| 19116-19120 | _G_THD_ILN[0-2] | % | THD Curent L1-L3 |

## Istoric

- **2025-01-07**: Creare initiala
  - Extractie date din PDF folosind PyMuPDF
  - Generare fisiere TXT, CSV, JSON
  - Structurare JSON pe tipuri de masuratori
  - Documentatie README

## Note

- Adresele prefixate cu `_G_` sunt adrese "gateway/global" - cele mai frecvent utilizate
- Pentru Little-Endian byte order, adaugati 32768 la adresa
- Valorile float ocupa 2 registre Modbus (32 biti)
- Valorile long64 ocupa 4 registre Modbus (64 biti)
