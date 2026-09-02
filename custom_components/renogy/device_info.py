"""Home Assistant device information for Renogy devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from renogy_ble.ble import RenogyBLEDevice

from .const import ATTR_MANUFACTURER, DOMAIN


def build_device_info(
    *,
    address: str,
    name: str,
    model: str,
    device: RenogyBLEDevice | None = None,
) -> DeviceInfo:
    """Build device metadata without substituting BLE details for versions."""
    info = DeviceInfo(
        identifiers={(DOMAIN, address)},
        name=name,
        manufacturer=ATTR_MANUFACTURER,
        model=model,
    )
    if device is None or not device.parsed_data:
        return info

    for key in ("sw_version", "hw_version", "serial_number"):
        if value := device.parsed_data.get(key):
            info[key] = str(value)
    return info
