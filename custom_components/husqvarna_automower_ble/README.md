# Husqvarna Automower BLE (dev build with full command set)

Drop-in replacement for the built-in `husqvarna_automower_ble` integration.
It uses your existing setup (same domain, same config entry, same Bluetooth
proxies) and adds three developer actions:

* `husqvarna_automower_ble.read_all`: read-only sweep of every getter for your model
* `husqvarna_automower_ble.list_commands`: every command, with parameter types, ranges and options
* `husqvarna_automower_ble.send_command`: send one command by name (writes need `allow_write: true`)

Install: copy this folder to `/config/custom_components/husqvarna_automower_ble`
and restart Home Assistant. Remove the folder and restart to go back to the
built-in integration.
