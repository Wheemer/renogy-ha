<div align="center">

<h1>
  <img src="custom_components/renogy/brand/icon.png" width="56" alt="Renogy integration icon" align="center">
  Renogy for Home Assistant
</h1>

### Local Bluetooth monitoring, control, and guarded Rover firmware updates

[![HACS Custom](https://img.shields.io/badge/HACS-CUSTOM-FD7E14?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://github.com/hacs/integration)
[![Home Assistant 2026.3+](https://img.shields.io/badge/HOME%20ASSISTANT-2026.3%2B-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/renogy-ha?include_prereleases&style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/renogy-ha/releases)
[![License](https://img.shields.io/badge/LICENSE-APACHE%202.0-64748B?style=for-the-badge&labelColor=555555)](LICENSE)

[Install](#install) | [Configure](#configure) | [Firmware Updates](#firmware-updates) | [Supported Devices](#supported-devices) | [Troubleshooting](#troubleshooting)

</div>

Renogy for Home Assistant is a custom integration for monitoring and controlling supported Renogy devices over Bluetooth Low Energy. It works with Home Assistant Bluetooth adapters and ESPHome Bluetooth proxies, keeps normal device telemetry local, and adds an optional guarded firmware-update path for the Renogy Rover 30.

This fork is based on [IAmTheMitchell/renogy-ha](https://github.com/IAmTheMitchell/renogy-ha) and retains its broad Renogy protocol support while adding controller polling improvements, startup-safe behavior, connection recovery controls, LiFePO4 state-of-charge support, and firmware updates.

> [!WARNING]
> This integration can control electrical equipment and, on explicitly supported hardware, write controller firmware. Use correctly rated wiring and protection. Keep the controller powered during an update. No software can remove every risk from a firmware flash.

## What It Does

- Discovers supported Renogy devices through Home Assistant Bluetooth and ESPHome Bluetooth proxies.
- Monitors controller, battery, solar, load, inverter, DCC charger, Communication Hub, and Smart Shunt data where supported.
- Exposes compatible power and energy sensors to Home Assistant.
- Controls the DC load output on supported charge controllers.
- Exposes supported DCC charging settings as Home Assistant controls.
- Supports intermittent and persistent Bluetooth sessions for non-shunt devices.
- Separates frequently changing controller telemetry from slower static reads to reduce unnecessary BLE traffic.
- Defers Renogy polling until Home Assistant has finished starting.
- Preserves entity availability through short Bluetooth interruptions and retries unavailable devices.
- Provides selectable LiFePO4 state-of-charge behavior for supported battery data.
- Checks for and installs official Rover 30 firmware through a Home Assistant update entity.

## Install

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=renogy-ha&category=integration)

If the button does not work:

1. Open HACS.
2. Open the menu and choose **Custom repositories**.
3. Add `https://github.com/Wheemer/renogy-ha` as an **Integration** repository.
4. Install **Renogy**.
5. Restart Home Assistant so the new Python module is loaded.
6. Open **Settings > Devices & services > Add integration** and select **Renogy**.

For a manual install, copy `custom_components/renogy` into `/config/custom_components/renogy` and restart Home Assistant.

After any integration update, restart Home Assistant. Reloading a config entry can unload and restore the already-imported integration, but it does not replace Python modules held by the running Home Assistant process.

## Configure

Renogy devices are normally discovered automatically. Select the discovered device, confirm its device type, and choose a polling interval.

Each configured device can expose these options when applicable:

| Setting | Purpose |
|---|---|
| **Polling interval** | Controls how often live device data is requested. |
| **Failed polls before unavailable** | Allows short BLE interruptions without immediately dropping every entity. |
| **Reconnect interval while unavailable** | Controls recovery attempts after the failure threshold is reached. |
| **Non-shunt connection mode** | Selects intermittent polling or a persistent session for controllers, batteries, DCC chargers, and inverters. |
| **Smart Shunt connection mode** | Selects sustained or intermittent Smart Shunt communication. |
| **Communication Hub / multiple batteries** | Enables Communication Hub battery-bank handling. |
| **Renogy firmware account** | Optionally enables official firmware checks for supported controllers. |

See [Connection modes](docs/connection-modes.md) for transport behavior and recommendations. Inverters can also select a model-specific register profile; see [Inverter profiles](docs/inverter-profiles.md).

## Firmware Updates

This fork includes firmware discovery and OTA installation for the **Renogy Rover 30**, identified by the exact controller model `RNG-CTRL-RVR30`.

Firmware access is optional. Normal sensors, controls, polling, and Bluetooth operation do not require a Renogy account.

### Enable Firmware Checks

1. Open **Settings > Devices & services > Renogy**.
2. Open **Configure** for the Rover 30 entry.
3. Enter the Renogy account email and password used by the DC Home app.
4. Submit the form and wait for the **Renogy Rover firmware** update entity to check the official catalog.

The password is exchanged for Renogy access tokens and is not saved in the config entry. Signing out of firmware updates removes the saved firmware authorization without affecting device telemetry.

### Update Safeguards

Before an update is offered or installed, the integration:

- Requires the exact supported controller model and firmware SKU.
- Parses and compares complete firmware versions and refuses reinstalls or downgrades.
- Rechecks Renogy's catalog immediately before transfer.
- Accepts only bounded HTTPS firmware downloads.
- Verifies the catalog checksum when Renogy supplies one.
- Downloads the image twice and requires matching SHA-256 hashes when Renogy omits a checksum.
- Verifies the image's declared length and embedded controller model.
- Negotiates the BLE packet size used by Renogy's Android updater.
- Requires the expected acknowledgement for bootloader entry, every data block, and completion.
- Retries timeouts only; an explicit rejection stops the update.
- Waits for the controller to return and verifies the newly reported version.

Keep the Rover powered continuously, close the Renogy phone app, and maintain a reliable Bluetooth path for the entire update. Do not disconnect the BT module or battery while an update is in progress.

## Supported Devices

Support follows Renogy protocol families, so available entities vary by model and firmware.

- Charge controllers using BT-1 or BT-2, including Rover and Wanderer families.
- DCC and RBC DC-DC chargers using BT-1 or BT-2.
- Legacy batteries advertising as `BT-TH-*` with `BATT` or `BATTERY` in the name.
- Battery Pro devices advertising as `RNGRBP*` or `RNGC*`.
- RNGPRO-family batteries advertising as `RNGPRO*`.
- Battery Pro advertisements using manufacturer ID `0xE14C`.
- Renogy inverters advertising as `RNGRIU*`.
- Smart Shunt 300 devices advertising as `RTMShunt300*`.
- Communication Hub battery banks where supported by the connected protocol.

Renogy Adventurer controllers are expected to use a compatible family but are not confirmed. When reporting an unsupported device, include the exact model, Bluetooth name, relevant debug logs, and a comparison with the Renogy app.

## Available Data

Entities are created only when the selected device type and its protocol expose the corresponding value.

### Charge Controllers and DCC Chargers

- Battery voltage, current, temperature, percentage, and charging state
- Solar voltage, current, power, daily generation, and total generation
- Load state, voltage, current, power, and daily consumption
- Controller temperature, model, device ID, and operating status
- DC load switch on supported controllers
- Charging limits, battery type, voltage thresholds, timing, and solar cutoff controls on supported DCC chargers

### Batteries and Communication Hubs

- Voltage, current, power, temperature, and state of charge
- Remaining and rated capacity
- Cycle count, cell count, cell voltages, and protection status when reported
- Multi-battery bank information through supported Communication Hub devices

### Inverters

- Battery and AC output voltage
- AC output current and frequency
- Input frequency
- Active and apparent load power
- Temperature, model, and device ID

### Smart Shunt 300

- Voltage, current, power, state of charge, and charge state
- Derived energy totals with restoration across Home Assistant restarts

Energy Dashboard compatibility depends on the underlying measurement. Voltage or voltage-derived state of charge is not presented as measured battery energy. Use a real current/energy source such as a compatible shunt for battery charge and discharge energy.

## Bluetooth And Recovery

Home Assistant chooses the usable Bluetooth path from local adapters and ESPHome proxies. A phone connected to the Renogy module can prevent Home Assistant from connecting, so close the Renogy app while testing or operating the integration.

Persistent-session mode keeps a supported non-shunt BLE connection open between polls. Intermittent mode reconnects for each read. The best choice depends on the device firmware and Bluetooth environment; see [Connection modes](docs/connection-modes.md).

The controller polling path keeps fast-changing values current without requesting static identity and configuration blocks on every cycle. Recovery settings are exposed per config entry so a temporary failure does not need to make every entity unavailable immediately.

## Troubleshooting

### Device Is Not Discovered

1. Confirm the Renogy device or BT-1/BT-2 module is powered.
2. Close the DC Home or Renogy phone app so it releases the BLE connection.
3. Confirm Bluetooth is configured in Home Assistant and at least one local adapter or ESPHome proxy can hear the advertisement.
4. Check that the advertised name matches a supported family listed above.
5. Open **Settings > System > Logs** and review Renogy and Bluetooth messages.

### Entities Become Unavailable

1. Check whether the device is still advertising in Home Assistant's Bluetooth diagnostics.
2. Confirm another client is not holding the connection.
3. Review the polling interval, failure threshold, reconnect interval, and connection mode.
4. Download integration diagnostics before deleting or recreating the config entry.

### Firmware Update Does Not Appear

1. Confirm the detected model is exactly `RNG-CTRL-RVR30`.
2. Confirm firmware login succeeded in the integration's Configure dialog.
3. Open the firmware update entity and check its catalog status attributes.
4. Trigger **Check for updates** from Home Assistant.
5. Review Renogy logs for authentication, catalog, download, image-validation, or BLE negotiation errors.

### Debug Logging

1. Open **Settings > Devices & services > Renogy**.
2. Open the integration menu and select **Enable debug logging**.
3. Reproduce the problem.
4. Stop debug logging and download the generated log file.

Do not publish Renogy credentials, access tokens, Bluetooth addresses you consider private, or unredacted diagnostics.

## Development

Run the checks from the repository root:

```bash
python -m ruff check custom_components/renogy tests
python -m pytest -q
```

Firmware protocol behavior is covered by focused catalog, image-validation, BLE transport, acknowledgement, retry, and post-update verification tests.

## Credits

This fork builds on the work in [IAmTheMitchell/renogy-ha](https://github.com/IAmTheMitchell/renogy-ha), its contributors, and the [renogy-ble](https://github.com/IAmTheMitchell/renogy-ble) library used by the integration.

Renogy product names and trademarks belong to their respective owner. This project is not affiliated with or endorsed by Renogy.

## License

Licensed under the [Apache License 2.0](LICENSE).
