"""Structured JSON-ish logging written to logs/harness.log."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config.settings import LOGS_ROOT


def get_logger(name: str = "harness") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(Path(LOGS_ROOT) / "harness.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def event_log(run_id: str, kind: str, **fields) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "run": run_id, "kind": kind, **fields}
    get_logger().info(json.dumps(payload, default=str))
