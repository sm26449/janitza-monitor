"""Modbus TCP Client for Janitza UMG 512-PRO."""

import time
import logging
import threading
from typing import Dict, List, Optional, Callable, Any

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .config import ModbusConfig, SelectedRegister, PollGroup
from .register_parser import RegisterParser

# Suppress pymodbus exception logging
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)


class ModbusConnection:
    """Modbus TCP connection with thread-safe access."""

    def __init__(self, config: ModbusConfig):
        self.config = config
        self.client: ModbusTcpClient = None
        self.connected = False
        self.lock = threading.Lock()
        self.successful_reads = 0
        self.failed_reads = 0

    def connect(self) -> bool:
        """Establish Modbus TCP connection."""
        try:
            self.client = ModbusTcpClient(
                host=self.config.host,
                port=self.config.port,
                timeout=self.config.timeout
            )
            self.connected = self.client.connect()
            if self.connected:
                logger.info(f"Modbus connected to {self.config.host}:{self.config.port}")
            return self.connected
        except Exception as e:
            logger.error(f"Modbus connection error: {e}")
            return False

    def disconnect(self):
        """Close Modbus connection."""
        with self.lock:
            if self.client:
                self.client.close()
            self.connected = False
            logger.info("Modbus disconnected")

    def read_registers(self, address: int, count: int) -> Optional[List[int]]:
        """Read holding registers with thread-safe access and retry logic."""
        with self.lock:
            for attempt in range(self.config.retry_attempts):
                try:
                    # Reconnect if needed
                    if not self.connected or not self.client.is_socket_open():
                        self.client = ModbusTcpClient(
                            host=self.config.host,
                            port=self.config.port,
                            timeout=self.config.timeout
                        )
                        self.connected = self.client.connect()
                        if not self.connected:
                            time.sleep(0.1)
                            continue

                    # Janitza uses 0-based addressing in documentation
                    # but Modbus protocol is 0-indexed, so we use address directly
                    result = self.client.read_holding_registers(
                        address=address,
                        count=count,
                        slave=self.config.unit_id
                    )

                    if not result.isError() and result.registers:
                        self.successful_reads += 1
                        return result.registers
                    elif result.isError():
                        if attempt < self.config.retry_attempts - 1:
                            time.sleep(self.config.retry_delay)

                except Exception as e:
                    logger.debug(f"Read error at address {address}: {e}")
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        time.sleep(self.config.retry_delay)

            self.failed_reads += 1
            return None


class RegisterPoller(threading.Thread):
    """
    Polling thread for a specific poll group.

    Each poll group (realtime, normal, slow) has its own interval and registers.
    """

    def __init__(self, name: str, interval: int, registers: List[SelectedRegister],
                 connection: ModbusConnection, parser: RegisterParser,
                 publish_callback: Callable):
        super().__init__(daemon=True, name=f"Poller-{name}")
        self.poll_group_name = name
        self.interval = interval
        self.registers = registers
        self.connection = connection
        self.parser = parser
        self.publish_callback = publish_callback

        self.running = False
        self._stop_event = threading.Event()

        # Poll rate tracking
        self.poll_count = 0
        self.last_poll_time = None

        # Optimize reads by grouping consecutive addresses
        self._read_groups = self._create_read_groups()

    def _create_read_groups(self) -> List[Dict]:
        """
        Group consecutive register addresses for optimized batch reads.

        Returns list of groups with start address, count, and register configs.
        """
        if not self.registers:
            return []

        # Sort by address
        sorted_regs = sorted(self.registers, key=lambda r: r.address)

        groups = []
        current_group = None

        for reg in sorted_regs:
            reg_count = self.parser.get_register_count(reg.data_type)

            if current_group is None:
                # Start new group
                current_group = {
                    'start': reg.address,
                    'end': reg.address + reg_count,
                    'registers': [reg]
                }
            elif reg.address <= current_group['end'] + 10:
                # Extend group (allow small gaps up to 10 registers)
                current_group['end'] = max(current_group['end'], reg.address + reg_count)
                current_group['registers'].append(reg)
            else:
                # Gap too large, save current and start new
                groups.append(current_group)
                current_group = {
                    'start': reg.address,
                    'end': reg.address + reg_count,
                    'registers': [reg]
                }

        if current_group:
            groups.append(current_group)

        # Add count to each group
        for g in groups:
            g['count'] = g['end'] - g['start']

        return groups

    def _poll_registers(self) -> Dict[int, Any]:
        """
        Poll all registers in this group.

        Returns dict mapping address -> parsed value.
        """
        results = {}

        for group in self._read_groups:
            raw_data = self.connection.read_registers(group['start'], group['count'])

            if raw_data is None:
                logger.warning(f"Failed to read registers {group['start']}-{group['end']}")
                continue

            # Parse each register in this group
            for reg in group['registers']:
                offset = reg.address - group['start']
                reg_count = self.parser.get_register_count(reg.data_type)

                if offset + reg_count <= len(raw_data):
                    reg_values = raw_data[offset:offset + reg_count]
                    value = self.parser.parse_value(reg_values, reg.data_type)
                    if value is not None:
                        results[reg.address] = {
                            'value': value,
                            'register': reg,
                        }

        return results

    def run(self):
        # Create event loop for this thread (required by pymodbus 3.x)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        self.running = True
        reg_addrs = [r.address for r in self.registers]
        logger.info(f"Poller {self.poll_group_name}: started with {len(self.registers)} registers, interval {self.interval}s")
        logger.debug(f"Poller {self.poll_group_name}: addresses {reg_addrs[:10]}...")

        while self.running:
            try:
                data = self._poll_registers()

                if data:
                    self.publish_callback(self.poll_group_name, data)
                    self.poll_count += 1
                    self.last_poll_time = time.time()
                    logger.debug(f"Poller {self.poll_group_name}: read {len(data)} values")

            except Exception as e:
                import traceback
                logger.error(f"Poller {self.poll_group_name} error: {e}\n{traceback.format_exc()}")

            # Sleep for interval
            self._stop_event.wait(self.interval)

        logger.info(f"Poller {self.poll_group_name}: stopped")

    def stop(self):
        self.running = False
        self._stop_event.set()


