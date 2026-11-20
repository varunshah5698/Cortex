"""Structured JSON-ish logging written to logs/harness.log."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config.settings import LOGS_ROOT

# Marks the handler this module owns, so a moved LOGS_ROOT replaces our handler and
# leaves any handler someone else attached alone.
_OWNED = "_harness_log_file"


def _log_path() -> Path:
    return Path(LOGS_ROOT) / "harness.log"


def get_logger(name: str = "harness") -> logging.Logger:
    """The run's file logger, pointed at the current LOGS_ROOT.

    LOGS_ROOT is read on every call rather than captured once: a test or an eval suite
    that redirects it must not keep appending to the project's log through a handler
    opened earlier in the process.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    path = _log_path()
    for handler in list(logger.handlers):
        if not getattr(handler, _OWNED, None):
            continue
        if getattr(handler, _OWNED) == str(path):
            return logger
        logger.removeHandler(handler)
        handler.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _OWNED, str(path))
    logger.addHandler(handler)
    return logger


def event_log(run_id: str, kind: str, **fields) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "run": run_id, "kind": kind, **fields}
    get_logger().info(json.dumps(payload, default=str))
