"""Register parser for Janitza UMG 512-PRO Modbus data."""

import struct
from typing import List, Optional, Any, Dict


class RegisterParser:
    """
    Parser for Janitza Modbus register data.

    Supports various data types:
    - float (32-bit IEEE 754)
    - int32 (signed 32-bit)
    - uint32 (unsigned 32-bit)
    - int16 (signed 16-bit)
    - uint16 (unsigned 16-bit)
    - int64/long64 (signed 64-bit)
    - double (64-bit IEEE 754)
    """

    # Number of 16-bit registers per data type
    REGISTER_COUNTS = {
        'float': 2,
        'float32': 2,
        'int32': 2,
        'uint32': 2,
        'int16': 1,
        'uint16': 1,
        'short': 1,
        'int64': 4,
        'long64': 4,
        'uint64': 4,
        'double': 4,
    }

    def __init__(self, byte_order: str = 'big'):
        """
        Initialize parser.

        Args:
            byte_order: 'big' for Big-Endian (default), 'little' for Little-Endian
        """
        self.byte_order = byte_order
        self._endian_prefix = '>' if byte_order == 'big' else '<'

    def get_register_count(self, data_type: str) -> int:
        """Get number of 16-bit registers needed for a data type."""
        return self.REGISTER_COUNTS.get(data_type.lower(), 2)

    def parse_value(self, registers: List[int], data_type: str) -> Optional[Any]:
        """
        Parse register values according to data type.

        Args:
            registers: List of 16-bit register values
            data_type: Data type string

        Returns:
            Parsed value or None if parsing fails
        """
        if not registers:
            return None

        data_type = data_type.lower()

        try:
            if data_type in ('float', 'float32'):
                return self._parse_float(registers)
            elif data_type == 'double':
                return self._parse_double(registers)
            elif data_type == 'int32':
                return self._parse_int32(registers)
            elif data_type == 'uint32':
                return self._parse_uint32(registers)
            elif data_type in ('int16', 'short'):
                return self._parse_int16(registers)
            elif data_type == 'uint16':
                return self._parse_uint16(registers)
            elif data_type in ('int64', 'long64'):
                return self._parse_int64(registers)
            elif data_type == 'uint64':
                return self._parse_uint64(registers)
            else:
                # Default to float
                return self._parse_float(registers)
        except Exception:
            return None

    def _parse_float(self, registers: List[int]) -> Optional[float]:
        """Parse 32-bit IEEE 754 float from 2 registers."""
        if len(registers) < 2:
            return None

        # Combine registers to bytes (Big-Endian: high word first)
        if self.byte_order == 'big':
            raw_bytes = struct.pack('>HH', registers[0], registers[1])
        else:
            raw_bytes = struct.pack('<HH', registers[1], registers[0])

        value = struct.unpack('>f' if self.byte_order == 'big' else '<f', raw_bytes)[0]

        # Check for NaN or Inf
        if value != value or abs(value) == float('inf'):
            return None

        return value

    def _parse_double(self, registers: List[int]) -> Optional[float]:
        """Parse 64-bit IEEE 754 double from 4 registers."""
        if len(registers) < 4:
            return None

        if self.byte_order == 'big':
            raw_bytes = struct.pack('>HHHH', registers[0], registers[1],
                                    registers[2], registers[3])
        else:
            raw_bytes = struct.pack('<HHHH', registers[3], registers[2],
                                    registers[1], registers[0])

        value = struct.unpack('>d' if self.byte_order == 'big' else '<d', raw_bytes)[0]

        if value != value or abs(value) == float('inf'):
            return None

        return value

    def _parse_int32(self, registers: List[int]) -> Optional[int]:
        """Parse signed 32-bit integer from 2 registers."""
        if len(registers) < 2:
            return None

        if self.byte_order == 'big':
            raw_bytes = struct.pack('>HH', registers[0], registers[1])
            return struct.unpack('>i', raw_bytes)[0]
        else:
            raw_bytes = struct.pack('<HH', registers[1], registers[0])
            return struct.unpack('<i', raw_bytes)[0]

    def _parse_uint32(self, registers: List[int]) -> Optional[int]:
        """Parse unsigned 32-bit integer from 2 registers."""
        if len(registers) < 2:
            return None

        if self.byte_order == 'big':
            return (registers[0] << 16) | registers[1]
        else:
            return (registers[1] << 16) | registers[0]

    def _parse_int16(self, registers: List[int]) -> Optional[int]:
        """Parse signed 16-bit integer from 1 register."""
        if len(registers) < 1:
            return None

        value = registers[0]
        if value >= 32768:
            value -= 65536
        return value

    def _parse_uint16(self, registers: List[int]) -> Optional[int]:
        """Parse unsigned 16-bit integer from 1 register."""
        if len(registers) < 1:
            return None
        return registers[0]

    def _parse_int64(self, registers: List[int]) -> Optional[int]:
        """Parse signed 64-bit integer from 4 registers."""
        if len(registers) < 4:
            return None

        if self.byte_order == 'big':
            raw_bytes = struct.pack('>HHHH', registers[0], registers[1],
                                    registers[2], registers[3])
            return struct.unpack('>q', raw_bytes)[0]
        else:
            raw_bytes = struct.pack('<HHHH', registers[3], registers[2],
                                    registers[1], registers[0])
            return struct.unpack('<q', raw_bytes)[0]

    def _parse_uint64(self, registers: List[int]) -> Optional[int]:
        """Parse unsigned 64-bit integer from 4 registers."""
        if len(registers) < 4:
            return None

        if self.byte_order == 'big':
            return ((registers[0] << 48) | (registers[1] << 32) |
                    (registers[2] << 16) | registers[3])
        else:
            return ((registers[3] << 48) | (registers[2] << 32) |
                    (registers[1] << 16) | registers[0])

    def parse_registers(self, all_registers: Dict[int, List[int]],
                        register_configs: List[Dict]) -> Dict[int, Any]:
        """
        Parse multiple registers based on configuration.

        Args:
            all_registers: Dict mapping address -> register values
            register_configs: List of register configurations with address, data_type

        Returns:
            Dict mapping address -> parsed value
        """
        results = {}

        for config in register_configs:
            address = config['address']
            data_type = config.get('data_type', 'float')

            if address in all_registers:
                value = self.parse_value(all_registers[address], data_type)
                results[address] = value

        return results
