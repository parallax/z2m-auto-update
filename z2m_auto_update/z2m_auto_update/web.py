"""Ingress web UI and JSON API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .engine import Engine

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def build_app(engine: Engine, wake) -> web.Application:
    app = web.Application()
    app["engine"] = engine
    app["wake"] = wake
    app.add_routes(
        [
            web.get("/", index),
            web.get("/index.html", index),
            web.get("/api/state", state),
            web.get("/api/health", health),
            web.post("/api/action", action),
        ]
    )
    return app


async def index(request: web.Request) -> web.Response:
    html = (STATIC / "index.html").read_text()
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def state(request: web.Request) -> web.Response:
    engine: Engine = request.app["engine"]
    data = engine.snapshot()
    data["version"] = __version__
    data["settings"] = _public_settings(engine)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def action(request: web.Request) -> web.Response:
    engine: Engine = request.app["engine"]
    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text="expected a JSON body") from None
    name = body.get("action")
    ieee = body.get("ieee")
    global_actions = {
        "pause": engine.pause,
        "resume": engine.resume,
        "run_now": engine.trigger_run_now,
        "check_all": engine.check_all,
        "reset_failures": engine.reset_failures,
    }
    device_actions = {
        "check": engine.check_device,
        "update": engine.update_device,
        "skip": engine.skip_device,
        "unskip": engine.unskip_device,
        "retry": engine.retry_device,
    }
    if name in global_actions:
        result = global_actions[name]()
    elif name in device_actions:
        if not ieee:
            raise web.HTTPBadRequest(text="ieee is required")
        result = device_actions[name](ieee)
        if result is False:
            raise web.HTTPNotFound(text="unknown device")
    else:
        raise web.HTTPBadRequest(text=f"unknown action {name!r}")
    request.app["wake"].set()
    return web.json_response({"ok": True, "result": result})


def _public_settings(engine: Engine) -> dict[str, Any]:
    s = engine.settings
    return {
        "concurrent_updates": s.concurrent_updates,
        "update_window_start": s.update_window_start,
        "update_window_end": s.update_window_end,
        "include_battery": s.include_battery,
        "allow": s.allow,
        "deny": s.deny,
        "max_attempts": s.max_attempts,
        "retry_delay_minutes": s.retry_delay_minutes,
        "start_timeout_seconds": s.start_timeout_seconds,
        "stall_timeout_seconds": s.stall_timeout_seconds,
        "cooldown_seconds": s.cooldown_seconds,
        "check_interval_hours": s.check_interval_hours,
        "check_spacing_seconds": s.check_spacing_seconds,
        "base_topic": s.base_topic,
    }
