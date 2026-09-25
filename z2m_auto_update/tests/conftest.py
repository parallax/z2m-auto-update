from __future__ import annotations

import json

import pytest

from z2m_auto_update.config import Settings
from z2m_auto_update.engine import Engine


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def device(ieee: str, name: str, *, ota: bool = True, battery: bool = False, vendor="Philips", model="LWG004"):
    return {
        "ieee_address": ieee,
        "friendly_name": name,
        "type": "EndDevice" if battery else "Router",
        "power_source": "Battery" if battery else "Mains (single phase)",
        "supported": True,
        "disabled": False,
        "interview_state": "SUCCESSFUL",
        "definition": {"model": model, "vendor": vendor, "description": "Bulb", "supports_ota": ota},
    }


DEVICES = [
    {"ieee_address": "0x0000", "friendly_name": "Coordinator", "type": "Coordinator", "definition": None},
    device("0x0001", "Kitchen Sink"),
    device("0x0002", "Attic 05"),
    device("0x0003", "Hall Motion", battery=True, model="9290012607"),
    device("0x0004", "Dumb Plug", ota=False, vendor="Acme", model="PLUG1"),
]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings():
    return Settings(cooldown_seconds=0, startup_grace_seconds=30, check_spacing_seconds=10, retry_delay_minutes=0)


@pytest.fixture
def engine(settings, clock):
    e = Engine(settings, now=clock)
    e.on_bridge_state({"state": "online"})
    e.on_devices(DEVICES)
    return e


def available(engine: Engine, name: str, installed=1, latest=2):
    engine.on_device_state(
        name, {"update": {"state": "available", "installed_version": installed, "latest_version": latest}}
    )


def updating(engine: Engine, name: str, progress: float, remaining: int = 100):
    engine.on_device_state(
        name,
        {
            "update": {
                "state": "updating",
                "progress": progress,
                "remaining": remaining,
                "installed_version": 1,
                "latest_version": 2,
            }
        },
    )


def update_requests(publishes):
    return [json.loads(p.payload) for p in publishes if p.topic.endswith("ota_update/update")]


def check_requests(publishes):
    return [json.loads(p.payload) for p in publishes if p.topic.endswith("ota_update/check")]
