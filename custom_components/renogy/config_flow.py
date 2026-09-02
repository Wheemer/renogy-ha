"""Config flow for Renogy BLE integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_SCAN_INTERVAL

from .const import (
    CONF_BAUDRATE,
    CONF_DEVICE_TYPE,
    CONF_NON_SHUNT_CONNECTION_MODE,
    CONF_SERIAL_PORT,
    CONF_SHUNT_CONNECTION_MODE,
    CONF_SLAVE_ADDRESS,
    CONF_TRANSPORT,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_SERIAL_BAUDRATE,
    DEFAULT_NON_SHUNT_CONNECTION_MODE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SHUNT_CONNECTION_MODE,
    DEFAULT_SLAVE_ADDRESS,
    DEFAULT_TRANSPORT,
    DEVICE_TYPES,
    DOMAIN,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    NON_SHUNT_CONNECTION_MODES,
    SHUNT_CONNECTION_MODES,
    SUPPORTED_DEVICE_TYPES,
    DeviceType,
    TransportType,
    TRANSPORT_TYPES,
)
from .device_name import (
    detect_device_type_from_ble_name,
    has_real_device_name,
    is_supported_renogy_ble_name,
)

UNKNOWN_DEVICE_NAME = "Unknown Renogy Device"

# Common schema fields for device configuration
DEVICE_TYPE_SCHEMA = {
    vol.Required(CONF_DEVICE_TYPE, default=DEFAULT_DEVICE_TYPE): vol.In(DEVICE_TYPES),
}

SCAN_INTERVAL_SCHEMA = {
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
        vol.Coerce(int),
        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
    ),
}

# Base configuration schema without device selection
CONFIG_SCHEMA = vol.Schema({**DEVICE_TYPE_SCHEMA, **SCAN_INTERVAL_SCHEMA})


def _display_name_for_discovery(discovery_info: BluetoothServiceInfoBleak) -> str:
    """Return a stable display name for a discovered BLE device."""
    if has_real_device_name(discovery_info.name):
        return discovery_info.name

    return UNKNOWN_DEVICE_NAME


def _detect_device_type_for_discovery(discovery_info: BluetoothServiceInfoBleak) -> str:
    """Detect the device type for a bluetooth discovery record."""
    manufacturer_data = getattr(discovery_info.advertisement, "manufacturer_data", {})
    return detect_device_type_from_ble_name(
        discovery_info.name,
        DEFAULT_DEVICE_TYPE,
        manufacturer_data=manufacturer_data,
    )


def _build_shunt_options_schema(default_mode: str) -> vol.Schema:
    """Build the Smart Shunt options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SHUNT_CONNECTION_MODE,
                default=default_mode,
            ): vol.In(SHUNT_CONNECTION_MODES)
        }
    )


