"""Firmware update entity for Renogy controllers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .ble import RenogyActiveBluetoothCoordinator
from .const import CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE, DOMAIN, LOGGER, DeviceType
from .device_info import build_device_info
from .firmware import (
    CONTROLLER_TYPE_ID,
    ROVER_30_SKU,
    RenogyFirmwareAuthStore,
    RenogyFirmwareClient,
    RenogyFirmwareError,
    RenogyFirmwareRelease,
    firmware_identity_uuid,
)

FIRMWARE_CHECK_INTERVAL = timedelta(hours=12)
FIRMWARE_INITIAL_CHECK_DELAY_SECONDS = 30
FIRMWARE_ERROR_RETRY_SECONDS = 15 * 60
POST_UPDATE_RETRY_SECONDS = 10
POST_UPDATE_RETRIES = 9


class RenogyFirmwareManager:
    """Coordinate firmware catalog checks without delaying HA startup."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: RenogyActiveBluetoothCoordinator,
    ) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.store = RenogyFirmwareAuthStore(hass, entry.entry_id)
        self.client = RenogyFirmwareClient(
            async_get_clientsession(hass),
            identity_uuid=firmware_identity_uuid(entry.entry_id),
        )
        self.release: RenogyFirmwareRelease | None = None
        self.last_error: str | None = None
        self.catalog_checked = False
        self._listeners: list[Callable[[], None]] = []
        self._refresh_lock = asyncio.Lock()
        self._initial_refresh_task: asyncio.Task[Any] | None = None
        self._unsub_interval: Callable[[], None] | None = None
        self._unsub_started: Callable[[], None] | None = None
        self._last_refresh_attempt: float | None = None

    async def async_load(self) -> None:
        """Load stored tokens without contacting Renogy."""
        self.client.auth = await self.store.async_load()

    def async_start(self) -> Callable[[], None]:
        """Schedule catalog checks only after Home Assistant is running."""
        if self.hass.state == CoreState.running:
            self._schedule_initial_refresh()
        else:

            @callback
            def _async_started(_event: Any) -> None:
                self._unsub_started = None
                self._schedule_initial_refresh()

            from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

            self._unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _async_started
            )
        self._unsub_interval = async_track_time_interval(
            self.hass, self._async_interval_refresh, FIRMWARE_CHECK_INTERVAL
        )
        return self.async_stop

    @callback
    def async_stop(self) -> None:
        """Cancel firmware callbacks and tasks."""
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._initial_refresh_task is not None:
            self._initial_refresh_task.cancel()
            self._initial_refresh_task = None
        self._listeners.clear()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe an update entity to manager state changes."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def async_device_updated(self) -> None:
        """Publish installed-version changes and retry an initially skipped check."""
        self._notify_listeners()
        retry_due = (
            self._last_refresh_attempt is None
            or time.monotonic() - self._last_refresh_attempt
            >= FIRMWARE_ERROR_RETRY_SECONDS
        )
        if not self.catalog_checked and self.client.auth is not None and retry_due:
            self._schedule_initial_refresh(delay=2)

    async def async_refresh(self) -> None:
        """Refresh the official firmware catalog."""
        if self.client.auth is None or self._controller_sku() is None:
            self._notify_listeners()
            return
        async with self._refresh_lock:
            self._last_refresh_attempt = time.monotonic()
            try:
                self.release = await self.client.async_get_latest_release(
                    ROVER_30_SKU, CONTROLLER_TYPE_ID
                )
                self.last_error = None
                self.catalog_checked = True
                if self.client.auth is not None:
                    await self.store.async_save(self.client.auth)
            except RenogyFirmwareError as err:
                self.last_error = str(err)
                self.catalog_checked = False
                LOGGER.warning("Renogy firmware check failed: %s", err)
            self._notify_listeners()

    def _controller_sku(self) -> str | None:
        """Restrict OTA to the exact controller family proven from Renogy's app."""
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        model = str(data.get("model") or "").upper().replace(" ", "")
        if model and (model == ROVER_30_SKU or "RVR30" in model):
            return ROVER_30_SKU
        return None

    @callback
    def _schedule_initial_refresh(
        self, delay: int = FIRMWARE_INITIAL_CHECK_DELAY_SECONDS
    ) -> None:
        if (
            self._initial_refresh_task is not None
            and not self._initial_refresh_task.done()
        ):
            return
        self._initial_refresh_task = self.hass.async_create_task(
            self._async_delayed_refresh(delay)
        )

    async def _async_delayed_refresh(self, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            await self.async_refresh()
        finally:
            self._initial_refresh_task = None

    async def _async_interval_refresh(self, _now: Any) -> None:
        await self.async_refresh()

    @callback
    def _notify_listeners(self) -> None:
        for listener in tuple(self._listeners):
            listener()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware update entity for supported controllers."""
    if (
        entry.data.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
        != DeviceType.CONTROLLER.value
    ):
        return
    entry_data = hass.data[DOMAIN][entry.entry_id]
    manager = RenogyFirmwareManager(hass, entry, entry_data["coordinator"])
    await manager.async_load()
    entry_data["firmware_manager"] = manager
    entry.async_on_unload(manager.async_start())
    async_add_entities([RenogyControllerFirmwareUpdate(manager)])


class RenogyControllerFirmwareUpdate(UpdateEntity):
    """Represent official firmware offered for a Renogy Rover 30."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Firmware"
    _attr_title = "Renogy Rover firmware"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )
    _attr_should_poll = False

    def __init__(self, manager: RenogyFirmwareManager) -> None:
        """Initialize the update entity."""
        self.manager = manager
        self.coordinator = manager.coordinator
        self._attr_unique_id = f"{self.coordinator.address}_firmware"
        self._attr_in_progress = False
        self._attr_update_percentage: float | None = None
        self._attr_device_info = build_device_info(
            address=self.coordinator.address,
            name=f"Renogy {DeviceType.CONTROLLER.value.capitalize()}",
            model=ROVER_30_SKU,
            device=self.coordinator.device,
        )
        self._remove_manager_listener: Callable[[], None] | None = None
        self._remove_coordinator_listener: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        """Expose the entity after the exact supported model has been identified."""
        return self.manager._controller_sku() is not None

    @property
    def installed_version(self) -> str | None:
        """Return the version read directly from controller register 0x0014."""
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        value = data.get("sw_version")
        return str(value) if value else None

    @property
    def latest_version(self) -> str | None:
        """Return the version currently offered by Renogy."""
        return self.manager.release.version if self.manager.release else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose catalog status without leaking account credentials."""
        return {
            "firmware_sku": ROVER_30_SKU,
            "catalog_configured": self.manager.client.auth is not None,
            "catalog_checked": self.manager.catalog_checked,
            "catalog_error": self.manager.last_error,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller and catalog changes."""
        await super().async_added_to_hass()
        self._remove_manager_listener = self.manager.async_add_listener(
            self.async_write_ha_state
        )
        self._remove_coordinator_listener = self.coordinator.async_add_listener(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Release entity listeners."""
        if self._remove_manager_listener is not None:
            self._remove_manager_listener()
        if self._remove_coordinator_listener is not None:
            self._remove_coordinator_listener()
        await super().async_will_remove_from_hass()

    async def async_update(self) -> None:
        """Refresh the catalog when Home Assistant explicitly requests it."""
        await self.manager.async_refresh()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the exact firmware image offered by Renogy."""
        del backup, kwargs
        if self._attr_in_progress or self.coordinator.firmware_update_in_progress:
            raise HomeAssistantError("A Renogy firmware update is already in progress")

        release = self.manager.release
        if release is None:
            raise HomeAssistantError("No Renogy firmware release is available")
        if version is not None and version != release.version:
            raise HomeAssistantError("Renogy did not offer the requested version")

        self._attr_in_progress = True
        self._attr_update_percentage = 0
        self.async_write_ha_state()
        try:
            firmware = await self.manager.client.async_download(release)
            await self.coordinator.async_install_firmware(
                firmware, self._async_set_progress
            )
            await self._async_verify_installed_version(release.version)
        except RenogyFirmwareError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()

    @callback
    def _async_set_progress(self, value: float) -> None:
        self._attr_update_percentage = round(value, 1)
        self.async_write_ha_state()

    async def _async_verify_installed_version(self, expected: str) -> None:
        """Reconnect after the controller reboots and verify the exact version."""
        for _attempt in range(POST_UPDATE_RETRIES):
            await asyncio.sleep(POST_UPDATE_RETRY_SECONDS)
            self.coordinator._last_controller_static_attempt = 0
            await self.coordinator.async_request_refresh()
            if self._normalized_version(
                self.installed_version
            ) == self._normalized_version(expected):
                return
        raise HomeAssistantError(
            f"Controller did not report firmware {expected} after the update"
        )

    @staticmethod
    def _normalized_version(version: str | None) -> str:
        """Normalize Renogy's optional leading V for exact verification."""
        return (version or "").strip().lower().removeprefix("v")
