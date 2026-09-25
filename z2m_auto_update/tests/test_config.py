from __future__ import annotations

import json

from z2m_auto_update.config import load_settings


def test_options_file_then_env(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"mqtt_host": "broker", "deny": ["Attic *"], "concurrent_updates": 2, "bogus": 1}))
    monkeypatch.setenv("Z2M_AU_MQTT_PORT", "1884")
    monkeypatch.setenv("Z2M_AU_ALLOW", "Philips *, IKEA *")
    monkeypatch.setenv("Z2M_AU_INCLUDE_BATTERY", "false")
    monkeypatch.setenv("Z2M_AU_STATE_FILE", str(tmp_path / "s.json"))
    s = load_settings(str(opts))
    assert s.mqtt_host == "broker"
    assert s.mqtt_port == 1884
    assert s.deny == ["Attic *"]
    assert s.allow == ["Philips *", "IKEA *"]
    assert s.include_battery is False
    assert s.concurrent_updates == 2


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("Z2M_AU_STATE_FILE", str(tmp_path / "s.json"))
    s = load_settings(str(tmp_path / "missing.json"))
    assert s.base_topic == "zigbee2mqtt"
    assert s.concurrent_updates == 1
    assert s.state_file.endswith("s.json")
