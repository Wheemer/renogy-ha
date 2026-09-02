"""Wired Modbus RTU communication for Renogy controllers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DeviceType,
)

BATTERY_TYPE = {
    1: "open",
    2: "sealed",
    3: "gel",
    4: "lithium",
    5: "self-customized",
}

CHARGING_STATE = {
    0: "deactivated",
    1: "activated",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "floating",
    6: "current limiting",
}

LOAD_STATUS = {
    0: "off",
    1: "on",
}


@dataclass
class RenogySerialDevice:
    """Device shape shared with the existing Renogy entities."""

    address: str
    name: str
    device_type: str = DeviceType.CONTROLLER.value
    parsed_data: dict[str, Any] = field(default_factory=dict)
    is_available: bool = True
    rssi: None = None

    def update_availability(
        self, available: bool, _error: Exception | None = None
    ) -> None:
        """Update device availability."""
        self.is_available = available


class RenogySerialCoordinator:
    """Poll a Renogy controller over wired Modbus RTU."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        port: str,
        slave_address: int,
        baudrate: int = 9600,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        device_type: str = DeviceType.CONTROLLER.value,
    ) -> None:
        """Initialize the serial coordinator."""
        self.hass = hass
        self.logger = logger
        self.port = port
        self.slave_address = slave_address
        self.baudrate = baudrate
        self.scan_interval = scan_interval
        self.device_type = device_type
        self.address = f"serial://{port}/{slave_address}"
        self.device = RenogySerialDevice(
            address=self.address,
            name=f"Renogy {port} #{slave_address}",
            device_type=device_type,
        )
        self.data: dict[str, Any] = {}
        self.last_update_success = True
        self.update_interval = timedelta(seconds=scan_interval)
        self._update_listeners: list[Callable[[], None]] = []
        self._unsub_refresh: Callable[[], None] | None = None
        self._connection_lock = asyncio.Lock()

    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any = None
    ) -> Callable[[], None]:
        """Listen for data updates."""
        if update_callback not in self._update_listeners:
            self._update_listeners.append(update_callback)

        def remove_listener() -> None:
            if update_callback in self._update_listeners:
                self._update_listeners.remove(update_callback)

        return remove_listener

    def async_update_listeners(self) -> None:
        """Notify listeners."""
        for update_callback in list(self._update_listeners):
            update_callback()

    def async_start(self) -> Callable[[], None]:
        """Start polling."""
        self.async_stop()
        self._unsub_refresh = async_track_time_interval(
            self.hass, self._handle_refresh_interval, self.update_interval
        )
        self.hass.async_create_task(self.async_request_refresh())
        return self.async_stop

    def async_stop(self) -> None:
        """Stop polling."""
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None
        self._update_listeners = []

    async def async_shutdown(self) -> None:
        """Release resources."""
        self.async_stop()

    async def _handle_refresh_interval(self, _now=None) -> None:
        """Handle polling interval."""
        await self.async_request_refresh()

    async def async_request_refresh(self) -> None:
        """Refresh device data."""
        async with self._connection_lock:
            try:
                data = await self.hass.async_add_executor_job(self._read_controller)
            except Exception as err:
                self.last_update_success = False
                self.device.update_availability(False, err)
                self.logger.warning(
                    "Failed reading Renogy serial device %s: %s",
                    self.address,
                    err,
                )
                self.async_update_listeners()
                return

        self.data = data
        self.device.parsed_data = dict(data)
        self.device.name = data.get("model") or self.device.name
        self.device.update_availability(True)
        self.last_update_success = True
        self.async_update_listeners()

    def _instrument(self) -> Any:
        """Create a configured MinimalModbus instrument."""
        import minimalmodbus
        import serial

        instrument = minimalmodbus.Instrument(self.port, self.slave_address)
        instrument.serial.baudrate = self.baudrate
        instrument.serial.bytesize = 8
        instrument.serial.parity = serial.PARITY_NONE
        instrument.serial.stopbits = 1
        instrument.serial.timeout = 1
        instrument.mode = minimalmodbus.MODE_RTU
        instrument.clear_buffers_before_each_transaction = True
        return instrument

    def _read_controller(self) -> dict[str, Any]:
        """Read the common Renogy Rover controller registers."""
        instrument = self._instrument()
        data: dict[str, Any] = {}

        data["battery_percentage"] = self._read_register(instrument, 0x0100)
        data["battery_voltage"] = self._read_register(instrument, 0x0101, 1)
        temperature = self._read_register(instrument, 0x0103)
        if temperature is not None:
            data["battery_temperature"] = _decode_low_byte_temperature(temperature)
            data["controller_temperature"] = _decode_high_byte_temperature(temperature)

        data["load_voltage"] = self._read_register(instrument, 0x0104, 1)
        data["load_current"] = self._read_register(instrument, 0x0105, 2)
        data["load_power"] = self._read_register(instrument, 0x0106)
        data["pv_voltage"] = self._read_register(instrument, 0x0107, 1)
        data["pv_current"] = self._read_register(instrument, 0x0108, 2)
        data["pv_power"] = self._read_register(instrument, 0x0109)
        load_status = self._read_register(instrument, 0x010A)
        if load_status is not None:
            data["load_status"] = LOAD_STATUS.get(load_status & 0x00FF, load_status)

        data["max_charging_power_today"] = self._read_register(instrument, 0x010F)
        data["max_discharging_power_today"] = self._read_register(instrument, 0x0110)
        data["charging_amp_hours_today"] = self._read_register(instrument, 0x0111)
        data["discharging_amp_hours_today"] = self._read_register(instrument, 0x0112)
        data["power_generation_today"] = self._read_register(instrument, 0x0113)
        data["power_consumption_today"] = self._read_register(instrument, 0x0114)
        data["power_generation_total"] = self._read_long_as_decimal_string(
            instrument, 0x011C
        )

        charging_status = self._read_register(instrument, 0x0120)
        if charging_status is not None:
            data["charging_status"] = CHARGING_STATE.get(
                charging_status & 0x00FF, charging_status
            )

        battery_type = self._read_register(instrument, 0xE004)
        if battery_type is not None:
            data["battery_type"] = BATTERY_TYPE.get(battery_type, battery_type)

        model = self._read_string(instrument, 0x000C, 8)
        if model:
            data["model"] = model
            data["device_id"] = model

        return {key: value for key, value in data.items() if value is not None}

    def _read_register(
        self,
        instrument: Any,
        register: int,
        decimals: int = 0,
        function_code: int = 3,
        signed: bool = False,
    ) -> Any:
        """Read a register and tolerate missing firmware-specific values."""
        try:
            return instrument.read_register(
                register, decimals, function_code, signed=signed
            )
        except Exception as err:
            self.logger.debug(
                "Renogy serial register 0x%04X read failed: %s", register, err
            )
            return None

    def _read_string(
        self, instrument: Any, register: int, number_of_registers: int
    ) -> str | None:
        """Read a string register."""
        try:
            return instrument.read_string(register, number_of_registers).strip()
        except Exception as err:
            self.logger.debug(
                "Renogy serial string 0x%04X read failed: %s", register, err
            )
            return None

    def _read_long_as_decimal_string(self, instrument: Any, register: int) -> int | None:
        """Read a 32-bit counter stored as two Modbus registers."""
        try:
            registers = instrument.read_registers(register, 2, 3)
        except Exception as err:
            self.logger.debug(
                "Renogy serial long 0x%04X read failed: %s", register, err
            )
            return None

        try:
            return int(f"{registers[0]}{registers[1]}")
        except (IndexError, TypeError, ValueError):
            return None

    async def async_set_load_state(self, state: bool) -> bool:
        """Set DC load output state."""
        value = 1 if state else 0
        return await self.async_write_register(0x010A, value)

    async def async_write_register(self, register: int, value: int) -> bool:
        """Write a single Modbus register."""
        async with self._connection_lock:
            try:
                await self.hass.async_add_executor_job(
                    self._write_register, register, value
                )
            except Exception as err:
                self.last_update_success = False
                self.device.update_availability(False, err)
                self.logger.warning(
                    "Failed writing Renogy serial register 0x%04X: %s",
                    register,
                    err,
                )
                return False

        await self.async_request_refresh()
        return True

    def _write_register(self, register: int, value: int) -> None:
        """Write a register using MinimalModbus."""
        instrument = self._instrument()
        instrument.write_register(register, value, functioncode=6)


def _decode_low_byte_temperature(register: int) -> int:
    """Decode low-byte signed Renogy temperature."""
    value = register & 0x00FF
    sign = value >> 7
    magnitude = value & 0x7F
    return -magnitude if sign else magnitude


def _decode_high_byte_temperature(register: int) -> int:
    """Decode high-byte signed Renogy temperature."""
    value = (register >> 8) & 0x00FF
    sign = value >> 7
    magnitude = value & 0x7F
    return -magnitude if sign else magnitude

