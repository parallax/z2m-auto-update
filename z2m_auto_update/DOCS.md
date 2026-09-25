# Z2M Auto Update

Zigbee2MQTT can install OTA firmware updates, but when many devices have updates pending it starts them all at once and a large network falls over. This add-on watches Zigbee2MQTT over MQTT, keeps a queue of devices with updates available, and installs them one at a time (configurable), with timeouts, retries, an optional overnight window, allow and deny lists, and a status page in the Home Assistant sidebar.

## How it works

1. On start it reads the device list from `zigbee2mqtt/bridge/devices` and listens for each device's `update` attribute, which Zigbee2MQTT publishes whenever it learns that an update is available (it checks every device daily by default).
2. Devices reporting `update.state == "available"` go into the queue: mains-powered first, then battery-powered, alphabetically within each group.
3. Whenever fewer than `concurrent_updates` updates are running, and the update window is open, the next device is started with a `bridge/request/device/ota_update/update` request.
4. Progress is tracked from the device's `update` attribute. If a device does not start downloading within `start_timeout_seconds`, or stalls for `stall_timeout_seconds`, it is marked failed and the queue moves on. If it starts late, the add-on notices and tracks it rather than starting a second update.
5. After each update finishes or fails the add-on waits `cooldown_seconds` before starting the next one, so the mesh can settle.
6. Devices with an unknown update state are checked one at a time, spaced `check_spacing_seconds` apart, shortly after startup and again every `check_interval_hours`.

State (pause flag, failure counts, skips) is kept in `/data/state.json` so a restart carries on where it left off.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `base_topic` | `zigbee2mqtt` | Zigbee2MQTT's MQTT base topic. |
| `concurrent_updates` | `1` | How many updates may run at the same time. Leave at 1 unless you know your network can take more. |
| `update_window_start` / `update_window_end` | empty | Only start updates between these local times (`HH:MM`). Wraps past midnight (`22:00` to `06:00`). Empty means any time. |
| `include_battery` | `true` | Whether battery-powered devices are queued at all. They are always queued after mains devices. |
| `allow` | `[]` | If not empty, only devices matching one of these patterns are updated. |
| `deny` | `[]` | Devices matching one of these patterns are never updated. Deny wins over allow. |
| `max_attempts` | `3` | Attempts per device before giving up until you press Retry or Reset failures. |
| `retry_delay_minutes` | `60` | Minimum wait after a failed attempt before the device is tried again. |
| `start_timeout_seconds` | `60` | Give up on a device that has not started downloading within this time and move on. |
| `stall_timeout_seconds` | `900` | Give up on a device whose progress has not changed for this long. |
| `cooldown_seconds` | `30` | Pause between updates. |
| `check_interval_hours` | `24` | How often to ask Zigbee2MQTT to re-check every device. `0` disables the periodic check (Zigbee2MQTT still does its own daily check). |
| `check_spacing_seconds` | `10` | Gap between individual update checks. |
| `ha_discovery` | `true` | Publish MQTT discovery so the add-on shows up as a device in Home Assistant. |
| `ha_discovery_prefix` | `homeassistant` | Discovery prefix, if you changed it in the MQTT integration. |
| `status_topic` | `z2m-auto-update` | Topic prefix for the add-on's own status and command messages. |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`, `mqtt_tls` | from Supervisor | Only needed if you don't use the Mosquitto add-on. |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. |

Allow and deny patterns are shell-style globs matched, case-insensitively, against the friendly name, IEEE address, model, vendor, and `vendor model`. Examples: `Bedroom *`, `0x00178801*`, `LWG004`, `Philips *`, `IKEA of Sweden *`.

## Home Assistant entities

With discovery enabled you get a **Z2M Auto Update** device with:

- `sensor.z2m_auto_update_status` with the queue and current device as attributes
- `sensor.z2m_auto_update_queue_length`, `sensor.z2m_auto_update_current_device`, `sensor.z2m_auto_update_progress`
- `switch.z2m_auto_update_paused`
- `button.z2m_auto_update_run_now` (process the queue now, ignoring the window)
- `button.z2m_auto_update_check_for_updates`

## MQTT interface

- `z2m-auto-update/state` (retained JSON): status, queue, current device, progress.
- `z2m-auto-update/availability`: `online` or `offline`.
- `z2m-auto-update/command`: publish `pause`, `resume`, `run_now`, `check_all` or `reset_failures`.
- `z2m-auto-update/paused/set`: `ON` or `OFF`.

## Web UI

Open the add-on from the sidebar. The page shows the current update with progress, the queue, every OTA-capable device with its state, and a log. Per device you can trigger a check, push it to the front of the queue, skip it, or reset its failure count.

## Running outside Home Assistant

The image runs anywhere Docker does. Configure it with environment variables prefixed `Z2M_AU_`, for example:

```bash
docker run -d --name z2m-auto-update -p 8099:8099 -v z2m-auto-update:/data \
  -e Z2M_AU_MQTT_HOST=mqtt.local -e Z2M_AU_MQTT_USERNAME=user -e Z2M_AU_MQTT_PASSWORD=secret \
  -e Z2M_AU_UPDATE_WINDOW_START=01:00 -e Z2M_AU_UPDATE_WINDOW_END=06:00 \
  -e Z2M_AU_DENY="Garage *,IKEA of Sweden *" \
  ghcr.io/parallax/z2m-auto-update:latest
```

List options are comma-separated in environment variables. Set `TZ` (for example `TZ=Europe/London`) so the update window uses your local time; Home Assistant does this for you. The web UI is then at `http://host:8099/` with no authentication, so keep it on a trusted network.

## Troubleshooting

- **Nothing is queued but Zigbee2MQTT shows updates.** The add-on only knows a device's state once Zigbee2MQTT publishes it. Press *Check for updates*, or wait for the startup check to finish (it runs one check every `check_spacing_seconds`).
- **A device keeps failing to start.** Battery devices only pick up an OTA request when they wake up, which can be longer than `start_timeout_seconds`. Raise the timeout, or leave them and update them from Zigbee2MQTT's frontend when convenient.
- **Updates run outside the window.** *Run now* and *Update now* deliberately ignore the window until the queue is empty.
