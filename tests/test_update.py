"""Tests for the Renogy controller firmware update manager."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from enum import Enum, IntFlag
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_update_module() -> Any:
    """Load update.py with focused Home Assistant and integration stubs."""
    homeassistant = cast(Any, types.ModuleType("homeassistant"))
    components = cast(Any, types.ModuleType("homeassistant.components"))
    update_component = cast(Any, types.ModuleType("homeassistant.components.update"))

    class UpdateDeviceClass:
        FIRMWARE = "firmware"

    class UpdateEntityFeature(IntFlag):
        INSTALL = 1
        PROGRESS = 2

    class UpdateEntity:
        async def async_added_to_hass(self) -> None:
            return None

        async def async_will_remove_from_hass(self) -> None:
            return None

        def async_write_ha_state(self) -> None:
            return None

    update_component.UpdateDeviceClass = UpdateDeviceClass
    update_component.UpdateEntityFeature = UpdateEntityFeature
    update_component.UpdateEntity = UpdateEntity

    config_entries = cast(Any, types.ModuleType("homeassistant.config_entries"))
    config_entries.ConfigEntry = object

    core = cast(Any, types.ModuleType("homeassistant.core"))

    class CoreState(str, Enum):
        running = "running"

    core.CoreState = CoreState
    core.HomeAssistant = object
    core.callback = lambda func: func

    exceptions = cast(Any, types.ModuleType("homeassistant.exceptions"))
    exceptions.HomeAssistantError = RuntimeError

    helpers = cast(Any, types.ModuleType("homeassistant.helpers"))
    aiohttp_client = cast(Any, types.ModuleType("homeassistant.helpers.aiohttp_client"))
    aiohttp_client.async_get_clientsession = MagicMock(return_value=MagicMock())
    entity = cast(Any, types.ModuleType("homeassistant.helpers.entity"))

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    entity.EntityCategory = EntityCategory
    entity_platform = cast(
        Any, types.ModuleType("homeassistant.helpers.entity_platform")
    )
    entity_platform.AddEntitiesCallback = object
    event = cast(Any, types.ModuleType("homeassistant.helpers.event"))
    event.async_track_time_interval = MagicMock(return_value=lambda: None)

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.update": update_component,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.helpers.entity": entity,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.helpers.event": event,
    }
    sys.modules.update(modules)

    repo_root = Path(__file__).resolve().parents[1]
    custom_components = types.ModuleType("custom_components")
    custom_components.__path__ = [str(repo_root / "custom_components")]
    renogy = types.ModuleType("custom_components.renogy")
    renogy.__path__ = [str(repo_root / "custom_components" / "renogy")]
    sys.modules["custom_components"] = custom_components
    sys.modules["custom_components.renogy"] = renogy

    ble = cast(Any, types.ModuleType("custom_components.renogy.ble"))
    ble.RenogyActiveBluetoothCoordinator = object
    sys.modules["custom_components.renogy.ble"] = ble

    const = cast(Any, types.ModuleType("custom_components.renogy.const"))
    const.CONF_DEVICE_TYPE = "device_type"
    const.DEFAULT_DEVICE_TYPE = "controller"
    const.DOMAIN = "renogy"
    const.LOGGER = MagicMock()

    class DeviceType(str, Enum):
        CONTROLLER = "controller"

    const.DeviceType = DeviceType
    sys.modules["custom_components.renogy.const"] = const

    device_info = cast(Any, types.ModuleType("custom_components.renogy.device_info"))
    device_info.build_device_info = MagicMock(return_value={})
    sys.modules["custom_components.renogy.device_info"] = device_info

    firmware = cast(Any, types.ModuleType("custom_components.renogy.firmware"))
    firmware.CONTROLLER_TYPE_ID = 14
    firmware.ROVER_30_SKU = "RNG-CTRL-RVR30"

    class RenogyFirmwareError(Exception):
        pass

    @dataclass
    class RenogyFirmwareRelease:
        version: str
        url: str
        md5: str
        sku: str

    class RenogyFirmwareAuthStore:
        def __init__(self, _hass: Any, _entry_id: str) -> None:
            pass

        async def async_load(self) -> Any:
            return None

        async def async_save(self, _auth: Any) -> None:
            return None

    class RenogyFirmwareClient:
        def __init__(self, _session: Any, **_kwargs: Any) -> None:
            self.auth = None

    firmware.RenogyFirmwareError = RenogyFirmwareError
    firmware.RenogyFirmwareRelease = RenogyFirmwareRelease
    firmware.RenogyFirmwareAuthStore = RenogyFirmwareAuthStore
    firmware.RenogyFirmwareClient = RenogyFirmwareClient
    firmware.firmware_identity_uuid = lambda entry_id: f"identity-{entry_id}"
    sys.modules["custom_components.renogy.firmware"] = firmware

    name = "custom_components.renogy.update"
    sys.modules.pop(name, None)
    path = repo_root / "custom_components" / "renogy" / "update.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _manager(module: Any) -> Any:
    """Build a manager with a recognized Rover 30 coordinator."""
    hass = MagicMock()
    entry = MagicMock(entry_id="entry-1")
    coordinator = MagicMock(data={"model": "RNG-CTRL-RVR30"})
    return module.RenogyFirmwareManager(hass, entry, coordinator)


def test_no_update_result_does_not_schedule_on_every_device_poll() -> None:
    """A successful empty catalog result must not hammer Renogy's API."""
    module = _load_update_module()
    manager = _manager(module)
    manager.client.auth = object()
    manager.catalog_checked = True
    manager.release = None
    manager._schedule_initial_refresh = MagicMock()

    manager.async_device_updated()

    manager._schedule_initial_refresh.assert_not_called()


def test_catalog_failure_retry_is_rate_limited() -> None:
    """Telemetry updates retry a failed catalog check no more than every 15 minutes."""
    module = _load_update_module()
    manager = _manager(module)
    manager.client.auth = object()
    manager.catalog_checked = False
    manager._last_refresh_attempt = 100.0
    manager._schedule_initial_refresh = MagicMock()

    with patch.object(module.time, "monotonic", return_value=999.0):
        manager.async_device_updated()
    manager._schedule_initial_refresh.assert_not_called()

    with patch.object(module.time, "monotonic", return_value=1000.0):
        manager.async_device_updated()
    manager._schedule_initial_refresh.assert_called_once_with(delay=2)


def test_successful_empty_catalog_marks_check_complete() -> None:
    """No offered firmware is a completed check, not a retryable failure."""
    module = _load_update_module()
    manager = _manager(module)
    manager.client.auth = object()
    manager.client.async_get_latest_release = AsyncMock(return_value=None)
    manager.store.async_save = AsyncMock()

    asyncio.run(manager.async_refresh())

    assert manager.release is None
    assert manager.catalog_checked is True
    assert manager.last_error is None
    manager.store.async_save.assert_awaited_once()


def test_firmware_update_entity_is_diagnostic() -> None:
    """Firmware belongs in the device's diagnostic entity group."""
    module = _load_update_module()

    assert (
        module.RenogyControllerFirmwareUpdate._attr_entity_category
        == module.EntityCategory.DIAGNOSTIC
    )


def test_firmware_update_entity_rejects_duplicate_install() -> None:
    """A second service call must not queue another controller flash."""
    module = _load_update_module()
    manager = _manager(module)
    manager.coordinator.firmware_update_in_progress = True
    entity = module.RenogyControllerFirmwareUpdate(manager)

    with pytest.raises(module.HomeAssistantError, match="already in progress"):
        asyncio.run(entity.async_install(None, False))
