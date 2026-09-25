"""Tiny JSON file persistence for the bits of engine state worth keeping across restarts."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def load(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return {}


def save(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, p)
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)
