"""Configuration loading.

Precedence (lowest to highest): built-in defaults, ``/data/options.json`` (written by the
Home Assistant Supervisor from the add-on options), then ``Z2M_AU_*`` environment variables
for standalone use.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_PREFIX = "Z2M_AU_"


@dataclass
class Settings:
    # MQTT connection. Leave host empty inside Home Assistant to use the Supervisor's broker.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False

    # Zigbee2MQTT
    base_topic: str = "zigbee2mqtt"

    # Queue behaviour
    concurrent_updates: int = 1
    update_window_start: str = ""  # "HH:MM", empty = no window
    update_window_end: str = ""
    include_battery: bool = True
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    max_attempts: int = 3
    retry_delay_minutes: int = 60
    start_timeout_seconds: int = 60
    stall_timeout_seconds: int = 900
    cooldown_seconds: int = 30

    # Update checks
    check_interval_hours: int = 24  # 0 disables periodic re-checks
    check_spacing_seconds: int = 10
    startup_grace_seconds: int = 30

    # Integration
    ha_discovery: bool = True
    ha_discovery_prefix: str = "homeassistant"
    status_topic: str = "z2m-auto-update"
    web_port: int = 8099
    log_level: str = "info"

    # Paths
    state_file: str = ""

    @property
    def retry_delay_seconds(self) -> int:
        return self.retry_delay_minutes * 60

    @property
    def check_interval_seconds(self) -> int:
        return self.check_interval_hours * 3600


def _coerce(value: Any, target_type: type) -> Any:
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is list:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(value)
    if target_type is str:
        return "" if value is None else str(value)
    return value


def _field_type(f) -> type:
    t = f.type
    if isinstance(t, str):
        return {"str": str, "int": int, "bool": bool, "list[str]": list}[t]
    origin = getattr(t, "__origin__", None)
    return origin or t


def load_settings(options_path: str | None = None) -> Settings:
    """Build settings from defaults, the Supervisor options file and environment variables."""
    settings = Settings()
    known = {f.name: _field_type(f) for f in fields(Settings)}

    path = Path(options_path or os.environ.get(f"{ENV_PREFIX}OPTIONS", "/data/options.json"))
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.error("Could not parse %s: %s", path, exc)
            data = {}
        for key, value in data.items():
            if key in known and value is not None:
                setattr(settings, key, _coerce(value, known[key]))
            elif key not in known:
                log.warning("Ignoring unknown option %r", key)

    for name, typ in known.items():
        env_key = f"{ENV_PREFIX}{name.upper()}"
        if env_key in os.environ:
            setattr(settings, name, _coerce(os.environ[env_key], typ))

    if not settings.state_file:
        settings.state_file = "/data/state.json" if Path("/data").is_dir() else "state.json"

    settings.concurrent_updates = max(1, settings.concurrent_updates)
    settings.max_attempts = max(1, settings.max_attempts)
    return settings
