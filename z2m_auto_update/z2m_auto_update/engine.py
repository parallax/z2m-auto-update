"""The update queue state machine.

The engine has no I/O. It is fed Zigbee2MQTT messages and a clock, and it returns MQTT
messages to publish. That keeps every rule about ordering, timeouts and retries testable
without a broker.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

QUOTED = re.compile(r"'(.+?)'")


@dataclass
class Publish:
    topic: str
    payload: str
    retain: bool = False


@dataclass
class Event:
    ts: float
    level: str
    message: str
    device: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "level": self.level, "message": self.message, "device": self.device}


@dataclass
class Device:
    ieee: str
    friendly_name: str
    model: str = ""
    vendor: str = ""
    description: str = ""
    power_source: str = ""
    supports_ota: bool = False
    disabled: bool = False
    interviewed: bool = True

    # Live state reported by Zigbee2MQTT
    update_state: str | None = None  # idle | available | updating | scheduled | None (unknown)
    installed_version: int | None = None
    latest_version: int | None = None
    release_notes: str | None = None
    progress: float | None = None
    remaining: int | None = None
    availability: str | None = None  # online | offline | None (feature not enabled)

    # Bookkeeping (persisted)
    attempts: int = 0
    last_error: str | None = None
    last_result: str | None = None  # success | failed
    last_result_at: float | None = None
    last_check_at: float | None = None
    skipped: bool = False
    priority: bool = False

    @property
    def battery(self) -> bool:
        return (self.power_source or "").lower().startswith("battery")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ieee": self.ieee,
            "friendly_name": self.friendly_name,
            "model": self.model,
            "vendor": self.vendor,
            "description": self.description,
            "power_source": self.power_source,
            "battery": self.battery,
            "supports_ota": self.supports_ota,
            "disabled": self.disabled,
            "update_state": self.update_state,
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "release_notes": self.release_notes,
            "progress": self.progress,
            "remaining": self.remaining,
            "availability": self.availability,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "last_result_at": self.last_result_at,
            "last_check_at": self.last_check_at,
            "skipped": self.skipped,
            "priority": self.priority,
        }


@dataclass
class Active:
    ieee: str
    transaction: str | None
    started_at: float
    last_activity_at: float
    progress_seen: bool = False
    external: bool = False


@dataclass
class PendingCheck:
    ieee: str
    sent_at: float


PERSISTED_DEVICE_FIELDS = (
    "attempts",
    "last_error",
    "last_result",
    "last_result_at",
    "last_check_at",
    "skipped",
)


class Engine:
    def __init__(self, settings: Settings, now: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.now = now
        self.devices: dict[str, Device] = {}
        self.name_index: dict[str, str] = {}
        self.active: dict[str, Active] = {}
        self.pending_checks: dict[str, PendingCheck] = {}
        self.check_queue: deque[str] = deque()
        self.events: deque[Event] = deque(maxlen=500)
        self.paused = False
        self.run_now = False
        self.bridge_online = False
        self.devices_received = False
        self.cooldown_until = 0.0
        self.next_check_at = 0.0
        self.next_full_check_at: float | None = None
        self.startup_check_at: float | None = None
        self.last_check_all_at: float | None = None
        self.dirty = False  # persisted state changed
        self._lost_active: dict[str, str] = {}  # ieee -> transaction, for late starts
        self._done_transactions: deque[str] = deque(maxlen=200)
        self._restored: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def base(self) -> str:
        return self.settings.base_topic

    def _event(self, level: str, message: str, device: Device | None = None) -> None:
        self.events.append(Event(self.now(), level, message, device.friendly_name if device else None))
        getattr(log, level if level != "success" else "info")(
            "%s%s", f"[{device.friendly_name}] " if device else "", message
        )

    def _device_by_name(self, name: str | None) -> Device | None:
        if not name:
            return None
        ieee = self.name_index.get(name)
        if ieee:
            return self.devices.get(ieee)
        return self.devices.get(name)

    def _device_from_response(self, payload: dict[str, Any], pending: dict[str, Any]) -> Device | None:
        tx = payload.get("transaction")
        if tx and tx in pending:
            return self.devices.get(pending[tx].ieee)
        data = payload.get("data") or {}
        dev = self._device_by_name(data.get("id"))
        if dev:
            return dev
        match = QUOTED.search(payload.get("error") or "")
        return self._device_by_name(match.group(1)) if match else None

    def _matches(self, dev: Device, patterns: list[str]) -> bool:
        haystack = [dev.friendly_name, dev.ieee, dev.model, dev.vendor, f"{dev.vendor} {dev.model}"]
        return any(
            fnmatch.fnmatchcase(h.lower(), p.strip().lower())
            for p in patterns
            if p and p.strip()
            for h in haystack
        )

    def ineligible_reason(self, dev: Device) -> str | None:
        """Why a device can't be queued right now, or None if it can."""
        s = self.settings
        if not dev.supports_ota:
            return "no OTA support"
        if dev.disabled:
            return "disabled in Zigbee2MQTT"
        if s.deny and self._matches(dev, s.deny):
            return "in deny list"
        if s.allow and not self._matches(dev, s.allow):
            return "not in allow list"
        if dev.battery and not s.include_battery:
            return "battery devices excluded"
        if dev.skipped:
            return "skipped"
        if dev.availability == "offline":
            return "offline"
        if dev.attempts >= s.max_attempts:
            return f"gave up after {dev.attempts} attempts"
        if (
            dev.last_result == "failed"
            and dev.last_result_at is not None
            and self.now() - dev.last_result_at < s.retry_delay_seconds
        ):
            wait = int((s.retry_delay_seconds - (self.now() - dev.last_result_at)) / 60) + 1
            return f"retry in {wait} min"
        return None

    def queue(self) -> list[Device]:
        candidates = [
            d
            for d in self.devices.values()
            if d.update_state == "available" and d.ieee not in self.active and not self.ineligible_reason(d)
        ]
        candidates.sort(key=lambda d: (not d.priority, d.battery, d.friendly_name.lower()))
        return candidates

    def in_window(self, now: float | None = None) -> bool:
        s = self.settings
        if not s.update_window_start or not s.update_window_end:
            return True
        try:
            start = dtime.fromisoformat(s.update_window_start)
            end = dtime.fromisoformat(s.update_window_end)
        except ValueError:
            log.error("Invalid update window %r-%r, ignoring", s.update_window_start, s.update_window_end)
            return True
        t = datetime.fromtimestamp(now if now is not None else self.now()).time()
        if start <= end:
            return start <= t < end
        return t >= start or t < end

    # ----------------------------------------------------------------- inbound
    def on_bridge_state(self, payload: Any) -> None:
        state = payload.get("state") if isinstance(payload, dict) else payload
        online = str(state).lower() == "online"
        if online == self.bridge_online:
            return
        self.bridge_online = online
        self._event("info", f"Zigbee2MQTT is {'online' if online else 'offline'}")
        if not online:
            for ieee in list(self.active):
                dev = self.devices.get(ieee)
                self._finish(ieee, "failed", "Zigbee2MQTT went offline during the update", dev)

    def on_devices(self, payload: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        for raw in payload:
            if raw.get("type") == "Coordinator":
                continue
            ieee = raw.get("ieee_address")
            if not ieee:
                continue
            seen.add(ieee)
            definition = raw.get("definition") or {}
            dev = self.devices.get(ieee)
            if dev is None:
                dev = Device(ieee=ieee, friendly_name=raw.get("friendly_name", ieee))
                self.devices[ieee] = dev
                restored = self._restored.pop(ieee, None)
                if restored:
                    for key in PERSISTED_DEVICE_FIELDS:
                        if key in restored:
                            setattr(dev, key, restored[key])
            dev.friendly_name = raw.get("friendly_name", ieee)
            dev.model = definition.get("model") or raw.get("model_id") or ""
            dev.vendor = definition.get("vendor") or raw.get("manufacturer") or ""
            dev.description = definition.get("description") or ""
            dev.power_source = raw.get("power_source") or ""
            dev.supports_ota = bool(definition.get("supports_ota"))
            dev.disabled = bool(raw.get("disabled"))
            dev.interviewed = raw.get("interview_state", "SUCCESSFUL") == "SUCCESSFUL" or bool(
                raw.get("interview_completed", True)
            )
        for ieee in list(self.devices):
            if ieee not in seen:
                gone = self.devices.pop(ieee)
                self.active.pop(ieee, None)
                self._event("info", f"Device {gone.friendly_name} was removed from Zigbee2MQTT")
        self.name_index = {d.friendly_name: d.ieee for d in self.devices.values()}
        if not self.devices_received:
            self.devices_received = True
            now = self.now()
            self.startup_check_at = now + self.settings.startup_grace_seconds
            if self.settings.check_interval_seconds > 0:
                self.next_full_check_at = now + self.settings.check_interval_seconds
            ota = sum(1 for d in self.devices.values() if d.supports_ota)
            self._event("info", f"Found {len(self.devices)} devices, {ota} support OTA updates")

    def on_device_state(self, name: str, payload: dict[str, Any]) -> None:
        dev = self._device_by_name(name)
        if dev is None or not isinstance(payload, dict):
            return
        update = payload.get("update")
        if not isinstance(update, dict):
            return
        now = self.now()
        previous = dev.update_state
        state = update.get("state")
        if state is not None:
            dev.update_state = str(state)
        for key in ("installed_version", "latest_version"):
            if update.get(key) is not None:
                setattr(dev, key, update[key])
        if update.get("latest_release_notes") is not None:
            dev.release_notes = update["latest_release_notes"]

        if dev.update_state == "updating":
            dev.progress = update.get("progress")
            dev.remaining = update.get("remaining")
            active = self.active.get(dev.ieee)
            if active is None:
                tx = self._lost_active.pop(dev.ieee, None)
                self.active[dev.ieee] = Active(
                    dev.ieee, tx, now, now, progress_seen=True, external=tx is None
                )
                self._event(
                    "info",
                    "Update started late, tracking it" if tx else "Update started outside this add-on, tracking it",
                    dev,
                )
            else:
                if not active.progress_seen:
                    self._event("info", "Update in progress", dev)
                active.progress_seen = True
                active.last_activity_at = now
        else:
            dev.progress = None
            dev.remaining = None
            active = self.active.get(dev.ieee)
            if active and active.progress_seen and previous == "updating":
                # Zigbee2MQTT normally sends a response too; this is the fallback.
                if dev.installed_version is not None and dev.installed_version == dev.latest_version:
                    self._finish(dev.ieee, "success", "Update finished", dev)
                elif dev.update_state == "available":
                    self._finish(dev.ieee, "failed", "Update stopped before completion", dev)
            if previous != "available" and dev.update_state == "available":
                self._event("info", "Update available", dev)

    def on_availability(self, name: str, payload: Any) -> None:
        dev = self._device_by_name(name)
        if dev is None:
            return
        state = payload.get("state") if isinstance(payload, dict) else payload
        dev.availability = str(state).lower() if state is not None else None

    def on_check_response(self, payload: dict[str, Any]) -> None:
        dev = self._device_from_response(payload, self.pending_checks)
        tx = payload.get("transaction")
        if tx in self.pending_checks:
            del self.pending_checks[tx]
        if dev is None:
            return
        for pending_tx, pending in list(self.pending_checks.items()):
            if pending.ieee == dev.ieee:
                del self.pending_checks[pending_tx]
        dev.last_check_at = self.now()
        self.dirty = True
        data = payload.get("data") or {}
        if payload.get("status") == "ok":
            available = bool(data.get("update_available", data.get("updateAvailable")))
            previous = dev.update_state
            dev.update_state = "available" if available else "idle"
            dev.last_error = None
            if available and previous != "available":
                self._event("info", "Update available", dev)
        else:
            error = payload.get("error") or "unknown error"
            if "already in progress" in error:
                if dev.ieee not in self.active:
                    now = self.now()
                    self.active[dev.ieee] = Active(dev.ieee, None, now, now, progress_seen=True, external=True)
                return
            dev.last_error = error
            self._event("warning", f"Check failed: {error}", dev)

    def on_update_response(self, payload: dict[str, Any]) -> None:
        tx = payload.get("transaction")
        if tx and tx in self._done_transactions:
            return
        by_tx = {a.transaction: a for a in self.active.values() if a.transaction}
        dev = self._device_from_response(payload, by_tx)
        if dev is None:
            return
        if dev.ieee not in self.active:
            # Only accept late responses for updates we timed out on.
            lost_tx = self._lost_active.get(dev.ieee)
            if lost_tx is None or (tx and tx != lost_tx):
                return
        if payload.get("status") == "ok":
            data = payload.get("data") or {}
            to = (data.get("to") or {}).get("software_build_id")
            frm = (data.get("from") or {}).get("software_build_id")
            detail = f" ({frm} -> {to})" if frm or to else ""
            self._finish(dev.ieee, "success", f"Update finished{detail}", dev)
        else:
            error = payload.get("error") or "unknown error"
            if "already in progress" in error:
                active = self.active.get(dev.ieee)
                if active and active.transaction == tx:
                    # Our request collided with an update someone else started; keep tracking.
                    active.external = True
                    active.progress_seen = True
                    self._event("info", "Update already in progress elsewhere, tracking it", dev)
                return
            self._finish(dev.ieee, "failed", error, dev)

    def _finish(self, ieee: str, result: str, message: str, dev: Device | None) -> None:
        active = self.active.pop(ieee, None)
        lost_tx = self._lost_active.pop(ieee, None)
        for tx in (active.transaction if active else None, lost_tx):
            if tx:
                self._done_transactions.append(tx)
        now = self.now()
        self.cooldown_until = now + self.settings.cooldown_seconds
        if dev is None:
            return
        dev.last_result = result
        dev.last_result_at = now
        dev.progress = None
        dev.remaining = None
        dev.priority = False
        if result == "success":
            dev.attempts = 0
            dev.last_error = None
            if dev.update_state == "updating":
                dev.update_state = "idle"
            self._event("success", message, dev)
        else:
            dev.last_error = message
            if dev.update_state == "updating":
                dev.update_state = "available"
            self._event("error", f"Update failed: {message}", dev)
        self.dirty = True

    # ---------------------------------------------------------------- commands
    def pause(self) -> None:
        if not self.paused:
            self.paused = True
            self.dirty = True
            self._event("info", "Paused: no new updates will start")

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self.dirty = True
            self._event("info", "Resumed")

    def trigger_run_now(self) -> None:
        self.run_now = True
        self.cooldown_until = 0.0
        self._event("info", "Run now: processing the queue regardless of the update window")

    def check_all(self) -> int:
        """Queue a throttled update check for every OTA-capable device."""
        added = 0
        for dev in sorted(self.devices.values(), key=lambda d: (d.battery, d.friendly_name.lower())):
            if dev.supports_ota and not dev.disabled and dev.ieee not in self.active:
                if dev.ieee not in self.check_queue:
                    self.check_queue.append(dev.ieee)
                    added += 1
        self.last_check_all_at = self.now()
        self._event("info", f"Checking {added} devices for updates")
        return added

    def check_device(self, ieee: str) -> bool:
        dev = self.devices.get(ieee)
        if dev is None or not dev.supports_ota or ieee in self.active:
            return False
        if ieee in self.check_queue:
            self.check_queue.remove(ieee)
        self.check_queue.appendleft(ieee)
        return True

    def update_device(self, ieee: str) -> bool:
        """Move a device to the front of the queue and run it even outside the window."""
        dev = self.devices.get(ieee)
        if dev is None or not dev.supports_ota:
            return False
        dev.priority = True
        dev.skipped = False
        dev.attempts = 0
        dev.last_result_at = None
        self.run_now = True
        self.cooldown_until = 0.0
        self.dirty = True
        self._event("info", "Moved to the front of the queue", dev)
        return True

    def skip_device(self, ieee: str) -> bool:
        dev = self.devices.get(ieee)
        if dev is None:
            return False
        dev.skipped = True
        dev.priority = False
        self.dirty = True
        self._event("info", "Skipped", dev)
        return True

    def unskip_device(self, ieee: str) -> bool:
        dev = self.devices.get(ieee)
        if dev is None:
            return False
        dev.skipped = False
        self.dirty = True
        self._event("info", "No longer skipped", dev)
        return True

    def retry_device(self, ieee: str) -> bool:
        dev = self.devices.get(ieee)
        if dev is None:
            return False
        dev.attempts = 0
        dev.last_result_at = None
        dev.last_error = None
        self.dirty = True
        self._event("info", "Failure count reset", dev)
        return True

    def reset_failures(self) -> int:
        count = 0
        for dev in self.devices.values():
            if dev.attempts or dev.last_error:
                dev.attempts = 0
                dev.last_result_at = None
                dev.last_error = None
                count += 1
        if count:
            self.dirty = True
        self._event("info", f"Failure count reset on {count} devices")
        return count

    # -------------------------------------------------------------------- tick
    def tick(self) -> list[Publish]:
        now = self.now()
        out: list[Publish] = []
        self._expire_checks(now)
        self._enforce_timeouts(now)
        self._schedule_checks(now)
        if self.bridge_online and self.devices_received:
            out += self._start_updates(now)
            out += self._send_check(now)
        if self.run_now and not self.queue() and not self.active:
            self.run_now = False
        return out

    def _expire_checks(self, now: float) -> None:
        for tx, pending in list(self.pending_checks.items()):
            if now - pending.sent_at > 120:
                del self.pending_checks[tx]
                dev = self.devices.get(pending.ieee)
                if dev:
                    dev.last_error = "No response to update check"
                    self._event("warning", "No response to update check", dev)

    def _enforce_timeouts(self, now: float) -> None:
        s = self.settings
        for ieee, active in list(self.active.items()):
            dev = self.devices.get(ieee)
            if not active.progress_seen and now - active.started_at > s.start_timeout_seconds:
                # Zigbee2MQTT may still be waiting on the device; remember the transaction so a
                # late start is adopted rather than running alongside the next device.
                self._finish(ieee, "failed", f"Did not start within {s.start_timeout_seconds}s", dev)
                if active.transaction:
                    self._lost_active[ieee] = active.transaction
                    if active.transaction in self._done_transactions:
                        self._done_transactions.remove(active.transaction)
            elif active.progress_seen and now - active.last_activity_at > s.stall_timeout_seconds:
                self._finish(ieee, "failed", f"No progress for {s.stall_timeout_seconds}s", dev)

    def _start_updates(self, now: float) -> list[Publish]:
        out: list[Publish] = []
        if self.paused or now < self.cooldown_until:
            return out
        if not (self.in_window(now) or self.run_now):
            return out
        while len(self.active) < self.settings.concurrent_updates:
            queue = self.queue()
            if not queue:
                break
            dev = queue[0]
            tx = uuid.uuid4().hex[:12]
            dev.attempts += 1
            dev.priority = False
            self.dirty = True
            self.active[dev.ieee] = Active(dev.ieee, tx, now, now)
            self._event("info", f"Starting update (attempt {dev.attempts} of {self.settings.max_attempts})", dev)
            out.append(
                Publish(
                    f"{self.base}/bridge/request/device/ota_update/update",
                    json.dumps({"id": dev.ieee, "transaction": tx}),
                )
            )
        return out

    def _send_check(self, now: float) -> list[Publish]:
        if self.pending_checks or now < self.next_check_at or not self.check_queue:
            return []
        while self.check_queue:
            ieee = self.check_queue.popleft()
            dev = self.devices.get(ieee)
            if dev and dev.supports_ota and ieee not in self.active and dev.update_state != "updating":
                break
        else:
            return []
        tx = uuid.uuid4().hex[:12]
        self.pending_checks[tx] = PendingCheck(ieee, now)
        self.next_check_at = now + self.settings.check_spacing_seconds
        return [
            Publish(
                f"{self.base}/bridge/request/device/ota_update/check",
                json.dumps({"id": ieee, "transaction": tx}),
            )
        ]

    def _schedule_checks(self, now: float) -> None:
        if self.startup_check_at is not None and now >= self.startup_check_at:
            self.startup_check_at = None
            unknown = [
                d.ieee
                for d in sorted(self.devices.values(), key=lambda d: (d.battery, d.friendly_name.lower()))
                if d.supports_ota and not d.disabled and d.update_state is None
            ]
            for ieee in unknown:
                if ieee not in self.check_queue:
                    self.check_queue.append(ieee)
            if unknown:
                self._event("info", f"Checking {len(unknown)} devices with unknown update state")
        if self.next_full_check_at is not None and now >= self.next_full_check_at:
            self.next_full_check_at = now + self.settings.check_interval_seconds
            self.check_all()

    # ------------------------------------------------------------------- views
    def status(self) -> str:
        if not self.bridge_online:
            return "offline"
        if self.active:
            return "updating"
        if self.paused:
            return "paused"
        queue = self.queue()
        if queue and not (self.in_window() or self.run_now):
            return "waiting_for_window"
        if queue:
            return "queued"
        return "idle"

    def snapshot(self) -> dict[str, Any]:
        now = self.now()
        queue = self.queue()
        active = []
        for a in self.active.values():
            dev = self.devices.get(a.ieee)
            if dev:
                active.append(
                    {
                        **dev.as_dict(),
                        "started_at": a.started_at,
                        "elapsed": int(now - a.started_at),
                        "external": a.external,
                        "waiting_to_start": not a.progress_seen,
                    }
                )
        devices = []
        for dev in sorted(self.devices.values(), key=lambda d: d.friendly_name.lower()):
            if not dev.supports_ota:
                continue
            d = dev.as_dict()
            d["ineligible_reason"] = self.ineligible_reason(dev)
            d["queued"] = dev in queue
            d["active"] = dev.ieee in self.active
            d["check_pending"] = dev.ieee in self.check_queue or any(
                p.ieee == dev.ieee for p in self.pending_checks.values()
            )
            devices.append(d)
        return {
            "status": self.status(),
            "paused": self.paused,
            "run_now": self.run_now,
            "bridge_online": self.bridge_online,
            "in_window": self.in_window(now),
            "window": {"start": self.settings.update_window_start, "end": self.settings.update_window_end},
            "cooldown_remaining": max(0, int(self.cooldown_until - now)),
            "checks_pending": len(self.check_queue) + len(self.pending_checks),
            "checks_in_flight": [
                {"ieee": p.ieee, "sent_at": p.sent_at, "age": int(now - p.sent_at)}
                for p in self.pending_checks.values()
            ],
            "next_check_in": max(0, int(self.next_check_at - now)),
            "next_full_check_at": self.next_full_check_at,
            "queue": [d.ieee for d in queue],
            "active": active,
            "devices": devices,
            "events": [e.as_dict() for e in reversed(self.events)],
            "now": now,
        }

    # ------------------------------------------------------------- persistence
    def persisted_state(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "devices": {
                ieee: {k: getattr(d, k) for k in PERSISTED_DEVICE_FIELDS} for ieee, d in self.devices.items()
            },
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        self.paused = bool(data.get("paused", False))
        self._restored = dict(data.get("devices") or {})