def _build_non_shunt_options_schema(default_mode: str) -> vol.Schema:
    """Build the non-shunt connection mode schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_NON_SHUNT_CONNECTION_MODE,
                default=default_mode,
            ): vol.In(NON_SHUNT_CONNECTION_MODES)
        }
    )


class RenogyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Renogy BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._discovered_device: BluetoothServiceInfoBleak | None = None
        self._default_device_type: str = DEFAULT_DEVICE_TYPE

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> RenogyOptionsFlowHandler:
        """Return the options flow for this handler."""
        return RenogyOptionsFlowHandler(config_entry)

    def _is_renogy_device(self, discovery_info: BluetoothServiceInfoBleak) -> bool:
        """Check if a BLE device advertises a supported Renogy name."""
        manufacturer_data = getattr(
            discovery_info.advertisement, "manufacturer_data", {}
        )
        return is_supported_renogy_ble_name(
            discovery_info.name,
            manufacturer_data=manufacturer_data,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        # Check if this is a Renogy device based on the name
        if not self._is_renogy_device(discovery_info):
            return self.async_abort(reason="not_supported_device")

        LOGGER.debug(
            "Bluetooth auto-discovery for Renogy device: %s (%s)",
            discovery_info.name,
            discovery_info.address,
        )
        discovery_name = _display_name_for_discovery(discovery_info)

        # Set unique ID and check if already configured
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        # Store the discovered device for later
        self._discovered_device = discovery_info
        self._default_device_type = _detect_device_type_for_discovery(discovery_info)

        # Set title to user-readable name
        self.context["title_placeholders"] = {
            "name": discovery_name,
            "address": discovery_info.address,
        }

        # Proceed to configuration options
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device or configure options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if (
                not self._discovered_device
                and user_input.get(CONF_TRANSPORT) == TransportType.SERIAL.value
            ):
                return await self.async_step_serial()

            if not self._discovered_device and CONF_TRANSPORT in user_input:
                await self._async_discover_devices()
                if not self._discovered_devices:
                    return self.async_abort(reason="no_devices_found")
                return await self.async_step_bluetooth_manual()

            # Check if the selected device type is supported
            if (
                CONF_DEVICE_TYPE in user_input
                and user_input[CONF_DEVICE_TYPE] not in SUPPORTED_DEVICE_TYPES
            ):
                device_type = user_input[CONF_DEVICE_TYPE]
                LOGGER.warning("Unsupported device type selected: %s", device_type)

                # Generate a user-friendly error message with the device type
                return self.async_abort(
                    reason="unsupported_device_type",
                    description_placeholders={"device_type": device_type},
                )

            if self._discovered_device:
                # Coming from bluetooth discovery with device already selected
                user_input[CONF_ADDRESS] = self._discovered_device.address

                # Create a config entry
                return self.async_create_entry(
                    title=_display_name_for_discovery(self._discovered_device),
                    data=user_input,
                )
            elif CONF_ADDRESS in user_input:
                # Manual device selection
                address = user_input[CONF_ADDRESS]
                discovery_info = self._discovered_devices[address]
                detected_type = _detect_device_type_for_discovery(discovery_info)

                # Preserve an explicit user override, but fix the unchanged default
                # when discovery data identifies a non-controller device.
                if user_input.get(CONF_DEVICE_TYPE) == DEFAULT_DEVICE_TYPE:
                    user_input[CONF_DEVICE_TYPE] = detected_type

                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=_display_name_for_discovery(discovery_info),
                    data=user_input,
                )

        # If we have a discovered device from bluetooth auto-discovery,
        # just show config options (scan interval, etc)
        if self._discovered_device:
            discovered_schema = vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_TYPE, default=self._default_device_type
                    ): vol.In(DEVICE_TYPES),
                    **SCAN_INTERVAL_SCHEMA,
                }
            )
            return self.async_show_form(
                step_id="user",
                data_schema=discovered_schema,
                description_placeholders={
                    "device_name": _display_name_for_discovery(self._discovered_device),
                    "default_interval": str(DEFAULT_SCAN_INTERVAL),
                },
                errors=errors,
            )

        if not self._discovered_device:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_TRANSPORT,
                            default=DEFAULT_TRANSPORT,
                        ): vol.In(TRANSPORT_TYPES),
                    }
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_TYPE, default=self._default_device_type
                    ): vol.In(DEVICE_TYPES),
                    **SCAN_INTERVAL_SCHEMA,
                }
            ),
            description_placeholders={
                "device_name": _display_name_for_discovery(self._discovered_device),
                "default_interval": str(DEFAULT_SCAN_INTERVAL),
            },
            errors=errors,
        )

    async def async_step_bluetooth_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual BLE setup after transport selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return await self.async_step_user(user_input)

        await self._async_discover_devices()

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        # Show form to select a discovered device
        address_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {
                        address: (f"{_display_name_for_discovery(info)} ({address})")
                        for address, info in self._discovered_devices.items()
                    }
                ),
                **DEVICE_TYPE_SCHEMA,
                **SCAN_INTERVAL_SCHEMA,
            }
        )

        return self.async_show_form(
            step_id="bluetooth_manual",
            data_schema=address_schema,
            description_placeholders={
                "device_name": "Select below",
                "default_interval": str(DEFAULT_SCAN_INTERVAL),
            },
            errors=errors,
        )

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle wired serial Modbus setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            serial_port = user_input[CONF_SERIAL_PORT]
            slave_address = user_input[CONF_SLAVE_ADDRESS]
            unique_id = f"serial://{serial_port}/{slave_address}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            user_input[CONF_TRANSPORT] = TransportType.SERIAL.value
            user_input[CONF_DEVICE_TYPE] = DeviceType.CONTROLLER.value

            return self.async_create_entry(
                title=f"Renogy {serial_port} #{slave_address}",
                data=user_input,
            )

        return self.async_show_form(
            step_id="serial",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT): str,
                    vol.Required(
                        CONF_SLAVE_ADDRESS,
                        default=DEFAULT_SLAVE_ADDRESS,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=247)),
                    vol.Required(
                        CONF_BAUDRATE,
                        default=DEFAULT_SERIAL_BAUDRATE,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1200, max=115200)),
                    **SCAN_INTERVAL_SCHEMA,
                }
            ),
            errors=errors,
        )

    async def _async_discover_devices(self) -> None:
        """Discover Bluetooth devices."""
        LOGGER.debug("Scanning for Renogy BLE devices")

        self._discovered_devices = {}

        for discovery_info in bluetooth.async_discovered_service_info(self.hass):
            # Skip devices that don't match our pattern
            if not self._is_renogy_device(discovery_info):
                continue

            # Skip devices that are already configured
            address = discovery_info.address
            if address in self._async_current_ids():
                continue

            # Add to list of discovered devices
            self._discovered_devices[address] = discovery_info
            LOGGER.debug("Found Renogy device: %s (%s)", discovery_info.name, address)

        LOGGER.debug(
            "Found %s unconfigured Renogy devices", len(self._discovered_devices)
        )


class RenogyOptionsFlowHandler(OptionsFlow):
    """Handle Renogy BLE options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage integration options."""
        device_type = self._config_entry.data.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)

        if device_type == DeviceType.SHUNT300.value:
            if user_input is not None:
                return self.async_create_entry(title="", data=user_input)

            current_mode = self._config_entry.options.get(
                CONF_SHUNT_CONNECTION_MODE,
                DEFAULT_SHUNT_CONNECTION_MODE,
            )
            return self.async_show_form(
                step_id="init",
                data_schema=_build_shunt_options_schema(current_mode),
            )

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_mode = self._config_entry.options.get(
            CONF_NON_SHUNT_CONNECTION_MODE,
            DEFAULT_NON_SHUNT_CONNECTION_MODE,
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_build_non_shunt_options_schema(current_mode),
        )
