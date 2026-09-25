"""Glue between the engine, the MQTT broker and the web server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import uuid
from typing import Any

import aiohttp
import aiomqtt

from . import persist
from .config import Settings
from .discovery import discovery_messages, status_payload
from .engine import Engine, Publish

log = logging.getLogger(__name__)


async def supervisor_mqtt(settings: Settings) -> bool:
    """Fill in MQTT settings from the Home Assistant Supervisor's MQTT service, if available."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not query the Supervisor for MQTT details: %s", exc)
        return False
    data = body.get("data") or {}
    if not data.get("host"):
        log.warning("Supervisor has no MQTT service configured")
        return False
    settings.mqtt_host = data["host"]
    settings.mqtt_port = int(data.get("port", 1883))
    settings.mqtt_username = data.get("username") or ""
    settings.mqtt_password = data.get("password") or ""
    settings.mqtt_tls = bool(data.get("ssl"))
    log.info("Using the Home Assistant MQTT broker at %s:%s", settings.mqtt_host, settings.mqtt_port)
    return True


class Runner:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.client: aiomqtt.Client | None = None
        self.wake = asyncio.Event()
        self.connected = False
        self._last_status: str | None = None
        self._stopping = False

    # ------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        backoff = 2
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._session()
                backoff = 2
            except aiomqtt.MqttError as exc:
                self.connected = False
                self.client = None
                if self._stopping:
                    break
                if time.monotonic() - started > 60:
                    backoff = 2  # the session was healthy for a while, so don't punish the reconnect
                log.error("MQTT connection lost: %s. Reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stopping = True
        self.wake.set()

    async def _session(self) -> None:
        s = self.settings
        status = s.status_topic
        tls_context = ssl.create_default_context() if s.mqtt_tls else None
        async with aiomqtt.Client(
            s.mqtt_host,
            port=s.mqtt_port,
            username=s.mqtt_username or None,
            password=s.mqtt_password or None,
            tls_context=tls_context,
            identifier=f"z2m-auto-update-{uuid.uuid4().hex[:8]}",
            will=aiomqtt.Will(f"{status}/availability", "offline", retain=True),
            keepalive=60,
        ) as client:
            self.client = client
            self.connected = True
            log.info("Connected to MQTT broker %s:%s", s.mqtt_host, s.mqtt_port)
            await client.subscribe(f"{s.base_topic}/#")
            await client.subscribe(f"{status}/command")
            await client.subscribe(f"{status}/paused/set")
            await client.subscribe(f"{s.ha_discovery_prefix}/status")
            await self._publish_discovery()
            await client.publish(f"{status}/availability", "online", retain=True)
            self._last_status = None
            ticker = asyncio.create_task(self._ticker())
            try:
                async for message in client.messages:
                    await self._handle(message)
                    if self._stopping:
                        break
            finally:
                ticker.cancel()
                self.connected = False
                if self._stopping:
                    try:
                        await client.publish(f"{status}/availability", "offline", retain=True)
                    except aiomqtt.MqttError:
                        pass

    # -------------------------------------------------------------- messages
    async def _handle(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)
        raw = message.payload if isinstance(message.payload, (bytes, bytearray)) else b""
        s = self.settings
        base = s.base_topic + "/"
        status = s.status_topic

        if topic == f"{status}/command":
            await self._command(raw.decode(errors="replace").strip())
            return
        if topic == f"{status}/paused/set":
            if raw.decode(errors="replace").strip().upper() == "ON":
                self.engine.pause()
            else:
                self.engine.resume()
            self.wake.set()
            return
        if topic == f"{s.ha_discovery_prefix}/status":
            if raw.decode(errors="replace").strip().lower() == "online":
                await self._publish_discovery()
                self._last_status = None
                self.wake.set()
            return
        if not topic.startswith(base):
            return

        rest = topic[len(base):]
        payload = _json(raw)
        engine = self.engine
        if rest == "bridge/state":
            engine.on_bridge_state(payload)
        elif rest == "bridge/devices":
            if isinstance(payload, list):
                engine.on_devices(payload)
        elif rest == "bridge/response/device/ota_update/check":
            log.debug("Check response: %s", payload)
            if isinstance(payload, dict):
                engine.on_check_response(payload)
        elif rest == "bridge/response/device/ota_update/update":
            log.debug("Update response: %s", payload)
            if isinstance(payload, dict):
                engine.on_update_response(payload)
        elif rest.startswith("bridge/"):
            return
        elif rest.endswith("/availability"):
            engine.on_availability(rest[: -len("/availability")], payload)
        elif isinstance(payload, dict) and "update" in payload:
            engine.on_device_state(rest, payload)
        else:
            return
        self.wake.set()

    async def _command(self, command: str) -> None:
        engine = self.engine
        actions = {
            "pause": engine.pause,
            "resume": engine.resume,
            "run_now": engine.trigger_run_now,
            "check_all": engine.check_all,
            "reset_failures": engine.reset_failures,
        }
        action = actions.get(command)
        if action is None:
            log.warning("Unknown command %r", command)
            return
        action()
        self.wake.set()

    # ------------------------------------------------------------ publishing
    async def publish(self, messages: list[Publish]) -> None:
        if not self.client or not messages:
            return
        for m in messages:
            await self.client.publish(m.topic, m.payload, retain=m.retain)

    async def _publish_discovery(self) -> None:
        if self.settings.ha_discovery:
            await self.publish(discovery_messages(self.settings.ha_discovery_prefix, self.settings.status_topic))

    async def _publish_status(self, force: bool = False) -> None:
        payload = json.dumps(status_payload(self.engine.snapshot()), sort_keys=True)
        if force or payload != self._last_status:
            self._last_status = payload
            await self.publish([Publish(f"{self.settings.status_topic}/state", payload, retain=True)])

    async def _ticker(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1.0)
            except TimeoutError:
                pass
            self.wake.clear()
            try:
                await self.publish(self.engine.tick())
                await self._publish_status()
                if self.engine.dirty:
                    self.engine.dirty = False
                    persist.save(self.settings.state_file, self.engine.persisted_state())
            except aiomqtt.MqttError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Error in tick")


def _json(raw: bytes) -> Any:
    text = raw.decode(errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
