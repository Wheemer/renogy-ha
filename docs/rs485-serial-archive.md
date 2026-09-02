# RS485 Serial Archive

This branch preserves the earlier wired Renogy controller implementation recovered
from the original Codex session. It is intentionally separate from the maintained
Bluetooth and controller-firmware branch.

## Scope

The archived implementation adds a `bluetooth` or `serial` transport choice to the
config flow. Serial entries accept a device path, Modbus slave address, baud rate,
and scan interval. `RenogySerialCoordinator` uses Modbus RTU through
`minimalmodbus==2.1.1` and maps common Rover controller registers into the existing
Renogy entity model.

The serial entry unique ID is:

`serial://<serial_port>/<slave_address>`

Defaults are slave address `1`, baud rate `9600`, and Modbus RTU framing of 8 data
bits, no parity, and 1 stop bit.

## Preserved Register Coverage

The implementation reads battery percentage and voltage, battery and controller
temperature, load voltage/current/power/status, PV voltage/current/power, daily
generation and consumption values, total generation, charging state, battery type,
and the controller model string.

## Validation Boundary

This is an archival branch, not a production-ready release. The recovered code was
not validated against physical RS485 hardware. Before deployment it still needs:

- tests for serial config flow and coordinator parsing;
- verification of the exact controller and adapter pinout;
- verification of total-generation register decoding;
- correction of the remaining `BLE Address` metadata label in the serial switch path;
- live checks of entity units, state classes, and Energy Dashboard statistics.

Do not merge this branch into the Bluetooth firmware branch wholesale. Port specific
serial commits only after the hardware path has been tested.
