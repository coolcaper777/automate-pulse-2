# Automate Pulse 2

An [Indigo Domotics](https://www.indigodomo.com/) plugin that integrates **Rollease Acmeda Pulse 2** motorized shade hubs, exposing each hub and each shade as native Indigo devices.

- **Plugin ID:** `com.coolcaper.automatepulse2`
- **Requires:** Indigo Server API 3.1+
- **Python dependency:** [`aiopulse2`](https://pypi.org/project/aiopulse2/) 0.10.0 (bundled)

## Installation

1. Download the latest release's `Automate Pulse 2.indigoPlugin.zip` from the [Releases page](https://github.com/coolcaper777/automate-pulse-2/releases) and unzip it — you'll get a folder named `Automate Pulse 2.indigoPlugin`.
   - *(Cloning the repo instead? The checked-out folder is named `automate-pulse-2`, not `Automate Pulse 2.indigoPlugin` — rename it before installing, or Indigo won't recognize it as a plugin.)*
2. Double-click `Automate Pulse 2.indigoPlugin` (or drag it onto the Indigo Server icon) to install, or copy it into `~/Library/Application Support/Perceptive Automation/Indigo <version>/Plugins/`.
3. Restart the plugin from Indigo's Plugins menu.
4. On first launch, Indigo installs the bundled Python dependency (`aiopulse2`, from `requirements.txt`) automatically — no manual `pip install` needed.
5. Create a **Pulse 2 Hub** device with your hub's IP address; shades are then auto-discovered.

## What it does

- Connects to one or more Pulse 2 Hubs over their local websocket API.
- Auto-discovers every shade paired to a hub and creates a corresponding Indigo device.
- Keeps shade position, battery voltage/percent, signal strength, and movement state in sync in real time via the hub's push callbacks.
- Periodically refreshes battery info (every 5 minutes) and hub connectivity (every 30 seconds), auto-reconnecting a hub after ~90 seconds of no response.

## Devices

### Pulse 2 Hub (`pulseHub`)

Represents one physical hub.

| Config field | Description |
|---|---|
| Hub IP Address | Local IP address of the Pulse 2 Hub |

| State | Description |
|---|---|
| `hubStatus` | `Connecting...` / `Connected` / `Disconnected` |
| `hubModel` | Hub model, once reported |
| `hubFirmware` | Hub firmware version |
| `hubMAC` | Hub MAC address |

Changing the Hub IP Address on an existing device automatically disconnects and reconnects using the new address.

### Shade (`shadeDevice`)

Represents one motorized shade, exposed as a **dimmer** device (brightness = "how open" the shade is, 0–100%).

| Config field | Description |
|---|---|
| Shade ID | Unique ID from the Pulse 2 Hub (usually auto-filled by discovery) |
| Default Brightness | Optional 1–100% position used when a plain "On" command is sent. If unset, "On" returns the shade to fully open. |
| Log events and changes to Indigo log | Per-device toggle for info-level movement logging (on by default) |

| State | Description |
|---|---|
| `brightnessLevel` / `onOffState` | Standard dimmer states — position as "openness" |
| `BlindPosition` | Position, 0–100% (0 = fully closed) |
| `BatteryVolts` | Battery voltage, e.g. `11.6 V` |
| `batteryLevel` / `BatteryPercent` | Battery percentage |
| `BlindSignalStrength` | RF signal strength (dBm) |
| `shadeMoving` | `true` while the shade is in motion |
| `shadeAction` | `Opening` / `Closing` / `Stopped` |

## Actions

| Action | Applies to | Description |
|---|---|---|
| Reconnect Hub | Pulse 2 Hub | Force a disconnect/reconnect (e.g. after network trouble) |
| Stop Shade | Shade | Stop a shade mid-travel |

Standard dimmer actions (On/Off/Set Brightness/Brighten/Dim/Request Status) are also supported on Shade devices — On respects the "Default Brightness" setting if configured.

## Debug logging

Plugin → Configure → **Enable Debug Logging** turns on verbose logging of hub connections, shade discovery, callback processing, and battery/heartbeat polling. It can be toggled live without restarting the plugin. Leave it off for normal use; turn it on when troubleshooting a hub or a specific shade.

## Troubleshooting

- **Shade never appears:** confirm the hub shows `hubStatus = Connected`; if it doesn't, check the Hub IP Address and that the hub is reachable on your local network. Enable debug logging and look for `"Checking for rollers..."` / `"No rollers found..."` messages.
- **Hub shows Disconnected / Unresponsive:** the heartbeat auto-reconnects after ~90 seconds of no response; use the **Reconnect Hub** action to force it sooner.
- **Battery/signal missing:** these come from the hub's own periodic shadow query; enable debug logging to see `"No battery info for ..."` / `"No signal info for ..."` diagnostics.

## Credits

- **Plugin:** originally authored by [coolcaper777](https://github.com/coolcaper777) (bundle ID `com.coolcaper.automatepulse2`).
- **Hub protocol library:** [`aiopulse2`](https://pypi.org/project/aiopulse2/), the async Python client this plugin uses to talk to the Pulse 2 Hub.
- **Hardware:** [Rollease Acmeda](https://www.rolleaseacmeda.com/) Pulse 2 Hub and motorized shades.
- **Debug logging toggle, verbose instrumentation, and this documentation** were added with [Claude Code](https://claude.com/claude-code) (Anthropic).
