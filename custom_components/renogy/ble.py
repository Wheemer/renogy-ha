"""BLE communication module for Renogy devices."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any, cast

from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import clear_cache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from renogy_ble import ble as renogy_ble_module
from renogy_ble.ble import RenogyBleClient, RenogyBLEDevice, clean_device_name

from .const import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_NON_SHUNT_CONNECTION_MODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SHUNT_CONNECTION_MODE,
    DeviceType,
    NonShuntConnectionMode,
    ShuntConnectionMode,
)
from .device_name import (
    detect_device_type_from_ble_name,
    detect_device_type_from_model,
    has_real_device_name,
)

# Check if write_register is available in the library.
try:
    renogy_ble_ble: ModuleType | None = importlib.import_module("renogy_ble.ble")
except ImportError:
    renogy_ble_ble = None

if renogy_ble_ble is not None:
    create_modbus_write_request = getattr(
        renogy_ble_ble, "create_modbus_write_request", None
    )
    HAS_WRITE_SUPPORT = create_modbus_write_request is not None
else:
    create_modbus_write_request = None
    HAS_WRITE_SUPPORT = False

try:
    renogy_ble_shunt: ModuleType | None = importlib.import_module("renogy_ble.shunt")
except ImportError:
    renogy_ble_shunt = None

if renogy_ble_shunt is not None:
    shunt_client_class = getattr(renogy_ble_shunt, "ShuntBleClient", None)
    shunt_find_valid_payload_window = getattr(
        renogy_ble_shunt, "_find_valid_payload_window", None
    )
    shunt_expected_payload_length = getattr(
        renogy_ble_shunt, "SHUNT_EXPECTED_PAYLOAD_LENGTH", None
    )
    shunt_notify_char_uuid = getattr(
        renogy_ble_shunt,
        "SHUNT_NOTIFY_CHAR_UUID",
        "0000c411-0000-1000-8000-00805f9b34fb",
    )
else:
    shunt_client_class = None
    shunt_find_valid_payload_window = None
    shunt_expected_payload_length = None
    shunt_notify_char_uuid = "0000c411-0000-1000-8000-00805f9b34fb"

LOAD_CONTROL_REGISTER = getattr(renogy_ble_module, "LOAD_CONTROL_REGISTER", 0x010A)
SHUNT_RECONNECT_DELAY_SECONDS = 10
SHUNT_FORCE_UPDATE_INTERVAL_SECONDS = 300
SHUNT_DISCONNECT_TIMEOUT_SECONDS = 5.0
SHUNT_STARTUP_READY_TIMEOUT_SECONDS = 30.0
PREFERRED_RENOGY_SOURCE = "E8:48:B8:C8:20:00"
BLE_FAILURE_BACKOFF_STEPS = (60, 300, 900)
STALE_DATA_GRACE_SECONDS = 300
MAX_PREFERRED_ADVERTISEMENT_AGE_SECONDS = 120
MIN_REAL_ADVERTISEMENT_RSSI = -120
CONTROLLER_STATIC_REFRESH_INTERVAL_SECONDS = 60
CONTROLLER_LIVE_COMMAND_NAMES = ("pv",)
CONTROLLER_STATIC_COMMAND_NAMES = ("device_info", "device_id", "battery")


class RenogyActiveBluetoothCoordinator(
    ActiveBluetoothDataUpdateCoordinator[dict[str, Any]]
):
    """Class to manage fetching Renogy BLE data via active connections."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        address: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        device_type: str = DEFAULT_DEVICE_TYPE,
        shunt_connection_mode: str = DEFAULT_SHUNT_CONNECTION_MODE,
        non_shunt_connection_mode: str = DEFAULT_NON_SHUNT_CONNECTION_MODE,
        device_data_callback: Callable[[RenogyBLEDevice], Awaitable[None]]
        | None = None,
    ):
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=logger,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll_device,
            mode=BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self.device: RenogyBLEDevice | None = None
        self.scan_interval = scan_interval
        self.shunt_connection_mode = shunt_connection_mode
        self.non_shunt_connection_mode = non_shunt_connection_mode
        self.device_type = device_type
        self.last_poll_time: datetime | None = None
        self.device_data_callback = device_data_callback
        self.logger.debug(
            "Initialized coordinator for %s as %s with %ss interval "
            "(%s shunt mode, %s non-shunt mode)",
            address,
            device_type,
            scan_interval,
            shunt_connection_mode,
            non_shunt_connection_mode,
        )

        self._ble_client = self._build_ble_client_for_type(device_type)
        self._shunt_listener_task: asyncio.Task[Any] | None = None
        self._shunt_startup_gate_complete = False
        self._last_sustained_shunt_push = 0.0
        self._last_sustained_shunt_data: dict[str, Any] = {}
        self._shunt_energy_client = (
            shunt_client_class() if shunt_client_class is not None else None
        )

        # Add required properties for Home Assistant CoordinatorEntity compatibility
        self.last_update_success = True
        self._update_listeners: list[Callable[[], None]] = []
        self.update_interval = timedelta(seconds=scan_interval)
        self._unsub_refresh = None
        self._request_refresh_task = None

        # Add connection lock to prevent multiple concurrent connections
        self._connection_lock = asyncio.Lock()
        self._connection_in_progress = False
        self._ble_failure_count = 0
        self._ble_backoff_until = 0.0
        self._last_missing_service_log = 0.0
        self._last_success_monotonic = 0.0
        self._last_success_wall_time: datetime | None = None
        self._last_preferred_advertisement_time = 0.0
        self._last_stale_preferred_log = 0.0
        self._last_controller_static_refresh = 0.0

        # Warn only once when the reported model contradicts the configured type
        self._model_mismatch_warned = False

    def _build_ble_client_for_type(self, device_type: str) -> RenogyBleClient:
        """Build a BLE client suitable for the configured device type."""
        scanner = bluetooth.async_get_scanner(self.hass)
        if (
            self._uses_intermittent_shunt_reads(device_type)
            and shunt_client_class is not None
        ):
            return cast(RenogyBleClient, shunt_client_class())

        if self._uses_intermittent_shunt_reads(device_type):
            self.logger.warning(
                "ShuntBleClient not available in installed renogy-ble; "
                "falling back to RenogyBleClient for %s",
                self.address,
            )
        return self._build_generic_ble_client(scanner)

    def _build_generic_ble_client(self, scanner: Any) -> RenogyBleClient:
        """Build the generic library client for the active device mode."""
        client_kwargs: dict[str, Any] = {"scanner": scanner, "max_attempts": 1}
        if self._uses_persistent_non_shunt_session():
            client_kwargs["transport_mode"] = (
                NonShuntConnectionMode.PERSISTENT_SESSION.value
            )

        return RenogyBleClient(**client_kwargs)

    def _client_transport_mode(self) -> str:
        """Return the active transport mode reported by the current BLE client."""
        return getattr(
            self._ble_client,
            "transport_mode",
            getattr(
                self._ble_client,
                "_transport_mode",
                NonShuntConnectionMode.INTERMITTENT.value,
            ),
        )

    def _controller_commands_for_poll(self) -> tuple[dict[str, Any] | None, bool]:
        """Return the controller command subset for this poll.

        Controller identity/config registers are slow-changing. Keep live PV
        telemetry fast while only refreshing static-ish metadata once a minute.
        """
        if self.device_type != DeviceType.CONTROLLER.value:
            return None, False

        commands_by_type = getattr(renogy_ble_module, "COMMANDS", {})
        controller_commands = commands_by_type.get(DeviceType.CONTROLLER.value)
        if not controller_commands:
            return None, False

        now = time.monotonic()
        static_due = (
            not self.data
            or self._last_controller_static_refresh == 0.0
            or now - self._last_controller_static_refresh
            >= CONTROLLER_STATIC_REFRESH_INTERVAL_SECONDS
        )

        command_names = list(CONTROLLER_LIVE_COMMAND_NAMES)
        if static_due:
            command_names.extend(CONTROLLER_STATIC_COMMAND_NAMES)

        commands = {
            name: controller_commands[name]
            for name in command_names
            if name in controller_commands
        }
        return commands or None, static_due

    def _uses_sustained_shunt_listener(self, device_type: str | None = None) -> bool:
        """Return whether this coordinator should keep a sustained shunt listener."""
        resolved_type = device_type or self.device_type
        return (
            resolved_type == DeviceType.SHUNT300.value
            and self.shunt_connection_mode == ShuntConnectionMode.SUSTAINED.value
        )

    def _uses_intermittent_shunt_reads(self, device_type: str | None = None) -> bool:
        """Return whether this coordinator should use intermittent shunt reads."""
        resolved_type = device_type or self.device_type
        return (
            resolved_type == DeviceType.SHUNT300.value
            and self.shunt_connection_mode == ShuntConnectionMode.INTERMITTENT.value
        )

    def _uses_persistent_non_shunt_session(
        self, device_type: str | None = None
    ) -> bool:
        """Return whether persistent mode is enabled for a non-shunt device."""
        resolved_type = device_type or self.device_type
        return (
            resolved_type != DeviceType.SHUNT300.value
            and self.non_shunt_connection_mode
            == NonShuntConnectionMode.PERSISTENT_SESSION.value
        )

    @property
    def device_type(self) -> str:
        """Get the device type from configuration."""
        return self._device_type

    @device_type.setter
    def device_type(self, value: str) -> None:
        """Set the device type."""
        self._device_type = value

    async def async_request_refresh(self) -> None:
        """Request a refresh."""
        self.logger.debug("Manual refresh requested for device %s", self.address)

        if self._uses_sustained_shunt_listener():
            self.logger.debug(
                "Skipping refresh for sustained shunt %s; listener owns updates",
                self.address,
            )
            return

        # If a connection is already in progress, don't start another one
        if self._connection_in_progress:
            self.logger.debug(
                "Connection already in progress, skipping refresh request"
            )
            return

        if self._is_in_ble_backoff():
            self.logger.debug(
                "Skipping Renogy refresh for %s during BLE backoff (%.0fs left)",
                self.address,
                self._ble_backoff_until - time.monotonic(),
            )
            return

        service_info = self._service_info_for_operation()
        if (
            service_info is None
            and not self._can_use_cached_device_without_service_info()
        ):
            self._log_missing_service_info()
            if not self._keep_recent_data_available("missing service info"):
                self.last_update_success = False
            return
        if service_info is None:
            self.logger.debug(
                "No service info available for %s; using cached device context for "
                "persistent session refresh",
                self.address,
            )

        try:
            await self._async_poll_device(service_info)
            self.async_update_listeners()
        except Exception as err:
            self.last_update_success = False
            error_traceback = traceback.format_exc()
            self.logger.debug(
                "Error refreshing device %s: %s\n%s",
                self.address,
                str(err),
                error_traceback,
            )
            if not self._keep_recent_data_available("refresh failure", err):
                if self.device:
                    self.device.update_availability(False, err)

    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any = None
    ) -> Callable[[], None]:
        """Listen for data updates."""
        if update_callback not in self._update_listeners:
            self._update_listeners.append(update_callback)

        def remove_listener() -> None:
            """Remove update callback."""
            if update_callback in self._update_listeners:
                self._update_listeners.remove(update_callback)

        return remove_listener

    def async_update_listeners(self) -> None:
        """Update all registered listeners."""
        for update_callback in self._update_listeners:
            update_callback()

    def _schedule_refresh(self) -> None:
        """Schedule a refresh with the update interval."""
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        # Schedule the next refresh based on our scan interval
        self._unsub_refresh = async_track_time_interval(
            self.hass, self._handle_refresh_interval, self.update_interval
        )
        self.logger.debug("Scheduled next refresh in %s seconds", self.scan_interval)

    async def _handle_refresh_interval(self, _now=None):
        """Handle a refresh interval occurring."""
        self.logger.debug("Regular interval refresh for %s", self.address)
        await self.async_request_refresh()

    def async_start(self) -> Callable[[], None]:
        """Start polling."""
        self.logger.debug("Starting polling for device %s", self.address)

        def _unsub() -> None:
            """Unsubscribe from updates."""
            if self._unsub_refresh:
                self._unsub_refresh()
                self._unsub_refresh = None

        _unsub()  # Cancel any previous subscriptions

        if self._uses_sustained_shunt_listener():
            create_task = getattr(self.hass, "async_create_background_task", None)
            if callable(create_task):
                self._shunt_listener_task = create_task(
                    self._shunt_notification_loop(),
                    name=f"renogy_shunt_{self.address}",
                )
            else:
                self._shunt_listener_task = self.hass.async_create_task(
                    self._shunt_notification_loop()
                )
            return _unsub

        # We use the active update coordinator's start method
        # which already handles the bluetooth subscriptions
        result = super().async_start()

        # Schedule regular refreshes at our configured interval
        self._schedule_refresh()

        # Perform an initial refresh to get data as soon as possible
        self.hass.async_create_task(self.async_request_refresh())

        return result

    def _async_cancel_bluetooth_subscription(self) -> None:
        """Cancel the bluetooth subscription."""
        if hasattr(self, "_unsubscribe_bluetooth") and self._unsubscribe_bluetooth:
            self._unsubscribe_bluetooth()
            self._unsubscribe_bluetooth = None

    def _service_info_for_operation(self) -> BluetoothServiceInfoBleak | None:
        """Return the latest Home Assistant Bluetooth service info, if available."""
        service_info = bluetooth.async_last_service_info(self.hass, self.address)
        self._record_preferred_advertisement(service_info)
        return service_info

    def _log_missing_service_info(self) -> None:
        """Log missing service info without spamming every poll interval."""
        now = time.monotonic()
        if now - self._last_missing_service_log < 120:
            self.logger.debug(
                "No service info available for device %s; waiting for advertisement",
                self.address,
            )
            return
        self._last_missing_service_log = now
        self.logger.error(
            "No service info available for device %s. Ensure device is within "
            "range and powered on.",
            self.address,
        )

    def _is_in_ble_backoff(self) -> bool:
        """Return whether active BLE connects are cooling down after failures."""
        return time.monotonic() < self._ble_backoff_until

    def _record_ble_success(self) -> None:
        """Reset active BLE failure state after a successful read."""
        self._ble_failure_count = 0
        self._ble_backoff_until = 0.0
        self._last_success_monotonic = time.monotonic()
        self._last_success_wall_time = datetime.now()
        self.last_update_success = True

    def _has_recent_good_data(self) -> bool:
        """Return whether cached telemetry is fresh enough to keep exposed."""
        if not isinstance(self.data, dict) or not self.data:
            return False
        if self._last_success_monotonic <= 0:
            return False
        return (time.monotonic() - self._last_success_monotonic) <= STALE_DATA_GRACE_SECONDS

    def _keep_recent_data_available(self, reason: str, error: Exception | None = None) -> bool:
        """Keep entities available through brief BLE read failures."""
        if not self._has_recent_good_data():
            return False
        self.last_update_success = True
        if self.device is not None:
            self.device.update_availability(True, None)
        self.logger.info(
            "Keeping cached Renogy data for %s after transient %s%s",
            self.address,
            reason,
            f": {error}" if error is not None else "",
        )
        return True

    def _record_preferred_advertisement(
        self, service_info: BluetoothServiceInfoBleak | None
    ) -> None:
        """Remember when the preferred local adapter actually heard the device."""
        if service_info is None or service_info.source != PREFERRED_RENOGY_SOURCE:
            return

        if service_info.rssi is None or service_info.rssi <= MIN_REAL_ADVERTISEMENT_RSSI:
            return

        service_time = getattr(service_info, "time", 0.0) or 0.0
        if service_time > self._last_preferred_advertisement_time:
            self._last_preferred_advertisement_time = service_time

    def _preferred_advertisement_is_fresh(self) -> bool:
        """Return whether the preferred adapter has heard this device recently."""
        service_info = bluetooth.async_last_service_info(self.hass, self.address)
        self._record_preferred_advertisement(service_info)
        if self._last_preferred_advertisement_time <= 0:
            return False

        age = time.monotonic() - self._last_preferred_advertisement_time
        if age <= MAX_PREFERRED_ADVERTISEMENT_AGE_SECONDS:
            return True

        now = time.monotonic()
        if now - self._last_stale_preferred_log >= 120:
            self._last_stale_preferred_log = now
            self.logger.info(
                "Renogy %s has not been heard by preferred TP-Link source %s "
                "for %.0fs; skipping active BLE connect until a fresh "
                "advertisement appears",
                self.address,
                PREFERRED_RENOGY_SOURCE,
                age,
            )
        return False

    def _record_ble_failure(self) -> None:
        """Back off active BLE connects after failures."""
        step_index = min(self._ble_failure_count, len(BLE_FAILURE_BACKOFF_STEPS) - 1)
        delay = BLE_FAILURE_BACKOFF_STEPS[step_index]
        self._ble_failure_count += 1
        self._ble_backoff_until = time.monotonic() + delay
        self.logger.info(
            "Renogy BLE read failed for %s; backing off active connects for %ss",
            self.address,
            delay,
        )

    def _scanner_device_for_operation(self) -> Any | None:
        """Return the best current scanner device, preferring the local TP-Link."""
        if not self._preferred_advertisement_is_fresh():
            return None

        scanner_devices = bluetooth.async_scanner_devices_by_address(
            self.hass, self.address, connectable=True
        )
        if not scanner_devices:
            return None

        for scanner_device in scanner_devices:
            scanner_source = getattr(scanner_device.scanner, "source", None)
            if scanner_source == PREFERRED_RENOGY_SOURCE:
                if (
                    scanner_device.advertisement.rssi is None
                    or scanner_device.advertisement.rssi <= MIN_REAL_ADVERTISEMENT_RSSI
                ):
                    self.logger.info(
                        "Renogy %s preferred TP-Link path has stale RSSI %s; "
                        "skipping active BLE connect until a real advertisement "
                        "appears",
                        self.address,
                        scanner_device.advertisement.rssi,
                    )
                    return None
                return scanner_device

        # The Renogy module has shown it can disappear after failed proxy GATT
        # attempts. If the close TP-Link adapter is not currently seeing it, do
        # not poke it through a weaker/stale proxy path.
        strongest = max(
            scanner_devices,
            key=lambda item: item.advertisement.rssi
            if item.advertisement and item.advertisement.rssi is not None
            else -999,
        )
        self.logger.info(
            "Renogy %s is visible via %s (RSSI %s) but not via preferred TP-Link "
            "source %s; waiting instead of connecting through a proxy path",
            self.address,
            getattr(strongest.scanner, "source", None),
            strongest.advertisement.rssi if strongest.advertisement else None,
            PREFERRED_RENOGY_SOURCE,
        )
        return None

    def _update_device_from_scanner_device(self, scanner_device: Any) -> RenogyBLEDevice:
        """Update the cached device from a selected scanner-specific path."""
        ble_device = scanner_device.ble_device
        advertisement = scanner_device.advertisement
        manufacturer_data = getattr(advertisement, "manufacturer_data", {}) or {}
        detected_type = detect_device_type_from_ble_name(
            ble_device.name,
            self.device_type,
            manufacturer_data=manufacturer_data,
        )

        if not self.device:
            self.device = RenogyBLEDevice(
                ble_device,
                advertisement.rssi,
                device_type=detected_type,
                manufacturer_data=manufacturer_data,
            )
        else:
            self.device.ble_device = ble_device
            self.device.manufacturer_data = dict(manufacturer_data)
            if has_real_device_name(ble_device.name):
                self.device.name = clean_device_name(ble_device.name)
            self.device.rssi = advertisement.rssi
            self.device.device_type = detected_type

        self.device_type = detected_type
        return self.device

    def _can_use_cached_device_without_service_info(self) -> bool:
        """Return whether operations can fall back to the cached BLE device."""
        return (
            self.device is not None
            and self._client_transport_mode()
            == NonShuntConnectionMode.PERSISTENT_SESSION.value
        )

    def async_stop(self) -> None:
        """Stop polling."""
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        if self._shunt_listener_task is not None:
            self._shunt_listener_task.cancel()
            self._shunt_listener_task = None

        self._async_cancel_bluetooth_subscription()

        # Clean up any other resources that might need to be released
        self._update_listeners = []

    async def async_shutdown(self) -> None:
        """Stop polling and release any persistent BLE sessions."""
        self.async_stop()

        close_client = getattr(self._ble_client, "close", None)
        if callable(close_client):
            await close_client()

    def _update_device_from_service_info(
        self, service_info: BluetoothServiceInfoBleak
    ) -> RenogyBLEDevice:
        """Ensure the device instance is updated from Bluetooth service info."""
        manufacturer_data = getattr(service_info.advertisement, "manufacturer_data", {})
        if not manufacturer_data and self.device is not None:
            # Some follow-up advertisements omit manufacturer data entirely.
            manufacturer_data = self.device.manufacturer_data
        detected_type = detect_device_type_from_ble_name(
            service_info.name,
            self.device_type,
            manufacturer_data=manufacturer_data,
        )
        if self.device_type != detected_type:
            self.logger.debug(
                "Detected %s device from BLE name: %s",
                detected_type,
                service_info.name,
            )
            self.device_type = detected_type

        if not self.device:
            self.logger.debug(
                "Creating new RenogyBLEDevice for %s as %s",
                service_info.address,
                detected_type,
            )
            self.device = RenogyBLEDevice(
                service_info.device,
                service_info.advertisement.rssi,
                device_type=detected_type,
                manufacturer_data=manufacturer_data,
            )
        else:
            old_name = self.device.name
            self.device.ble_device = service_info.device
            self.device.manufacturer_data = dict(manufacturer_data)
            if has_real_device_name(service_info.name):
                cleaned_name = clean_device_name(service_info.name)
                if old_name != cleaned_name:
                    self.device.name = cleaned_name
                    self.logger.debug(
                        "Updated device name from '%s' to '%s'",
                        old_name,
                        cleaned_name,
                    )

            self.device.rssi = (
                service_info.advertisement.rssi
                if service_info.advertisement
                and service_info.advertisement.rssi is not None
                else service_info.device.rssi
            )

            if self.device.device_type != self.device_type:
                self.logger.debug(
                    "Updating device type from '%s' to '%s'",
                    self.device.device_type,
                    self.device_type,
                )
                self.device.device_type = self.device_type

        if (
            self._uses_intermittent_shunt_reads(self.device.device_type)
            and shunt_client_class is not None
            and not isinstance(self._ble_client, shunt_client_class)
        ):
            self.logger.debug(
                "Switching BLE client to Smart Shunt handler for %s",
                service_info.address,
            )
            self._ble_client = cast(RenogyBleClient, shunt_client_class())
        elif self._uses_sustained_shunt_listener(self.device.device_type) and (
            shunt_client_class is None
            or isinstance(self._ble_client, shunt_client_class)
        ):
            self.logger.debug(
                "Switching BLE client to generic handler for sustained shunt %s",
                service_info.address,
            )
            self._ble_client = RenogyBleClient(
                scanner=bluetooth.async_get_scanner(self.hass)
            )
        elif (
            self.device.device_type != DeviceType.SHUNT300.value
            and self._uses_persistent_non_shunt_session(self.device.device_type)
            and self._client_transport_mode()
            != NonShuntConnectionMode.PERSISTENT_SESSION.value
        ):
            self.logger.debug(
                "Switching BLE client to persistent non-shunt mode for %s",
                service_info.address,
            )
            self._ble_client = self._build_generic_ble_client(
                bluetooth.async_get_scanner(self.hass)
            )
        elif (
            self.device.device_type != DeviceType.SHUNT300.value
            and not self._uses_persistent_non_shunt_session(self.device.device_type)
            and self._client_transport_mode()
            == NonShuntConnectionMode.PERSISTENT_SESSION.value
        ):
            self.logger.debug(
                "Switching BLE client to intermittent non-shunt mode for %s",
                service_info.address,
            )
            self._ble_client = self._build_generic_ble_client(
                bluetooth.async_get_scanner(self.hass)
            )

        return self.device

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        last_poll: float | None,
    ) -> bool:
        """Determine if device needs polling based on time since last poll."""
        if self._uses_sustained_shunt_listener():
            return False

        # Only poll if hass is running and device is connectable
        if self.hass.state != CoreState.running:
            return False

        if self._is_in_ble_backoff():
            return False

        # Check if we have a connectable device
        scanner_device = self._scanner_device_for_operation()
        if not scanner_device:
            self.logger.debug(
                "No safe connectable device path found for %s", service_info.address
            )
            return False

        # If a connection is already in progress, don't start another one
        if self._connection_in_progress:
            self.logger.debug("Connection already in progress, skipping poll")
            return False

        # If we've never polled or it's been longer than the scan interval, poll
        if last_poll is None:
            self.logger.debug("First poll for device %s", service_info.address)
            return True

        # Check if enough time has elapsed since the last poll
        time_since_poll = datetime.now().timestamp() - last_poll
        should_poll = time_since_poll >= self.scan_interval

        if should_poll:
            self.logger.debug(
                "Time to poll device %s after %.1fs",
                service_info.address,
                time_since_poll,
            )

        return should_poll

    def _process_sustained_shunt_notification(self, data: bytes) -> bool:
        """Parse and publish one sustained Smart Shunt notification payload."""
        if (
            shunt_find_valid_payload_window is None
            or shunt_expected_payload_length is None
        ):
            return False

        maybe_payload = shunt_find_valid_payload_window(
            data, shunt_expected_payload_length
        )
        if maybe_payload is None:
            return False

        raw_payload, parsed_data = maybe_payload
        now = time.monotonic()
        if self._shunt_energy_client is not None:
            charged_kwh, discharged_kwh = (
                self._shunt_energy_client._integrate_energy_totals(
                    device_address=self.address,
                    power_w=parsed_data.get("shunt_power"),
                    now_ts=now,
                )
            )
            parsed_data["energy_charged_total"] = round(charged_kwh, 3)
            parsed_data["energy_discharged_total"] = round(discharged_kwh, 3)
        parsed_data["raw_payload"] = raw_payload.hex()
        parsed_data["raw_words"] = [
            int.from_bytes(raw_payload[i : i + 2], "big", signed=False)
            for i in range(0, len(raw_payload), 2)
        ]

        changed = any(
            parsed_data.get(key) != self._last_sustained_shunt_data.get(key)
            for key in (
                "shunt_voltage",
                "shunt_current",
                "shunt_power",
                "shunt_soc",
                "energy_charged_total",
                "energy_discharged_total",
            )
        )
        stale = (
            now - self._last_sustained_shunt_push >= SHUNT_FORCE_UPDATE_INTERVAL_SECONDS
        )
        # Keep the recovery path alive after a transient listener failure even
        # when the first restored payload matches the previous values.
        if not changed and not stale and self.last_update_success:
            return True

        if self.device is not None:
            existing_data = (
                dict(self.device.parsed_data)
                if isinstance(self.device.parsed_data, dict)
                else {}
            )
            existing_data.update(parsed_data)
            self.device.parsed_data = existing_data
            self.device.update_availability(True, None)

        current_data = dict(self.data) if isinstance(self.data, dict) else {}
        current_data.update(parsed_data)
        self.data = current_data
        self.last_update_success = True
        self._last_sustained_shunt_data = dict(parsed_data)
        self._last_sustained_shunt_push = now
        self.hass.loop.call_soon_threadsafe(self.async_update_listeners)
        return True

    async def _async_disconnect_shunt_client(self, client: Any) -> None:
        """Attempt to disconnect a shunt listener client without hanging."""
        disconnect = getattr(client, "disconnect", None)
        if not callable(disconnect):
            return

        try:
            await asyncio.wait_for(
                disconnect(), timeout=SHUNT_DISCONNECT_TIMEOUT_SECONDS
            )
        except Exception:
            pass

    def _schedule_shunt_disconnect(self, client: Any) -> None:
        """Schedule shunt disconnect cleanup without blocking task cancellation."""
        create_task = getattr(self.hass, "async_create_background_task", None)
        if callable(create_task):
            create_task(
                self._async_disconnect_shunt_client(client),
                name=f"renogy_shunt_disconnect_{self.address}",
            )
            return

        self.hass.async_create_task(self._async_disconnect_shunt_client(client))

    def _has_connectable_scanner(self) -> bool:
        """Return whether Home Assistant has a connectable scanner available."""
        async_scanner_count = getattr(bluetooth, "async_scanner_count", None)
        if not callable(async_scanner_count):
            return True

        return async_scanner_count(self.hass, connectable=True) > 0

    def _is_fresh_startup_service_info(
        self,
        service_info: BluetoothServiceInfoBleak | None,
        startup_monotonic: float,
    ) -> bool:
        """Return whether service info was seen after startup completed."""
        if service_info is None:
            return False

        seen_time = getattr(service_info, "time", None)
        if seen_time is None:
            return True

        return seen_time >= startup_monotonic

    async def _async_wait_for_shunt_startup_ready(self) -> None:
        """Delay the first sustained shunt connect until HA bluetooth is ready."""
        if self._shunt_startup_gate_complete:
            return

        if getattr(self.hass, "state", None) == CoreState.running:
            self._shunt_startup_gate_complete = True
            return

        startup_monotonic = time.monotonic()
        ready_event = asyncio.Event()

        @callback
        def _async_is_ready(
            service_info: BluetoothServiceInfoBleak | None = None,
        ) -> bool:
            if getattr(self.hass, "state", None) != CoreState.running:
                return False

            if not self._has_connectable_scanner():
                return False

            latest_service_info = service_info or bluetooth.async_last_service_info(
                self.hass,
                self.address,
            )
            return self._is_fresh_startup_service_info(
                latest_service_info,
                startup_monotonic,
            )

        @callback
        def _async_started(_event: Any) -> None:
            if _async_is_ready():
                ready_event.set()

        @callback
        def _async_bluetooth_event(
            service_info: BluetoothServiceInfoBleak,
            change: BluetoothChange,
        ) -> None:
            if change == BluetoothChange.ADVERTISEMENT and _async_is_ready(
                service_info
            ):
                ready_event.set()

        unsub_started = None
        if hasattr(self.hass, "bus") and hasattr(self.hass.bus, "async_listen_once"):
            unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED,
                _async_started,
            )

        unsub_bluetooth = bluetooth.async_register_callback(
            self.hass,
            _async_bluetooth_event,
            {"address": self.address, "connectable": True},
            BluetoothScanningMode.ACTIVE,
        )

        try:
            if not _async_is_ready():
                try:
                    await asyncio.wait_for(
                        ready_event.wait(),
                        timeout=SHUNT_STARTUP_READY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    self.logger.debug(
                        "Timed out waiting for Smart Shunt startup readiness on %s; "
                        "continuing with reconnect",
                        self.address,
                    )
        finally:
            if callable(unsub_started):
                unsub_started()
            unsub_bluetooth()
            self._shunt_startup_gate_complete = True

    async def _async_prepare_shunt_reconnect(self, existing_device: Any) -> Any | None:
        """Clear BlueZ device state before a sustained shunt reconnect."""
        try:
            cache_cleared = await clear_cache(self.address)
        except Exception as err:  # noqa: BLE001
            self.logger.debug(
                "Failed to clear Smart Shunt BlueZ state for %s before reconnect: %s",
                self.address,
                err,
            )
            return existing_device

        if not cache_cleared:
            return existing_device

        self.logger.debug(
            "Cleared Smart Shunt BlueZ state for %s before reconnect",
            self.address,
        )

        refreshed_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if refreshed_device is None:
            self.logger.debug(
                "Smart Shunt %s has not been rediscovered after clearing BlueZ state",
                self.address,
            )
            return None

        return refreshed_device

    async def _shunt_notification_loop(self) -> None:
        """Maintain a sustained notification listener for Smart Shunt devices."""
        while True:
            client: Any = None
            got_live_data = False
            disconnect_attempted = False
            try:
                await self._async_wait_for_shunt_startup_ready()
                service_info = bluetooth.async_last_service_info(
                    self.hass, self.address
                )
                if not service_info:
                    self.logger.debug(
                        "No Smart Shunt service info available for %s; retrying in %ss",
                        self.address,
                        SHUNT_RECONNECT_DELAY_SECONDS,
                    )
                    await asyncio.sleep(SHUNT_RECONNECT_DELAY_SECONDS)
                    continue

                self._update_device_from_service_info(service_info)
                connect_device = await self._async_prepare_shunt_reconnect(
                    service_info.device
                )
                if connect_device is None:
                    await asyncio.sleep(SHUNT_RECONNECT_DELAY_SECONDS)
                    continue

                if self.device is not None:
                    self.device.ble_device = connect_device

                client = await establish_connection(
                    BleakClient,
                    connect_device,
                    self.device.name if self.device is not None else self.address,
                    max_attempts=1,
                )

                def notification_handler(
                    _sender: BleakGATTCharacteristic | int | str, data: bytearray
                ) -> None:
                    nonlocal got_live_data
                    try:
                        if self._process_sustained_shunt_notification(bytes(data)):
                            got_live_data = True
                    except Exception as err:  # noqa: BLE001
                        self.logger.warning(
                            "Smart Shunt notification handling failed for %s: %s",
                            self.address,
                            err,
                            exc_info=True,
                        )

                await client.start_notify(shunt_notify_char_uuid, notification_handler)
                while getattr(client, "is_connected", True):
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                if client is not None and getattr(client, "is_connected", False):
                    self._schedule_shunt_disconnect(client)
                return
            except Exception as err:
                self.last_update_success = False
                if self.device is not None:
                    self.device.update_availability(False, err)
                self.hass.loop.call_soon_threadsafe(self.async_update_listeners)
                self.logger.debug(
                    "Smart Shunt listener error for %s: %s",
                    self.address,
                    err,
                )
                if client is not None:
                    await self._async_disconnect_shunt_client(client)
                    disconnect_attempted = True

            if (
                client is not None
                and not disconnect_attempted
                and getattr(client, "is_connected", False)
            ):
                await self._async_disconnect_shunt_client(client)

            if client is not None and not got_live_data:
                self.logger.debug(
                    "Smart Shunt listener for %s disconnected before a live payload",
                    self.address,
                )

            await asyncio.sleep(SHUNT_RECONNECT_DELAY_SECONDS)

    async def _read_device_data(
        self, service_info: BluetoothServiceInfoBleak | None
    ) -> bool:
        """Read data from a Renogy BLE device using active connection."""
        async with self._connection_lock:
            try:
                self._connection_in_progress = True
                success = False
                error: Exception | None = None
                scanner_device = self._scanner_device_for_operation()
                if scanner_device is None:
                    if not self._keep_recent_data_available("missing preferred connectable path"):
                        self.last_update_success = False
                    self.logger.debug(
                        "Skipping Renogy BLE read for %s; preferred TP-Link source "
                        "%s does not currently have a safe connectable path",
                        self.address,
                        PREFERRED_RENOGY_SOURCE,
                    )
                    return False

                device = self._update_device_from_scanner_device(scanner_device)
                self.logger.debug(
                    "Polling %s device: %s (%s)",
                    device.device_type,
                    device.name,
                    device.address,
                )

                previous_data = dict(self.data) if isinstance(self.data, dict) else {}
                original_commands = getattr(self._ble_client, "_commands", None)
                controller_commands, controller_static_due = (
                    self._controller_commands_for_poll()
                )
                try:
                    if (
                        controller_commands is not None
                        and isinstance(original_commands, dict)
                    ):
                        scoped_commands = dict(original_commands)
                        scoped_commands[DeviceType.CONTROLLER.value] = controller_commands
                        self._ble_client._commands = scoped_commands
                        self.logger.debug(
                            "Controller poll for %s using command subset: %s",
                            device.address,
                            ", ".join(controller_commands),
                        )

                    read_result = await self._ble_client.read_device(device)
                except (BleakError, asyncio.TimeoutError) as err:
                    success = False
                    error = err
                    self.logger.debug(
                        "BLE read failed for %s: %s",
                        device.address,
                        err,
                    )
                else:
                    success = read_result.success
                    error = read_result.error
                    if error is not None and not isinstance(error, Exception):
                        error = Exception(str(error))
                finally:
                    if (
                        controller_commands is not None
                        and isinstance(original_commands, dict)
                    ):
                        self._ble_client._commands = original_commands

                if success:
                    device.update_availability(True, None)
                    self._record_ble_success()
                    if controller_static_due:
                        self._last_controller_static_refresh = time.monotonic()
                else:
                    self._record_ble_failure()
                    if not self._keep_recent_data_available("BLE read failure", error):
                        device.update_availability(False, error)
                        self.last_update_success = False

                # Update coordinator data if successful
                if success and device.parsed_data:
                    merged_data = dict(previous_data)
                    merged_data.update(device.parsed_data)
                    device.parsed_data = merged_data
                    self.data = merged_data
                    self.logger.debug("Updated coordinator data: %s", self.data)
                    self._warn_if_model_mismatch()

                return success
            finally:
                self._connection_in_progress = False

    def _warn_if_model_mismatch(self) -> None:
        """Warn once when the reported model implies a different device type.

        A BT-TH module advertises the same BLE name regardless of the device
        behind it, so entries can end up configured as the default type even
        though the model register identifies e.g. a DC-DC charger.
        """
        if self._model_mismatch_warned or not self.data:
            return

        model = self.data.get("model")
        detected_type = detect_device_type_from_model(model)
        if detected_type is None or detected_type == self.device_type:
            return

        self._model_mismatch_warned = True
        self.logger.warning(
            "Device %s reports model %s, which is a '%s' device, but this "
            "entry is configured as '%s'. Reconfigure the integration entry "
            "to switch the device type and unlock the correct entities.",
            self.address,
            model,
            detected_type,
            self.device_type,
        )

    async def async_set_load_state(self, state: bool) -> bool:
        """Set the DC load on/off."""
        if self._connection_in_progress:
            self.logger.debug("Connection already in progress, skipping load write")
            return False

        service_info = self._service_info_for_operation()
        if (
            service_info is None
            and not self._can_use_cached_device_without_service_info()
        ):
            self.logger.error(
                "No service info available for device %s. Ensure device is within "
                "range and powered on.",
                self.address,
            )
            return False
        if service_info is None:
            self.logger.debug(
                "No service info available for %s; using cached device context for "
                "persistent session load write",
                self.address,
            )

        async with self._connection_lock:
            self._connection_in_progress = True
            try:
                scanner_device = self._scanner_device_for_operation()
                if scanner_device is None:
                    self.logger.debug(
                        "Skipping Renogy load write for %s; preferred TP-Link source "
                        "%s does not currently have a safe connectable path",
                        self.address,
                        PREFERRED_RENOGY_SOURCE,
                    )
                    self.last_update_success = False
                    return False

                device = self._update_device_from_scanner_device(scanner_device)
                value = 1 if state else 0
                write_single_register = getattr(
                    self._ble_client, "write_single_register", None
                )
                if write_single_register is None:
                    self.logger.error(
                        "Renogy BLE library does not support write_single_register"
                    )
                    device.update_availability(False, None)
                    self.last_update_success = False
                    return False

                write_result = await write_single_register(
                    device, LOAD_CONTROL_REGISTER, value
                )
                device.update_availability(write_result.success, write_result.error)
                self.last_update_success = write_result.success

                if write_result.success:
                    load_state = "on" if state else "off"
                    if device.parsed_data is not None:
                        device.parsed_data["load_status"] = load_state
                    if isinstance(self.data, dict):
                        self.data["load_status"] = load_state
                    else:
                        self.data = {"load_status": load_state}
                    self.async_update_listeners()

                return write_result.success
            finally:
                self._connection_in_progress = False

    async def _async_poll_device(
        self, service_info: BluetoothServiceInfoBleak | None
    ) -> dict[str, Any]:
        """Poll the device and return parsed data."""
        if self._uses_sustained_shunt_listener():
            return self.data if isinstance(self.data, dict) else {}

        # If a connection is already in progress, don't start another one
        if self._connection_in_progress:
            self.logger.debug("Connection already in progress, skipping poll")
            return self.data if isinstance(self.data, dict) else {}

        self.last_poll_time = datetime.now()
        if service_info is not None:
            self.logger.debug(
                "Polling device: %s (%s)", service_info.name, service_info.address
            )
        elif self.device is not None:
            self.logger.debug(
                "Polling device from cached context: %s (%s)",
                self.device.name,
                self.device.address,
            )

        # Read device data using service_info and Home Assistant's Bluetooth API
        success = await self._read_device_data(service_info)

        if success and self.device and self.device.parsed_data:
            # Log the parsed data for debugging
            self.logger.debug("Parsed data: %s", self.device.parsed_data)

            # Call the callback if available
            if self.device_data_callback:
                try:
                    await self.device_data_callback(self.device)
                except Exception as e:
                    self.logger.error("Error in device data callback: %s", str(e))

            # Update all listeners after successful data acquisition
            return dict(self.device.parsed_data)

        else:
            failed_address = (
                service_info.address if service_info is not None else self.address
            )
            self.logger.info("Failed to retrieve data from %s", failed_address)
            self._keep_recent_data_available("poll failure")
            return self.data if isinstance(self.data, dict) else {}

    @callback
    def _async_handle_unavailable(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going unavailable."""
        self.logger.info("Device %s is no longer available", service_info.address)
        if not self._keep_recent_data_available("Bluetooth unavailable event"):
            self.last_update_success = False
        self.async_update_listeners()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        self._record_preferred_advertisement(service_info)

        # Update RSSI if device exists
        if self.device:
            self.device.rssi = service_info.advertisement.rssi
            self.device.last_seen = datetime.now()

    async def async_write_register(self, register: int, value: int) -> bool:
        """Write a single register value to the device.

        Args:
            register: Register address to write (e.g., 0xE004 for battery type)
            value: 16-bit value to write

        Returns:
            True if write was successful, False otherwise
        """
        if not self.device:
            self.logger.error("Cannot write register: no device connected")
            return False

        # Check if write support is available in renogy-ble library
        if not HAS_WRITE_SUPPORT:
            self.logger.error(
                "Write support not available in renogy-ble library. "
                "Please update to a version with write_register support."
            )
            return False

        # Try to use the library's write method if available.
        write_register_fn = getattr(self._ble_client, "write_register", None)
        if callable(write_register_fn):
            write_register = cast(
                Callable[[RenogyBLEDevice, int, int], Awaitable[bool]],
                write_register_fn,
            )
            try:
                success = await write_register(self.device, register, value)
                if success:
                    # Trigger a refresh to update the new value
                    await self.async_request_refresh()
                return success
            except Exception as e:
                self.logger.error("Error writing register %s: %s", hex(register), e)
                return False
        else:
            self.logger.error(
                "write_register method not available in RenogyBleClient. "
                "Please update renogy-ble library."
            )
            return False