class ModbusClient:
    """
    Main Modbus client for Janitza UMG 512-PRO.

    Features:
    - Multiple poll groups with different intervals
    - Optimized batch reads for consecutive registers
    - Thread-safe connection sharing
    - Automatic reconnection
    """

    def __init__(self, config: ModbusConfig, registers: List[SelectedRegister],
                 poll_groups: Dict[str, PollGroup], publish_callback: Callable = None):
        """
        Initialize Modbus client.

        Args:
            config: Modbus connection configuration
            registers: List of registers to poll
            poll_groups: Dict of poll group configurations
            publish_callback: Callback for publishing data (poll_group, data)
        """
        self.config = config
        self.registers = registers
        self.poll_groups = poll_groups
        self.publish_callback = publish_callback or (lambda *args: None)

        self.parser = RegisterParser()
        self.connection = ModbusConnection(config)

        self.pollers: List[RegisterPoller] = []
        self.connected = False

    def connect(self) -> bool:
        """Connect to Janitza device."""
        self.connected = self.connection.connect()
        return self.connected

    def disconnect(self):
        """Disconnect and stop all pollers."""
        for poller in self.pollers:
            poller.stop()
            poller.join(timeout=5)

        self.connection.disconnect()
        self.connected = False

    def start_polling(self):
        """Start polling threads for each poll group."""
        # Group registers by poll group
        registers_by_group: Dict[str, List[SelectedRegister]] = {}
        for reg in self.registers:
            group_name = reg.poll_group
            if group_name not in registers_by_group:
                registers_by_group[group_name] = []
            registers_by_group[group_name].append(reg)

        # Create poller for each group with registers
        for group_name, regs in registers_by_group.items():
            if group_name not in self.poll_groups:
                logger.warning(f"Unknown poll group: {group_name}, using 'normal'")
                group_name = 'normal'

            group_config = self.poll_groups.get(group_name)
            if not group_config:
                continue

            poller = RegisterPoller(
                name=group_name,
                interval=group_config.interval,
                registers=regs,
                connection=self.connection,
                parser=self.parser,
                publish_callback=self.publish_callback
            )
            poller.start()
            self.pollers.append(poller)

        logger.info(f"Started {len(self.pollers)} polling threads")

    def read_register(self, address: int, data_type: str = 'float') -> Optional[Any]:
        """
        Read a single register (for on-demand queries).

        Args:
            address: Register address
            data_type: Data type for parsing

        Returns:
            Parsed value or None
        """
        count = self.parser.get_register_count(data_type)
        raw_data = self.connection.read_registers(address, count)

        if raw_data:
            return self.parser.parse_value(raw_data, data_type)
        return None

    def read_registers_batch(self, registers: List[Dict]) -> Dict[int, Any]:
        """
        Read multiple registers in optimized batches.

        Args:
            registers: List of dicts with 'address' and 'data_type'

        Returns:
            Dict mapping address -> parsed value
        """
        if not registers:
            return {}

        # Sort by address
        sorted_regs = sorted(registers, key=lambda r: r['address'])

        results = {}
        current_batch_start = None
        current_batch_end = None
        current_batch_regs = []

        def flush_batch():
            nonlocal current_batch_start, current_batch_end, current_batch_regs
            if current_batch_start is None:
                return

            count = current_batch_end - current_batch_start
            raw_data = self.connection.read_registers(current_batch_start, count)

            if raw_data:
                for reg in current_batch_regs:
                    offset = reg['address'] - current_batch_start
                    reg_count = self.parser.get_register_count(reg.get('data_type', 'float'))
                    if offset + reg_count <= len(raw_data):
                        reg_values = raw_data[offset:offset + reg_count]
                        value = self.parser.parse_value(reg_values, reg.get('data_type', 'float'))
                        if value is not None:
                            results[reg['address']] = value

            current_batch_start = None
            current_batch_end = None
            current_batch_regs = []

        # Group into batches
        for reg in sorted_regs:
            reg_count = self.parser.get_register_count(reg.get('data_type', 'float'))
            reg_end = reg['address'] + reg_count

            if current_batch_start is None:
                current_batch_start = reg['address']
                current_batch_end = reg_end
                current_batch_regs = [reg]
            elif reg['address'] <= current_batch_end + 10:
                # Extend batch
                current_batch_end = max(current_batch_end, reg_end)
                current_batch_regs.append(reg)
            else:
                # Flush and start new
                flush_batch()
                current_batch_start = reg['address']
                current_batch_end = reg_end
                current_batch_regs = [reg]

        flush_batch()
        return results

    def update_config(self, new_config: ModbusConfig):
        """Update Modbus configuration."""
        self.config = new_config
        self.connection.config = new_config
        logger.info(f"Modbus config updated: {new_config.host}:{new_config.port}")

    def update_registers(self, registers: List[SelectedRegister], poll_groups: Dict[str, PollGroup]):
        """Update register list and poll groups."""
        self.registers = registers
        self.poll_groups = poll_groups
        logger.info(f"Modbus registers updated: {len(registers)} registers")

    def reconnect(self) -> bool:
        """
        Reconnect to Modbus device with current config.
        Stops pollers, disconnects, reconnects, and restarts pollers.
        """
        logger.info("Modbus reconnecting...")

        # Stop all pollers
        for poller in self.pollers:
            poller.stop()
        for poller in self.pollers:
            poller.join(timeout=5)
        self.pollers.clear()

        # Disconnect
        self.connection.disconnect()
        self.connected = False

        # Create new connection with current config
        self.connection = ModbusConnection(self.config)

        # Reconnect
        self.connected = self.connection.connect()
        if self.connected:
            self.start_polling()
            logger.info("Modbus reconnected successfully")
        else:
            logger.warning("Modbus reconnection failed")

        return self.connected

    def reload_registers(self):
        """Reload registers and restart pollers without full reconnect."""
        logger.info("Reloading Modbus registers...")

        # Stop all pollers
        for poller in self.pollers:
            poller.stop()
        for poller in self.pollers:
            poller.join(timeout=5)
        self.pollers.clear()

        # Restart pollers with updated registers
        if self.connected:
            self.start_polling()
            logger.info("Modbus registers reloaded")

    def get_stats(self) -> Dict:
        """Return client statistics."""
        # Calculate poll rate (polls per second across all pollers)
        total_polls = sum(p.poll_count for p in self.pollers)

        # Calculate theoretical poll rate based on intervals
        # Each poller contributes 1/interval polls per second
        poll_rate = 0.0
        for poller in self.pollers:
            if poller.running and poller.interval > 0:
                poll_rate += 1.0 / poller.interval

        return {
            'connected': self.connected,
            'host': self.config.host,
            'port': self.config.port,
            'unit_id': self.config.unit_id,
            'successful_reads': self.connection.successful_reads,
            'failed_reads': self.connection.failed_reads,
            'errors': self.connection.failed_reads,
            'poll_groups': len(self.pollers),
            'total_registers': len(self.registers),
            'total_polls': total_polls,
            'poll_rate': round(poll_rate, 2),
        }
