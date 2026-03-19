#!/usr/bin/env python3
"""
Script pentru extragerea datelor din PDF-ul Janitza UMG 512-PRO.
Extrage informatii generale si adrese Modbus.
"""

import fitz  # PyMuPDF
import re
import json
import csv
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple

@dataclass
class ModbusEntry:
    """Reprezentare a unei intrari Modbus."""
    address: int
    data_type: str
    access: str  # RD, WR, RD/WR
    name: str
    unit: str
    description: str
    page: int = 0

class JanitzaPDFExtractor:
    """Extractor pentru PDF-ul Janitza UMG 512-PRO."""

    DATA_TYPES = ['float', 'short', 'int', 'uint', 'long64', 'char', 'byte', 'double']
    UNITS = ['V', 'A', 'W', 'VA', 'var', 'Wh', 'VAh', 'varh', 'Hz', '%',
             '°', 'deg', 's', 'sec', 'min', 'h', 'ns', '°C', 'n', '-']

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.doc = fitz.open(pdf_path)
        self.entries: List[ModbusEntry] = []
        self.general_info: Dict = {
            'device': 'UMG 512-PRO',
            'manufacturer': 'Janitza electronics GmbH',
        }

    def extract_page_text(self, page_num: int) -> str:
        """Extrage textul dintr-o pagina."""
        page = self.doc[page_num]
        return page.get_text()

    def get_total_pages(self) -> int:
        """Returneaza numarul total de pagini."""
        return len(self.doc)

    def parse_modbus_from_text(self, text: str, page_num: int) -> List[ModbusEntry]:
        """Parseaza intrarile Modbus din text cu format multi-line."""
        entries = []
        lines = [l.strip() for l in text.split('\n') if l.strip()]

        i = 0
        while i < len(lines):
            line = lines[i]

            # Skip headers si linii non-adresa
            if line in ['UMG 512', 'Modbus address list', 'Address', 'Format', 'RD/WR', 'Designation', 'Unit', 'Note']:
                i += 1
                continue
            if 'Address' in line and 'Format' in line:
                i += 1
                continue

            # Detecteaza adresa (numar la inceputul liniei)
            addr_match = re.match(r'^(\d+)$', line)
            if addr_match:
                addr = int(addr_match.group(1))

                # Colecteaza urmatoarele linii pentru a forma o intrare completa
                entry_lines = [line]
                j = i + 1

                # Colecteaza pana la urmatoarea adresa sau header
                while j < len(lines) and j < i + 10:
                    next_line = lines[j]
                    # Stop daca gasim alta adresa
                    if re.match(r'^\d+$', next_line) and int(next_line) != addr:
                        break
                    # Stop daca gasim header
                    if next_line in ['UMG 512', 'Address', 'Modbus address list'] or \
                       ('Address' in next_line and 'Format' in next_line):
                        break
                    entry_lines.append(next_line)
                    j += 1

                # Parseaza entry-ul
                entry = self._parse_entry_lines(addr, entry_lines, page_num)
                if entry:
                    entries.append(entry)

                i = j
                continue

            # Alternativ: linie completa cu toate campurile
            full_match = re.match(r'^(\d+)\s+(float|short|int|uint|long64)\s+(RD|WR|RD/WR)\s+(\S+)\s*(.*)$', line, re.IGNORECASE)
            if full_match:
                addr = int(full_match.group(1))
                dtype = full_match.group(2).lower()
                access = full_match.group(3)
                name = full_match.group(4)
                rest = full_match.group(5).strip()

                unit, desc = self._parse_unit_desc(rest)

                entry = ModbusEntry(
                    address=addr,
                    data_type=dtype,
                    access=access,
                    name=name,
                    unit=unit,
                    description=desc,
                    page=page_num + 1
                )
                entries.append(entry)

            i += 1

        return entries

    def _parse_entry_lines(self, addr: int, lines: List[str], page_num: int) -> Optional[ModbusEntry]:
        """Parseaza o intrare din mai multe linii."""
        dtype = None
        access = None
        name = None
        unit = '-'
        desc = ''

        for line in lines[1:]:  # Skip prima linie (adresa)
            line = line.strip()
            if not line:
                continue

            # Detecteaza tipul de date
            if line.lower() in self.DATA_TYPES:
                dtype = line.lower()
                continue

            # Detecteaza access (poate fi singur sau cu nume)
            if line in ['RD', 'WR', 'RD/WR']:
                access = line
                continue

            # Access + nume pe aceeasi linie (ex: "RD/WR _NAME")
            access_name_match = re.match(r'^(RD|WR|RD/WR)\s+(_\S+)', line)
            if access_name_match:
                access = access_name_match.group(1)
                name = access_name_match.group(2)
                continue

            # Detecteaza nume (incepe cu _)
            if line.startswith('_'):
                name = line
                continue

            # Detecteaza unitate
            if line in self.UNITS:
                unit = line
                continue

            # Altfel e descriere
            if dtype and access and name:
                if desc:
                    desc += ' ' + line
                else:
                    # Poate fi unitate + descriere
                    found_unit = False
                    for u in self.UNITS:
                        if line.startswith(u + ' '):
                            unit = u
                            desc = line[len(u):].strip()
                            found_unit = True
                            break
                        elif line == u:
                            unit = u
                            found_unit = True
                            break
                    if not found_unit:
                        desc = line

        if dtype and access and name:
            return ModbusEntry(
                address=addr,
                data_type=dtype,
                access=access,
                name=name,
                unit=unit,
                description=desc,
                page=page_num + 1
            )
        return None

    def _parse_unit_desc(self, text: str) -> Tuple[str, str]:
        """Extrage unitatea si descrierea din text."""
        unit = '-'
        desc = text

        for u in self.UNITS:
            if text.startswith(u + ' '):
                unit = u
                desc = text[len(u):].strip()
                break
            elif text == u:
                unit = u
                desc = ''
                break

        return unit, desc

    def parse_modbus_entries(self):
        """Parseaza intrarile Modbus din toate paginile."""
        print("Extrag adrese Modbus...")

        for page_num in range(self.get_total_pages()):
            text = self.extract_page_text(page_num)
            page_entries = self.parse_modbus_from_text(text, page_num)
            self.entries.extend(page_entries)

            if page_entries:
                print(f"  Pagina {page_num + 1}: {len(page_entries)} intrari")

        # Remove duplicates, keeping the one with better description
        seen = {}
        for entry in self.entries:
            key = (entry.address, entry.name)
            if key not in seen or len(entry.description) > len(seen[key].description):
                seen[key] = entry

        self.entries = sorted(seen.values(), key=lambda x: x.address)
        print(f"\nTotal: {len(self.entries)} adrese unice")

    def categorize_entries(self) -> Dict[str, List[ModbusEntry]]:
        """Categorizeaza intrarile dupa tip."""
        categories = {
            'basic': [],
            'measured_200ms': [],
            'mean_values': [],
            'min_values': [],
            'max_values': [],
            'energy': [],
            'fft_harmonics': [],
            'config_other': [],
        }

        for e in self.entries:
            name = e.name
            addr = e.address

            if name.startswith('_G_') or name in ['_REALTIME', '_SYSTIME', '_DAY', '_MONTH', '_YEAR', '_HOUR', '_MIN', '_SEC', '_WEEKDAY']:
                categories['basic'].append(e)
            elif '_FFT_' in name:
                categories['fft_harmonics'].append(e)
            elif '_AVG' in name and '_MAX' not in name:
                categories['mean_values'].append(e)
            elif '_MIN' in name:
                categories['min_values'].append(e)
            elif '_MAX' in name:
                categories['max_values'].append(e)
            elif any(x in name for x in ['_WH', '_QH', '_VAH', 'ENERGY', '_CNT']):
                categories['energy'].append(e)
            elif 3793 <= addr <= 4209 and '_AVG' not in name:
                categories['measured_200ms'].append(e)
            else:
                categories['config_other'].append(e)

        return categories

    def categorize_by_measurement_type(self) -> Dict[str, Dict]:
        """Categorizeaza intrarile dupa tipul de masurare."""
        types = {
            'voltage': {
                'name': 'Tensiuni (Voltage)',
                'subtypes': {
                    'line_neutral': {'name': 'Linie-Neutru (L-N)', 'entries': []},
                    'line_line': {'name': 'Linie-Linie (L-L)', 'entries': []},
                    'sequence': {'name': 'Secventa (Sequence)', 'entries': []},
                }
            },
            'current': {
                'name': 'Curenti (Current)',
                'subtypes': {
                    'phase': {'name': 'Per faza', 'entries': []},
                    'sum': {'name': 'Suma/Total', 'entries': []},
                    'sequence': {'name': 'Secventa', 'entries': []},
                }
            },
            'power': {
                'name': 'Puteri (Power)',
                'subtypes': {
                    'active': {'name': 'Activa P (W)', 'entries': []},
                    'reactive': {'name': 'Reactiva Q (var)', 'entries': []},
                    'apparent': {'name': 'Aparenta S (VA)', 'entries': []},
                    'distortion': {'name': 'Distorsiune D', 'entries': []},
                }
            },
            'energy': {
                'name': 'Energie (Energy)',
                'subtypes': {
                    'active': {'name': 'Activa (Wh)', 'entries': []},
                    'reactive': {'name': 'Reactiva (varh)', 'entries': []},
                    'apparent': {'name': 'Aparenta (VAh)', 'entries': []},
                }
            },
            'power_factor': {
                'name': 'Factor de putere',
                'subtypes': {
                    'cos_phi': {'name': 'CosPhi', 'entries': []},
                    'pf': {'name': 'Power Factor', 'entries': []},
                }
            },
            'frequency': {
                'name': 'Frecventa (Frequency)',
                'entries': []
            },
            'thd_harmonics': {
                'name': 'THD si Armonice',
                'subtypes': {
                    'thd_voltage': {'name': 'THD Tensiune', 'entries': []},
                    'thd_current': {'name': 'THD Curent', 'entries': []},
                    'fft_voltage': {'name': 'FFT Tensiune', 'entries': []},
                    'fft_current': {'name': 'FFT Curent', 'entries': []},
                    'interharmonics': {'name': 'Interarmonice', 'entries': []},
                }
            },
            'quality': {
                'name': 'Calitate retea',
                'subtypes': {
                    'flicker': {'name': 'Flicker', 'entries': []},
                    'symmetry': {'name': 'Simetrie', 'entries': []},
                    'crest_factor': {'name': 'Factor de cresta', 'entries': []},
                    'peak_values': {'name': 'Valori de varf', 'entries': []},
                }
            },
            'statistics': {
                'name': 'Statistici',
                'subtypes': {
                    'mean': {'name': 'Valori medii (AVG)', 'entries': []},
                    'min': {'name': 'Valori minime (MIN)', 'entries': []},
                    'max': {'name': 'Valori maxime (MAX)', 'entries': []},
                }
            },
            'time': {
                'name': 'Data si ora',
                'entries': []
            },
            'config': {
                'name': 'Configurare',
                'subtypes': {
                    'averaging_time': {'name': 'Timp mediere', 'entries': []},
                    'transformer': {'name': 'Transformator', 'entries': []},
                    'other': {'name': 'Altele', 'entries': []},
                }
            },
        }

        for e in self.entries:
            name = e.name.upper()
            entry_dict = asdict(e)

            # Timp
            if name in ['_REALTIME', '_SYSTIME', '_DAY', '_MONTH', '_YEAR', '_HOUR', '_MIN', '_SEC', '_WEEKDAY']:
                types['time']['entries'].append(entry_dict)
            # Frecventa
            elif 'FREQ' in name and 'AVG' not in name:
                types['frequency']['entries'].append(entry_dict)
            # THD/FFT
            elif '_FFT_U' in name:
                if 'Z' in name:
                    types['thd_harmonics']['subtypes']['interharmonics']['entries'].append(entry_dict)
                else:
                    types['thd_harmonics']['subtypes']['fft_voltage']['entries'].append(entry_dict)
            elif '_FFT_I' in name:
                if 'Z' in name:
                    types['thd_harmonics']['subtypes']['interharmonics']['entries'].append(entry_dict)
                else:
                    types['thd_harmonics']['subtypes']['fft_current']['entries'].append(entry_dict)
            elif 'THD_U' in name or 'ZHD_U' in name:
                types['thd_harmonics']['subtypes']['thd_voltage']['entries'].append(entry_dict)
            elif 'THD_I' in name or 'ZHD_I' in name or 'TDD' in name:
                types['thd_harmonics']['subtypes']['thd_current']['entries'].append(entry_dict)
            # Statistici
            elif '_AVG' in name and '_MAX' not in name:
                types['statistics']['subtypes']['mean']['entries'].append(entry_dict)
            elif '_MIN' in name:
                types['statistics']['subtypes']['min']['entries'].append(entry_dict)
            elif '_MAX' in name:
                types['statistics']['subtypes']['max']['entries'].append(entry_dict)
            # Energie
            elif '_WH' in name or 'ENERGY' in name:
                types['energy']['subtypes']['active']['entries'].append(entry_dict)
            elif '_QH' in name:
                types['energy']['subtypes']['reactive']['entries'].append(entry_dict)
            elif '_VAH' in name or '_WH_S' in name:
                types['energy']['subtypes']['apparent']['entries'].append(entry_dict)
            # Factor de putere
            elif 'COS_PHI' in name or 'COS_SUM' in name:
                types['power_factor']['subtypes']['cos_phi']['entries'].append(entry_dict)
            elif '_PF' in name or 'PFLN' in name:
                types['power_factor']['subtypes']['pf']['entries'].append(entry_dict)
            # Calitate retea
            elif 'FLI' in name or 'FLICKER' in name:
                types['quality']['subtypes']['flicker']['entries'].append(entry_dict)
            elif '_SYM' in name or '_UN' == name or '_UM' == name or '_UG' == name:
                types['quality']['subtypes']['symmetry']['entries'].append(entry_dict)
            elif '_CF' in name or 'CREST' in name:
                types['quality']['subtypes']['crest_factor']['entries'].append(entry_dict)
            elif 'PEAK' in name:
                types['quality']['subtypes']['peak_values']['entries'].append(entry_dict)
            # Puteri
            elif '_PLN' in name or '_P_SUM' in name or ('_P' in name and 'PHASE' not in name and 'PEAK' not in name):
                types['power']['subtypes']['active']['entries'].append(entry_dict)
            elif '_QLN' in name or '_Q_SUM' in name:
                types['power']['subtypes']['reactive']['entries'].append(entry_dict)
            elif '_SLN' in name or '_S_SUM' in name:
                types['power']['subtypes']['apparent']['entries'].append(entry_dict)
            elif '_DLN' in name or 'DISTORTION' in name:
                types['power']['subtypes']['distortion']['entries'].append(entry_dict)
            # Curenti
            elif '_ILN' in name or '_IL[' in name or '_IL_' in name:
                types['current']['subtypes']['phase']['entries'].append(entry_dict)
            elif '_I_SUM' in name:
                types['current']['subtypes']['sum']['entries'].append(entry_dict)
            elif '_IN' == name or '_IM' == name or '_IG' == name:
                types['current']['subtypes']['sequence']['entries'].append(entry_dict)
            # Tensiuni
            elif '_ULN' in name or '_ULN[' in name:
                types['voltage']['subtypes']['line_neutral']['entries'].append(entry_dict)
            elif '_ULL' in name or '_ULL[' in name:
                types['voltage']['subtypes']['line_line']['entries'].append(entry_dict)
            elif '_UN' in name or '_UM' in name or '_UG' in name:
                types['voltage']['subtypes']['sequence']['entries'].append(entry_dict)
            # Config
            elif '_AVG_T' in name:
                types['config']['subtypes']['averaging_time']['entries'].append(entry_dict)
            elif 'TRANSFORMER' in name or 'RATIO' in name:
                types['config']['subtypes']['transformer']['entries'].append(entry_dict)
            else:
                types['config']['subtypes']['other']['entries'].append(entry_dict)

        return types

    def save_json(self, output_path: str):
        """Salveaza datele in format JSON structurat."""
        measurement_types = self.categorize_by_measurement_type()

        # Calculeaza statistici pentru fiecare tip
        def count_entries(obj):
            if 'entries' in obj:
                return len(obj['entries'])
            elif 'subtypes' in obj:
                return sum(count_entries(st) for st in obj['subtypes'].values())
            return 0

        data = {
            'device': {
                'model': 'UMG 512-PRO',
                'manufacturer': 'Janitza electronics GmbH',
                'document': 'Modbus Address List'
            },
            'modbus': {
                'protocol': {
                    'slave_functions': [
                        {'code': 3, 'hex': '03', 'name': 'Read Holding Registers'},
                        {'code': 4, 'hex': '04', 'name': 'Read Input Registers'},
                        {'code': 6, 'hex': '06', 'name': 'Preset Single Register'},
                        {'code': 16, 'hex': '10', 'name': 'Preset Multiple Registers'},
                        {'code': 23, 'hex': '17', 'name': 'Read/Write 4X Registers'},
                    ],
                    'byte_order': 'Big-Endian (add 32768 for Little-Endian)',
                    'update_rate_ms': 200
                },
                'transfer': {
                    'baud_rates_kbps': [9.6, 19.2, 38.4, 57.6, 115.2, 921.6],
                    'data_bits': 8,
                    'parity': 'none',
                    'stop_bits': 2
                }
            },
            'data_types': {
                'char': {'bits': 8, 'min': 0, 'max': 255},
                'byte': {'bits': 8, 'min': -128, 'max': 127},
                'short': {'bits': 16, 'min': -32768, 'max': 32767},
                'int': {'bits': 32, 'signed': True},
                'uint': {'bits': 32, 'signed': False},
                'long64': {'bits': 64, 'signed': True},
                'float': {'bits': 32, 'format': 'IEEE 754'},
                'double': {'bits': 64, 'format': 'IEEE 754'}
            },
            'averaging_times': [
                {'n': 0, 'seconds': 5},
                {'n': 1, 'seconds': 10},
                {'n': 2, 'seconds': 15},
                {'n': 3, 'seconds': 30},
                {'n': 4, 'seconds': 60},
                {'n': 5, 'seconds': 300},
                {'n': 6, 'seconds': 480},
                {'n': 7, 'seconds': 600},
                {'n': 8, 'seconds': 900},
            ],
            'statistics': {
                'total_addresses': len(self.entries),
                'by_type': {k: count_entries(v) for k, v in measurement_types.items()}
            },
            'measurements': measurement_types
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Salvat JSON: {output_path}")

    def save_csv(self, output_dir: str):
        """Salveaza datele in format CSV per categorie."""
        categories = self.categorize_entries()

        for cat_name, entries in categories.items():
            if not entries:
                continue

            output_path = Path(output_dir) / f"{cat_name}.csv"
            with open(output_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['Address', 'Type', 'Access', 'Name', 'Unit', 'Description'])
                for e in entries:
                    writer.writerow([e.address, e.data_type, e.access, e.name, e.unit, e.description])
            print(f"Salvat CSV: {output_path} ({len(entries)} intrari)")

    def save_txt(self, output_dir: str):
        """Salveaza datele in format TXT per categorie."""
        categories = self.categorize_entries()

        names = {
            'basic': ('01-basic.txt', 'Adrese de baza'),
            'measured_200ms': ('02-measured-200ms.txt', 'Masuratori 200ms'),
            'mean_values': ('03-mean-values.txt', 'Valori medii'),
            'min_values': ('04-min-values.txt', 'Valori minime'),
            'max_values': ('05-max-values.txt', 'Valori maxime'),
            'energy': ('06-energy.txt', 'Energie'),
            'fft_harmonics': ('07-fft-harmonics.txt', 'FFT si Armonice'),
            'config_other': ('08-config-other.txt', 'Configurare si altele'),
        }

        for cat_name, entries in categories.items():
            if not entries:
                continue

            filename, title = names.get(cat_name, (f"{cat_name}.txt", cat_name))
            output_path = Path(output_dir) / filename

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(f"# Janitza UMG 512-PRO - {title}\n")
                f.write("# Address | Type | Access | Name | Unit | Description\n\n")

                for e in entries:
                    line = f"{e.address:<6} {e.data_type:<7} {e.access:<5} {e.name:<35} {e.unit:<6} {e.description}"
                    f.write(line.rstrip() + '\n')

            print(f"Salvat TXT: {output_path} ({len(entries)} intrari)")

    def save_general_info(self, output_path: str):
        """Salveaza informatiile generale."""
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("# Janitza UMG 512-PRO - Informatii Generale\n\n")

            f.write("## Dispozitiv\n")
            f.write("- Model: UMG 512-PRO\n")
            f.write("- Producator: Janitza electronics GmbH\n\n")

            f.write("## Formate de Date\n")
            f.write("| Tip | Biti | Min | Max |\n")
            f.write("|-----|------|-----|-----|\n")
            f.write("| char | 8 | 0 | 255 |\n")
            f.write("| byte | 8 | -128 | 127 |\n")
            f.write("| short | 16 | -32768 | 32767 |\n")
            f.write("| int | 32 | -2^31 | 2^31-1 |\n")
            f.write("| uint | 32 | 0 | 2^32-1 |\n")
            f.write("| long64 | 64 | -2^63 | 2^63-1 |\n")
            f.write("| float | 32 | IEEE 754 | IEEE 754 |\n")
            f.write("| double | 64 | IEEE 754 | IEEE 754 |\n\n")

            f.write("## Parametri Transfer Modbus\n")
            f.write("- Baud rate: 9.6, 19.2, 38.4, 57.6, 115.2, 921.6 kbps\n")
            f.write("- Data bits: 8\n")
            f.write("- Parity: none\n")
            f.write("- Stop bits: 2 (UMG512-PRO), 1-2 (external)\n")
            f.write("- Byte order: Big-Endian (default)\n")
            f.write("- Pentru Little-Endian: adauga 32768 la adresa\n")
            f.write("- Update rate: 200ms\n\n")

            f.write("## Functii Modbus Slave\n")
            f.write("| Cod | Hex | Functie |\n")
            f.write("|-----|-----|--------|\n")
            f.write("| 03 | 03 | Read Holding Registers |\n")
            f.write("| 04 | 04 | Read Input Registers |\n")
            f.write("| 06 | 06 | Preset Single Register |\n")
            f.write("| 16 | 10 | Preset Multiple Registers |\n")
            f.write("| 23 | 17 | Read/Write 4X Registers |\n\n")

            f.write("## Functii Modbus Master\n")
            f.write("| Cod | Hex | Functie |\n")
            f.write("|-----|-----|--------|\n")
            f.write("| 01 | 01 | Read Coil Status |\n")
            f.write("| 02 | 02 | Read Input Status |\n")
            f.write("| 03 | 03 | Read Holding Registers |\n")
            f.write("| 04 | 04 | Read Input Registers |\n")
            f.write("| 05 | 05 | Force Single Coil |\n")
            f.write("| 06 | 06 | Preset Single Register |\n")
            f.write("| 15 | 0F | Force Multiple Coils |\n")
            f.write("| 16 | 10 | Preset Multiple Registers |\n")
            f.write("| 23 | 17 | Read/Write 4X Registers |\n\n")

            f.write("## Timpuri de Mediere (Averaging Times)\n")
            f.write("| n | Secunde | Minute |\n")
            f.write("|---|---------|--------|\n")
            f.write("| 0 | 5 | - |\n")
            f.write("| 1 | 10 | - |\n")
            f.write("| 2 | 15 | - |\n")
            f.write("| 3 | 30 | 0.5 |\n")
            f.write("| 4 | 60 | 1 |\n")
            f.write("| 5 | 300 | 5 |\n")
            f.write("| 6 | 480 | 8 |\n")
            f.write("| 7 | 600 | 10 |\n")
            f.write("| 8 | 900 | 15 |\n")

        print(f"Salvat info generale: {output_path}")

    def print_summary(self):
        """Afiseaza sumar."""
        print("\n" + "="*60)
        print("SUMAR")
        print("="*60)
        print(f"Pagini PDF: {self.get_total_pages()}")
        print(f"Adrese Modbus: {len(self.entries)}")

        categories = self.categorize_entries()
        print("\nPer categorie:")
        for cat_name, entries in categories.items():
            if entries:
                print(f"  {cat_name}: {len(entries)}")

        if self.entries:
            print(f"\nRange adrese: {self.entries[0].address} - {self.entries[-1].address}")

    def extract_all(self):
        """Extractie completa."""
        self.parse_modbus_entries()
        self.print_summary()

    def close(self):
        self.doc.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Extrage date din PDF Janitza UMG 512-PRO')
    parser.add_argument('pdf', nargs='?', default='janitza-umg512-manual.pdf', help='Fisierul PDF')
    parser.add_argument('--output', '-o', default='.', help='Director output')
    parser.add_argument('--format', '-f', choices=['all', 'json', 'csv', 'txt'], default='all')
    parser.add_argument('--page', '-p', type=int, help='Afiseaza textul unei pagini')
    args = parser.parse_args()

    extractor = JanitzaPDFExtractor(args.pdf)

    try:
        if args.page is not None:
            print(f"=== Pagina {args.page} ===")
            print(extractor.extract_page_text(args.page - 1))
        else:
            extractor.extract_all()

            output_dir = Path(args.output)
            output_dir.mkdir(parents=True, exist_ok=True)

            extractor.save_general_info(output_dir / '00-general-info.txt')

            if args.format in ['all', 'json']:
                extractor.save_json(output_dir / 'modbus_data.json')
            if args.format in ['all', 'csv']:
                extractor.save_csv(output_dir)
            if args.format in ['all', 'txt']:
                extractor.save_txt(output_dir)

            print("\nGata!")
    finally:
        extractor.close()


if __name__ == '__main__':
    main()
