"""Entry point: ``python -m z2m_auto_update``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiohttp import web

from . import __version__, persist
from .config import load_settings
from .engine import Engine
from .runner import Runner, supervisor_mqtt
from .web import build_app


async def main_async(options_path: str | None) -> int:
    settings = load_settings(options_path)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log = logging.getLogger("z2m_auto_update")
    log.info("Z2M Auto Update %s starting", __version__)

    if not settings.mqtt_host and not await supervisor_mqtt(settings):
        log.error(
            "No MQTT broker configured. Set mqtt_host (or Z2M_AU_MQTT_HOST) or enable the Supervisor MQTT service."
        )
        return 2

    engine = Engine(settings)
    engine.restore_state(persist.load(settings.state_file))
    runner = Runner(settings, engine)

    app = build_app(engine, runner.wake)
    web_runner = web.AppRunner(app, access_log=None)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", settings.web_port)
    await site.start()
    log.info("Web UI listening on port %s", settings.web_port)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(runner.run())
    await stop.wait()
    log.info("Shutting down")
    runner.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    persist.save(settings.state_file, engine.persisted_state())
    await web_runner.cleanup()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue Zigbee2MQTT OTA updates one at a time")
    parser.add_argument("--options", help="path to a JSON options file (default: /data/options.json)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.options)))


if __name__ == "__main__":
    main()
