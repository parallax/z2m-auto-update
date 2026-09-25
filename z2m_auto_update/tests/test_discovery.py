from __future__ import annotations

import json

from z2m_auto_update.discovery import discovery_messages, status_payload


def test_discovery_messages_are_retained_and_unique():
    msgs = discovery_messages("homeassistant", "z2m-auto-update")
    assert all(m.retain for m in msgs)
    ids = [json.loads(m.payload)["unique_id"] for m in msgs]
    assert len(ids) == len(set(ids))
    assert all(m.topic.startswith("homeassistant/") and m.topic.endswith("/config") for m in msgs)


def test_status_payload():
    snap = {
        "status": "updating",
        "paused": False,
        "bridge_online": True,
        "in_window": True,
        "queue": ["b"],
        "checks_pending": 0,
        "active": [{"friendly_name": "A", "progress": 12.345, "remaining": 100}],
        "devices": [
            {"ieee": "a", "friendly_name": "A", "update_state": "updating"},
            {"ieee": "b", "friendly_name": "B", "update_state": "available"},
        ],
    }
    p = status_payload(snap)
    assert p["current_device"] == "A"
    assert p["progress"] == 12.3
    assert p["queue"] == ["B"]
    assert p["queue_length"] == 1
