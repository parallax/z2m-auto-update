"""Home Assistant MQTT discovery payloads for the add-on's own entities."""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .engine import Publish

DEVICE = {
    "identifiers": ["z2m_auto_update"],
    "name": "Z2M Auto Update",
    "manufacturer": "Parallax",
    "model": "Zigbee2MQTT OTA queue",
    "sw_version": __version__,
}


def discovery_messages(prefix: str, status_topic: str) -> list[Publish]:
    state = f"{status_topic}/state"
    availability = f"{status_topic}/availability"
    common: dict[str, Any] = {
        "device": DEVICE,
        "availability_topic": availability,
        "state_topic": state,
    }

    def entity(component: str, object_id: str, extra: dict[str, Any]) -> Publish:
        payload = {**common, "unique_id": f"z2m_auto_update_{object_id}", **extra}
        return Publish(f"{prefix}/{component}/z2m_auto_update/{object_id}/config", json.dumps(payload), retain=True)

    return [
        entity(
            "sensor",
            "status",
            {
                "name": "Status",
                "icon": "mdi:update",
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": state,
            },
        ),
        entity(
            "sensor",
            "queue_length",
            {
                "name": "Queue length",
                "icon": "mdi:tray-full",
                "state_class": "measurement",
                "value_template": "{{ value_json.queue_length }}",
            },
        ),
        entity(
            "sensor",
            "current_device",
            {
                "name": "Current device",
                "icon": "mdi:zigbee",
                "value_template": "{{ value_json.current_device or 'none' }}",
            },
        ),
        entity(
            "sensor",
            "progress",
            {
                "name": "Progress",
                "icon": "mdi:progress-download",
                "unit_of_measurement": "%",
                "value_template": "{{ value_json.progress if value_json.progress is not none else 0 }}",
            },
        ),
        entity(
            "switch",
            "paused",
            {
                "name": "Paused",
                "icon": "mdi:pause-circle",
                "command_topic": f"{status_topic}/paused/set",
                "value_template": "{{ 'ON' if value_json.paused else 'OFF' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
        ),
        entity(
            "button",
            "run_now",
            {
                "name": "Run now",
                "icon": "mdi:play",
                "command_topic": f"{status_topic}/command",
                "payload_press": "run_now",
            },
        ),
        entity(
            "button",
            "check_all",
            {
                "name": "Check for updates",
                "icon": "mdi:magnify",
                "command_topic": f"{status_topic}/command",
                "payload_press": "check_all",
            },
        ),
    ]


def status_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    active = snapshot["active"]
    current = active[0] if active else None
    return {
        "status": snapshot["status"],
        "paused": snapshot["paused"],
        "bridge_online": snapshot["bridge_online"],
        "in_window": snapshot["in_window"],
        "queue_length": len(snapshot["queue"]),
        "queue": [d["friendly_name"] for d in snapshot["devices"] if d["ieee"] in snapshot["queue"]],
        "current_device": current["friendly_name"] if current else None,
        "progress": round(current["progress"], 1) if current and current.get("progress") is not None else None,
        "remaining": current.get("remaining") if current else None,
        "updates_available": sum(1 for d in snapshot["devices"] if d["update_state"] == "available"),
        "checks_pending": snapshot["checks_pending"],
    }
